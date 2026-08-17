"""Tests for the MCP registry.

The interesting properties are not CRUD. They are:

- a secret value never appears in anything the API returns
- the rendered files match what the real CLIs write, not what documentation says
- adopting the host's existing servers happens once and is never undone
- a server Codex cannot represent is reported, not dropped
"""

from __future__ import annotations

import json
import stat

import pytest
import tomllib

from ctswarm.ledger import Ledger
from ctswarm.mcp_registry import (
    MCP_REGISTRY_SETTING,
    McpRegistryError,
    delete_server,
    ensure_seeded,
    import_discovered,
    load_registry,
    materialize,
    normalize_server,
    render_claude_config,
    render_codex_config,
    save_registry,
    secret_status,
    secrets_path,
    selected_context,
    set_secrets,
    slugify,
    upsert_server,
)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.delenv("CTSWARM_MCP_SECRETS", raising=False)
    return Ledger(tmp_path / "ctswarm.db")


STDIO = {
    "name": "drive",
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-gdrive"],
    "env": {"GDRIVE_ROOT": "/docs"},
    "secret_env": ["GDRIVE_TOKEN"],
}

REMOTE = {
    "name": "linear",
    "transport": "http",
    "url": "https://mcp.linear.app/mcp",
    "secret_headers": ["Authorization"],
    "runtimes": ["claude_code"],
}


# -- validation ------------------------------------------------------------


def test_a_stdio_server_needs_a_command():
    with pytest.raises(McpRegistryError, match="needs a command"):
        normalize_server({"name": "x", "transport": "stdio"})


def test_a_remote_server_needs_a_url():
    with pytest.raises(McpRegistryError, match="needs a url"):
        normalize_server({"name": "x", "transport": "http"})


def test_a_stdio_server_may_not_also_carry_a_url():
    """Both set means one of them is being ignored, and the operator cannot
    tell which. Rejecting is the only answer that is not a guess."""
    with pytest.raises(McpRegistryError, match="must not carry a url"):
        normalize_server(
            {"name": "x", "transport": "stdio", "command": "npx", "url": "https://x"}
        )


def test_a_url_must_be_http():
    with pytest.raises(McpRegistryError, match="http"):
        normalize_server({"name": "x", "transport": "http", "url": "file:///etc/passwd"})


def test_an_env_name_that_is_not_a_shell_identifier_is_rejected():
    with pytest.raises(McpRegistryError, match="not valid"):
        normalize_server({**STDIO, "env": {"not a name": "v"}})


def test_a_value_cannot_be_both_plain_and_secret():
    """Otherwise which one wins at render time is an implementation detail
    deciding whether a credential is stored in the clear."""
    with pytest.raises(McpRegistryError, match="both a plain and a secret"):
        normalize_server({**STDIO, "env": {"TOKEN": "abc"}, "secret_env": ["TOKEN"]})


def test_an_unknown_runtime_is_rejected():
    with pytest.raises(McpRegistryError, match="unknown runtime"):
        normalize_server({**STDIO, "runtimes": ["opencode"]})


def test_slugify_produces_a_usable_id():
    assert slugify("Google Drive (work)") == "google-drive-work"
    assert slugify("!!!") == "server"


# -- storage ---------------------------------------------------------------


def test_a_server_round_trips(ledger):
    upsert_server(ledger, STDIO)
    stored = load_registry(ledger)
    assert [item.id for item in stored] == ["drive"]
    assert stored[0].args == ("-y", "@modelcontextprotocol/server-gdrive")
    assert stored[0].secret_env == ("GDRIVE_TOKEN",)


def test_adding_the_same_id_twice_is_refused(ledger):
    upsert_server(ledger, STDIO)
    with pytest.raises(McpRegistryError, match="already exists"):
        upsert_server(ledger, STDIO)


def test_updating_a_missing_server_is_refused(ledger):
    with pytest.raises(McpRegistryError, match="no MCP server"):
        upsert_server(ledger, STDIO, server_id="nope")


def test_an_update_keeps_the_id_the_payload_cannot_change_it(ledger):
    upsert_server(ledger, STDIO)
    upsert_server(ledger, {**STDIO, "id": "somethingelse", "name": "drive"}, server_id="drive")
    assert [item.id for item in load_registry(ledger)] == ["drive"]


def test_a_corrupt_stored_entry_is_skipped_rather_than_raising(ledger):
    """The registry is read on the launch path. One bad row written by an
    older version must not make it impossible to start any build at all."""
    ledger.set_setting(
        MCP_REGISTRY_SETTING,
        [{"name": "broken", "transport": "stdio"}, {**STDIO, "id": "drive"}],
    )
    assert [item.id for item in load_registry(ledger)] == ["drive"]


def test_every_change_is_audited(ledger):
    upsert_server(ledger, STDIO)
    upsert_server(ledger, REMOTE)
    delete_server(ledger, "drive")
    events = ledger.events("mcp_registry_updated")
    actions = [json.loads(item["detail"])["action"] for item in events]
    assert actions == ["added", "added", "removed"]
    assert json.loads(events[-1]["detail"])["removed"] == ["drive"]


# -- secrets ---------------------------------------------------------------


def test_a_secret_value_is_never_in_the_public_payload(ledger):
    upsert_server(ledger, STDIO)
    set_secrets(ledger, "drive", env={"GDRIVE_TOKEN": "super-secret-value"})
    payload = json.dumps([item.to_dict() for item in load_registry(ledger)])
    assert "super-secret-value" not in payload
    assert "GDRIVE_TOKEN" in payload


def test_the_secret_file_is_not_readable_by_anyone_else(ledger):
    set_secrets(ledger, "drive", env={"GDRIVE_TOKEN": "v"})
    mode = stat.S_IMODE(secrets_path(ledger).stat().st_mode)
    assert mode == 0o600, f"secrets file is {oct(mode)}"


def test_secrets_do_not_live_in_the_ledger(ledger):
    """The database is inspected by several tools and copied by every backup."""
    upsert_server(ledger, STDIO)
    set_secrets(ledger, "drive", env={"GDRIVE_TOKEN": "super-secret-value"})
    assert "super-secret-value" not in ledger.path.read_bytes().decode(
        "utf-8", errors="ignore"
    )


def test_an_empty_string_clears_a_secret(ledger):
    set_secrets(ledger, "drive", env={"GDRIVE_TOKEN": "v"})
    set_secrets(ledger, "drive", env={"GDRIVE_TOKEN": ""})
    assert json.loads(secrets_path(ledger).read_text()) == {}


def test_deleting_a_server_forgets_its_secrets(ledger):
    """Otherwise re-adding the same id silently inherits the old credential."""
    upsert_server(ledger, STDIO)
    set_secrets(ledger, "drive", env={"GDRIVE_TOKEN": "v"})
    delete_server(ledger, "drive")
    assert json.loads(secrets_path(ledger).read_text()) == {}


def test_a_declared_secret_with_no_value_is_reported_as_missing(ledger):
    upsert_server(ledger, STDIO)
    report = secret_status(ledger, load_registry(ledger))
    assert report["drive"]["env_missing"] == ["GDRIVE_TOKEN"]
    set_secrets(ledger, "drive", env={"GDRIVE_TOKEN": "v"})
    report = secret_status(ledger, load_registry(ledger))
    assert report["drive"]["env_set"] == ["GDRIVE_TOKEN"]
    assert report["drive"]["env_missing"] == []


# -- rendering -------------------------------------------------------------


def test_the_claude_shape_matches_what_the_cli_writes():
    """Verified against `claude mcp add --scope project`, which produces
    {"mcpServers": {name: {"type", "command", "args", "env"}}}."""
    server = normalize_server(STDIO)
    rendered = render_claude_config([server], {"drive": {"env": {"GDRIVE_TOKEN": "v"}}})
    assert rendered == {
        "mcpServers": {
            "drive": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-gdrive"],
                "env": {"GDRIVE_ROOT": "/docs", "GDRIVE_TOKEN": "v"},
            }
        }
    }


def test_the_claude_shape_for_a_remote_server():
    server = normalize_server(REMOTE)
    rendered = render_claude_config(
        [server], {"linear": {"headers": {"Authorization": "Bearer t"}}}
    )
    assert rendered["mcpServers"]["linear"] == {
        "type": "http",
        "url": "https://mcp.linear.app/mcp",
        "headers": {"Authorization": "Bearer t"},
    }


def test_the_codex_output_is_valid_toml_in_the_shape_codex_writes():
    """Verified against `codex mcp add`, which produces [mcp_servers.<name>]
    with command/args and a nested [mcp_servers.<name>.env] table."""
    server = normalize_server(STDIO)
    text, skipped = render_codex_config([server], {"drive": {"env": {"GDRIVE_TOKEN": "v"}}})
    assert skipped == []
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["drive"]["command"] == "npx"
    assert parsed["mcp_servers"]["drive"]["args"] == [
        "-y",
        "@modelcontextprotocol/server-gdrive",
    ]
    assert parsed["mcp_servers"]["drive"]["env"] == {
        "GDRIVE_ROOT": "/docs",
        "GDRIVE_TOKEN": "v",
    }


def test_codex_gets_a_bearer_variable_not_a_header():
    server = normalize_server(
        {
            "name": "remote",
            "transport": "http",
            "url": "https://example.com/mcp",
            "bearer_token_env_var": "MY_TOKEN",
        }
    )
    text, skipped = render_codex_config([server])
    assert skipped == []
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["remote"] == {
        "url": "https://example.com/mcp",
        "bearer_token_env_var": "MY_TOKEN",
    }


def test_a_header_authenticated_server_is_reported_not_dropped_by_codex():
    """Codex has no per-server header support. Silently omitting the server
    would leave the operator debugging a tool that is simply absent."""
    server = normalize_server(
        {
            "name": "linear",
            "transport": "http",
            "url": "https://mcp.linear.app/mcp",
            "secret_headers": ["Authorization"],
            "runtimes": ["claude_code", "codex"],
        }
    )
    text, skipped = render_codex_config(
        [server], {"linear": {"headers": {"Authorization": "Bearer t"}}}
    )
    assert text == ""
    assert len(skipped) == 1
    assert "header" in skipped[0]["reason"]


def test_an_sse_server_is_reported_as_claude_only():
    server = normalize_server(
        {
            "name": "events",
            "transport": "sse",
            "url": "https://example.com/sse",
            "runtimes": ["claude_code", "codex"],
        }
    )
    _, skipped = render_codex_config([server])
    assert skipped[0]["reason"].startswith("Codex has no sse transport")
    assert "events" in render_claude_config([server])["mcpServers"]


def test_a_disabled_server_reaches_neither_harness():
    server = normalize_server({**STDIO, "enabled": False})
    assert render_claude_config([server])["mcpServers"] == {}
    assert render_codex_config([server]) == ("", [])


def test_runtime_targeting_is_honoured():
    server = normalize_server({**STDIO, "runtimes": ["claude_code"]})
    assert "drive" in render_claude_config([server])["mcpServers"]
    assert render_codex_config([server]) == ("", [])


# -- materialization -------------------------------------------------------


def test_materialize_writes_both_files_privately(ledger, tmp_path):
    upsert_server(ledger, STDIO)
    set_secrets(ledger, "drive", env={"GDRIVE_TOKEN": "v"})
    report = materialize(ledger, tmp_path / "mcp")

    claude = tmp_path / "mcp" / "claude.json"
    codex = tmp_path / "mcp" / "codex.toml"
    assert json.loads(claude.read_text())["mcpServers"]["drive"]["command"] == "npx"
    assert tomllib.loads(codex.read_text())["mcp_servers"]["drive"]["command"] == "npx"
    assert report["enabled"] == 1
    assert report["claude"] == ["drive"] and report["codex"] == ["drive"]

    for path in (claude, codex):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_materialize_is_rerunnable(ledger, tmp_path):
    upsert_server(ledger, STDIO)
    materialize(ledger, tmp_path / "mcp")
    first = (tmp_path / "mcp" / "claude.json").read_text()
    materialize(ledger, tmp_path / "mcp")
    assert (tmp_path / "mcp" / "claude.json").read_text() == first


def test_materialize_reports_what_codex_could_not_take(ledger, tmp_path):
    upsert_server(ledger, {**REMOTE, "runtimes": ["claude_code", "codex"]})
    set_secrets(ledger, "linear", headers={"Authorization": "Bearer t"})
    report = materialize(ledger, tmp_path / "mcp")
    assert report["claude"] == ["linear"]
    assert report["codex"] == []
    assert report["skipped"][0]["id"] == "linear"


def test_an_empty_registry_produces_an_empty_but_valid_config(ledger, tmp_path):
    """A container mounting these must start, not crash on a missing file."""
    materialize(ledger, tmp_path / "mcp")
    assert json.loads((tmp_path / "mcp" / "claude.json").read_text()) == {"mcpServers": {}}
    assert (tmp_path / "mcp" / "codex.toml").read_text() == ""


# -- importing -------------------------------------------------------------


def _host_files(tmp_path):
    claude = tmp_path / "claude.json"
    claude.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {"command": "npx", "args": ["-y", "@pw/mcp"]},
                    "sentry": {"type": "http", "url": "https://mcp.sentry.dev/mcp"},
                }
            }
        )
    )
    codex = tmp_path / "codex.toml"
    codex.write_text('[mcp_servers.grep]\ncommand = "rg"\nargs = ["--json"]\n')
    return claude, codex


def test_import_adopts_both_host_files(ledger, tmp_path):
    claude, codex = _host_files(tmp_path)
    result = import_discovered(ledger, claude_path=claude, codex_path=codex)
    assert sorted(result["added"]) == ["grep", "playwright", "sentry"]
    by_id = {item.id: item for item in load_registry(ledger)}
    assert by_id["playwright"].transport == "stdio"
    assert by_id["sentry"].transport == "http"
    assert by_id["grep"].source == "codex"


def test_import_never_overwrites_an_edited_entry(ledger, tmp_path):
    claude, codex = _host_files(tmp_path)
    import_discovered(ledger, claude_path=claude, codex_path=codex)
    upsert_server(
        ledger,
        {"name": "playwright", "transport": "stdio", "command": "edited"},
        server_id="playwright",
    )
    result = import_discovered(ledger, claude_path=claude, codex_path=codex)
    assert result["added"] == []
    assert {item["id"] for item in result["skipped"]} == {"playwright", "sentry", "grep"}
    by_id = {item.id: item for item in load_registry(ledger)}
    assert by_id["playwright"].command == "edited"


def test_import_survives_missing_host_files(ledger, tmp_path):
    result = import_discovered(
        ledger, claude_path=tmp_path / "nope.json", codex_path=tmp_path / "nope.toml"
    )
    assert result == {"added": [], "skipped": []}


def test_seeding_happens_once_and_is_not_undone(ledger, tmp_path):
    """Rendering replaces the inherited configuration, so without a one-time
    import an operator would silently lose every server they already had.
    Repeating the import would just as silently restore ones they removed."""
    claude, codex = _host_files(tmp_path)

    first = ensure_seeded(ledger, claude_path=claude, codex_path=codex)
    assert first["seeded"] is True
    assert len(first["added"]) == 3

    delete_server(ledger, "sentry")
    second = ensure_seeded(ledger, claude_path=claude, codex_path=codex)
    assert second["seeded"] is False
    assert {item.id for item in load_registry(ledger)} == {"playwright", "grep"}


def test_seeding_a_host_with_no_servers_still_marks_itself_done(ledger, tmp_path):
    """Otherwise every restart re-scans, and an operator who deliberately
    emptied the registry gets the host's servers back."""
    ensure_seeded(
        ledger, claude_path=tmp_path / "none.json", codex_path=tmp_path / "none.toml"
    )
    assert ledger.setting(MCP_REGISTRY_SETTING, None) == []
    assert ensure_seeded(ledger)["seeded"] is False


# -- prompt context --------------------------------------------------------


def test_the_prompt_context_names_the_harnesses(ledger):
    upsert_server(ledger, STDIO)
    text = selected_context(["drive"], load_registry(ledger))
    assert "drive" in text and "Claude Code" in text and "Codex" in text


def test_the_prompt_context_omits_a_disabled_server(ledger):
    upsert_server(ledger, {**STDIO, "enabled": False})
    assert "No MCP servers" in selected_context(["drive"], load_registry(ledger))


def test_the_prompt_context_ignores_an_unknown_selection(ledger):
    assert "No MCP servers" in selected_context(["ghost"], load_registry(ledger))


def test_the_registry_is_capped(ledger):
    many = [
        normalize_server({"name": f"s{index}", "transport": "stdio", "command": "x"})
        for index in range(65)
    ]
    with pytest.raises(McpRegistryError, match="at most 64"):
        save_registry(ledger, many)
