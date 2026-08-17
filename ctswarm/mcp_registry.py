"""Operator-controlled MCP server registry.

Before this module, MCP "support" meant two things, neither of which the
operator controlled:

- ``project_workspace.discover_mcp_servers`` read the host's ``~/.claude.json``
  and ``~/.codex/config.toml`` and produced a secret-free inventory for display
- the launch API accepted a list of server ids, and that list was folded into
  the *prompt* as a bullet list of names

The servers actually worked only because the compose file bind-mounts the whole
host configuration into the agent containers. So every build got every server
on the host regardless of what the operator selected, the selection was
decoration, and there was no way to add, edit, or remove anything without
hand-editing files on the host.

This module owns the list instead. The registry lives in the ledger under
``mcp_registry_v1``; secret values live beside the database in a file this
module creates ``0600``, and never travel through the API. Rendering turns the
enabled entries into the two configuration formats the CLIs actually read,
which were verified against the shipped ``claude mcp add`` and ``codex mcp add``
rather than inferred from documentation:

``.mcp.json`` (Claude Code)::

    {"mcpServers": {"name": {"type": "stdio", "command": ..., "args": [...],
                             "env": {...}}}}

``config.toml`` (Codex)::

    [mcp_servers.name]
    command = "npx"
    args = ["-y", "@scope/server"]

    [mcp_servers.name.env]
    FOO = "bar"

The two formats are not equivalent, and the difference is not cosmetic: Codex
has no per-server header support, only ``bearer_token_env_var``. An HTTP server
that authenticates with a custom header can be given to Claude and cannot be
given to Codex. Rendering therefore reports what it skipped and why, because a
server that silently vanishes from one harness is a build that fails for a
reason nobody can see.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomllib

from .ledger import Ledger

MCP_REGISTRY_SETTING = "mcp_registry_v1"
MCP_REGISTRY_UPDATED = "mcp_registry_updated"

TRANSPORTS = ("stdio", "http", "sse")
RUNTIMES = ("claude_code", "codex")

#: Codex speaks stdio and streamable HTTP. It has no SSE transport, so an SSE
#: entry is a Claude-only entry no matter what the operator selects.
CODEX_TRANSPORTS = frozenset({"stdio", "http"})

MAX_SERVERS = 64
MAX_ARGS = 32

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$%&'*+.^_`|~-]*$")


class McpRegistryError(ValueError):
    """Raised when an operator payload cannot be stored safely."""


def slugify(value: str) -> str:
    """Turn a display name into a stable id."""
    lowered = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower()).strip("-_")
    return lowered[:64] or "server"


@dataclass(frozen=True)
class McpServer:
    """One MCP server as the operator configured it.

    Secret values are deliberately absent. This object is safe to serialize to
    the browser as-is, which is what keeps the privacy guarantee on the API a
    property of the type rather than of every endpoint that touches it.
    """

    id: str
    name: str
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    url: str = ""
    #: Non-secret values only. Names whose values are secret appear in
    #: ``secret_env`` instead, and are filled in at render time.
    env: dict[str, str] = field(default_factory=dict)
    secret_env: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)
    secret_headers: tuple[str, ...] = ()
    #: Codex reads a bearer token from the process environment rather than
    #: accepting a header literal, so this names a variable rather than holding
    #: a value.
    bearer_token_env_var: str = ""
    enabled: bool = True
    runtimes: tuple[str, ...] = RUNTIMES
    note: str = ""
    #: ``registry`` for something the operator added, or the host file an
    #: imported entry came from. Provenance survives so an operator can tell
    #: what they chose from what was inherited.
    source: str = "registry"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "env": dict(self.env),
            "secret_env": list(self.secret_env),
            "headers": dict(self.headers),
            "secret_headers": list(self.secret_headers),
            "bearer_token_env_var": self.bearer_token_env_var,
            "enabled": self.enabled,
            "runtimes": list(self.runtimes),
            "note": self.note,
            "source": self.source,
        }

    def supports(self, runtime: str) -> bool:
        if runtime not in self.runtimes:
            return False
        if runtime == "codex":
            return self.transport in CODEX_TRANSPORTS
        return True


def _string_map(value: Any, *, label: str, pattern: re.Pattern[str]) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise McpRegistryError(f"{label} must be an object")
    result: dict[str, str] = {}
    for key, item in value.items():
        name = str(key).strip()
        if not pattern.match(name):
            raise McpRegistryError(f"{label} name {name!r} is not valid")
        result[name] = str(item)
    return result


def _name_tuple(value: Any, *, label: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise McpRegistryError(f"{label} must be a list")
    names: list[str] = []
    for item in value:
        name = str(item).strip()
        if not pattern.match(name):
            raise McpRegistryError(f"{label} name {name!r} is not valid")
        names.append(name)
    return tuple(dict.fromkeys(names))


def normalize_server(payload: Any, *, existing_ids: set[str] | None = None) -> McpServer:
    """Validate one operator-supplied server definition."""
    if not isinstance(payload, dict):
        raise McpRegistryError("server must be an object")

    name = str(payload.get("name") or "").strip()
    if not name:
        raise McpRegistryError("server name is required")

    identifier = str(payload.get("id") or "").strip().lower() or slugify(name)
    if not _ID_PATTERN.match(identifier):
        raise McpRegistryError(
            f"server id {identifier!r} must be lowercase letters, digits, dashes, "
            "or underscores"
        )

    transport = str(payload.get("transport") or "").strip().lower()
    if transport not in TRANSPORTS:
        raise McpRegistryError(f"transport must be one of {', '.join(TRANSPORTS)}")

    command = str(payload.get("command") or "").strip()
    url = str(payload.get("url") or "").strip()

    raw_args = payload.get("args") or []
    if not isinstance(raw_args, list | tuple):
        raise McpRegistryError("args must be a list")
    if len(raw_args) > MAX_ARGS:
        raise McpRegistryError(f"args may hold at most {MAX_ARGS} entries")
    args = tuple(str(item) for item in raw_args)

    if transport == "stdio":
        if not command:
            raise McpRegistryError("a stdio server needs a command")
        if url:
            raise McpRegistryError("a stdio server must not carry a url")
    else:
        if not url:
            raise McpRegistryError(f"a {transport} server needs a url")
        if not url.startswith(("http://", "https://")):
            raise McpRegistryError("url must be http:// or https://")
        if command:
            raise McpRegistryError(f"a {transport} server must not carry a command")

    runtimes = _name_tuple(
        payload.get("runtimes") or list(RUNTIMES),
        label="runtime",
        pattern=re.compile(r"^[a-z_]+$"),
    )
    unknown = [item for item in runtimes if item not in RUNTIMES]
    if unknown:
        raise McpRegistryError(f"unknown runtime: {', '.join(unknown)}")
    if not runtimes:
        raise McpRegistryError("a server must target at least one runtime")

    bearer = str(payload.get("bearer_token_env_var") or "").strip()
    if bearer and not _ENV_NAME_PATTERN.match(bearer):
        raise McpRegistryError(f"bearer token variable {bearer!r} is not valid")

    server = McpServer(
        id=identifier,
        name=name,
        transport=transport,
        command=command,
        args=args,
        url=url,
        env=_string_map(payload.get("env"), label="env", pattern=_ENV_NAME_PATTERN),
        secret_env=_name_tuple(
            payload.get("secret_env"), label="env", pattern=_ENV_NAME_PATTERN
        ),
        headers=_string_map(
            payload.get("headers"), label="header", pattern=_HEADER_NAME_PATTERN
        ),
        secret_headers=_name_tuple(
            payload.get("secret_headers"), label="header", pattern=_HEADER_NAME_PATTERN
        ),
        bearer_token_env_var=bearer,
        enabled=bool(payload.get("enabled", True)),
        runtimes=runtimes,
        note=str(payload.get("note") or "").strip(),
        source=str(payload.get("source") or "registry").strip() or "registry",
    )

    overlap = set(server.env) & set(server.secret_env)
    if overlap:
        raise McpRegistryError(
            f"{', '.join(sorted(overlap))} cannot be both a plain and a secret value"
        )
    overlap = set(server.headers) & set(server.secret_headers)
    if overlap:
        raise McpRegistryError(
            f"{', '.join(sorted(overlap))} cannot be both a plain and a secret header"
        )

    if existing_ids is not None and identifier in existing_ids:
        raise McpRegistryError(f"a server with id {identifier!r} already exists")

    return server


# -- storage ---------------------------------------------------------------


def load_registry(ledger: Ledger) -> list[McpServer]:
    """Every configured server, in display order.

    A stored entry that no longer validates is dropped rather than raising.
    The registry is read on the launch path, and a single malformed entry
    written by an older version must not make it impossible to start a build.
    """
    stored = ledger.setting(MCP_REGISTRY_SETTING, None)
    if not isinstance(stored, list):
        return []
    servers: list[McpServer] = []
    seen: set[str] = set()
    for entry in stored:
        try:
            server = normalize_server(entry)
        except McpRegistryError:
            continue
        if server.id in seen:
            continue
        seen.add(server.id)
        servers.append(server)
    servers.sort(key=lambda item: (item.name.lower(), item.id))
    return servers


def save_registry(
    ledger: Ledger,
    servers: list[McpServer],
    *,
    changed_by: str = "mission-control",
    action: str = "replaced",
) -> list[McpServer]:
    """Persist the whole registry and record what changed."""
    if len(servers) > MAX_SERVERS:
        raise McpRegistryError(f"at most {MAX_SERVERS} servers may be registered")
    previous = {server.id for server in load_registry(ledger)}
    ordered = sorted(servers, key=lambda item: (item.name.lower(), item.id))
    ledger.set_setting(MCP_REGISTRY_SETTING, [server.to_dict() for server in ordered])
    current = {server.id for server in ordered}
    ledger.record_event(
        MCP_REGISTRY_UPDATED,
        {
            "changed_by": changed_by,
            "action": action,
            "added": sorted(current - previous),
            "removed": sorted(previous - current),
            "total": len(ordered),
        },
    )
    return ordered


def upsert_server(
    ledger: Ledger,
    payload: Any,
    *,
    server_id: str | None = None,
    changed_by: str = "mission-control",
) -> list[McpServer]:
    """Add a server, or replace the one at ``server_id``."""
    existing = load_registry(ledger)
    if server_id is None:
        taken = {server.id for server in existing}
        server = normalize_server(payload, existing_ids=taken)
        return save_registry(
            ledger, [*existing, server], changed_by=changed_by, action="added"
        )

    if not any(server.id == server_id for server in existing):
        raise McpRegistryError(f"no MCP server with id {server_id!r}")
    updated = normalize_server({**payload, "id": server_id})
    replaced = [updated if item.id == server_id else item for item in existing]
    return save_registry(ledger, replaced, changed_by=changed_by, action="updated")


def delete_server(
    ledger: Ledger, server_id: str, *, changed_by: str = "mission-control"
) -> list[McpServer]:
    """Remove a server and forget its secrets."""
    existing = load_registry(ledger)
    if not any(server.id == server_id for server in existing):
        raise McpRegistryError(f"no MCP server with id {server_id!r}")
    remaining = [server for server in existing if server.id != server_id]
    result = save_registry(ledger, remaining, changed_by=changed_by, action="removed")
    # Orphaned secrets are a liability, not a convenience. A server removed and
    # re-added under the same id must not silently inherit the old credential.
    secrets = load_secrets(ledger)
    if secrets.pop(server_id, None) is not None:
        write_secrets(ledger, secrets)
    return result


# -- secrets ---------------------------------------------------------------


def secrets_path(ledger: Ledger) -> Path:
    """Where secret values live: beside the database, never in it.

    The ledger file is world-readable by design; several tools inspect it. A
    credential in there would be a credential in every backup and every
    ``sqlite3`` session, so it gets its own ``0600`` file instead.
    """
    override = os.environ.get("CTSWARM_MCP_SECRETS", "").strip()
    if override:
        return Path(override).expanduser()
    return ledger.path.parent / "mcp-secrets.json"


def load_secrets(ledger: Ledger) -> dict[str, dict[str, dict[str, str]]]:
    path = secrets_path(ledger)
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, dict[str, dict[str, str]]] = {}
    for server_id, buckets in payload.items():
        if not isinstance(buckets, dict):
            continue
        result[str(server_id)] = {
            bucket: {str(k): str(v) for k, v in values.items()}
            for bucket, values in buckets.items()
            if bucket in {"env", "headers"} and isinstance(values, dict)
        }
    return result


def write_secrets(ledger: Ledger, secrets: dict[str, dict[str, dict[str, str]]]) -> None:
    path = secrets_path(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    # Create the file empty and restricted, then write. Writing first and
    # chmod-ing after leaves a window where the credential is readable.
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as stream:
        json.dump(secrets, stream, indent=2, sort_keys=True)
        stream.write("\n")
    temporary.replace(path)


def set_secrets(
    ledger: Ledger,
    server_id: str,
    *,
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    """Store secret values for one server.

    An empty string clears one value, which is how a settings form removes a
    credential without a separate delete affordance.
    """
    secrets = load_secrets(ledger)
    bucket = secrets.setdefault(server_id, {})
    for label, values in (("env", env), ("headers", headers)):
        if values is None:
            continue
        target = bucket.setdefault(label, {})
        for key, value in values.items():
            if value == "":
                target.pop(key, None)
            else:
                target[key] = str(value)
        if not target:
            bucket.pop(label, None)
    if not bucket:
        secrets.pop(server_id, None)
    write_secrets(ledger, secrets)


def secret_status(ledger: Ledger, servers: list[McpServer]) -> dict[str, dict[str, list[str]]]:
    """Which declared secrets actually have a value stored.

    A server declaring ``secret_env: ["TOKEN"]`` with nothing stored will start
    and then fail on its first authenticated call. Saying so on the settings
    screen is cheaper than discovering it mid-build.
    """
    secrets = load_secrets(ledger)
    report: dict[str, dict[str, list[str]]] = {}
    for server in servers:
        stored = secrets.get(server.id, {})
        env_present = set(stored.get("env", {}))
        header_present = set(stored.get("headers", {}))
        report[server.id] = {
            "env_set": sorted(name for name in server.secret_env if name in env_present),
            "env_missing": sorted(
                name for name in server.secret_env if name not in env_present
            ),
            "headers_set": sorted(
                name for name in server.secret_headers if name in header_present
            ),
            "headers_missing": sorted(
                name for name in server.secret_headers if name not in header_present
            ),
        }
    return report


# -- rendering -------------------------------------------------------------


def _resolved_env(server: McpServer, secrets: dict) -> dict[str, str]:
    stored = secrets.get(server.id, {}).get("env", {})
    values = dict(server.env)
    for name in server.secret_env:
        if name in stored:
            values[name] = stored[name]
    return values


def _resolved_headers(server: McpServer, secrets: dict) -> dict[str, str]:
    stored = secrets.get(server.id, {}).get("headers", {})
    values = dict(server.headers)
    for name in server.secret_headers:
        if name in stored:
            values[name] = stored[name]
    return values


def render_claude_config(
    servers: list[McpServer], secrets: dict | None = None
) -> dict[str, Any]:
    """Render the enabled servers into Claude Code's ``mcpServers`` shape."""
    secrets = secrets or {}
    entries: dict[str, Any] = {}
    for server in servers:
        if not server.enabled or not server.supports("claude_code"):
            continue
        if server.transport == "stdio":
            entry: dict[str, Any] = {"type": "stdio", "command": server.command}
            if server.args:
                entry["args"] = list(server.args)
            env = _resolved_env(server, secrets)
            if env:
                entry["env"] = env
        else:
            entry = {"type": server.transport, "url": server.url}
            headers = _resolved_headers(server, secrets)
            if headers:
                entry["headers"] = headers
        entries[server.name] = entry
    return {"mcpServers": entries}


def _toml_string(value: str) -> str:
    return json.dumps(value)


def render_codex_config(
    servers: list[McpServer], secrets: dict | None = None
) -> tuple[str, list[dict[str, str]]]:
    """Render the enabled servers into Codex's ``[mcp_servers.*]`` tables.

    Returns the TOML text and the list of servers Codex cannot represent, with
    the reason for each. Callers are expected to surface the second value: an
    operator who enabled a server for Codex and never sees it should be told
    the format could not carry it, not left to infer it from a failing build.
    """
    secrets = secrets or {}
    lines: list[str] = []
    skipped: list[dict[str, str]] = []

    for server in servers:
        if not server.enabled:
            continue
        if "codex" not in server.runtimes:
            continue
        if server.transport not in CODEX_TRANSPORTS:
            skipped.append(
                {
                    "id": server.id,
                    "reason": (
                        f"Codex has no {server.transport} transport; this server "
                        "is available to Claude Code only."
                    ),
                }
            )
            continue

        if server.transport == "stdio":
            lines.append(f"[mcp_servers.{server.name}]")
            lines.append(f"command = {_toml_string(server.command)}")
            if server.args:
                rendered = ", ".join(_toml_string(item) for item in server.args)
                lines.append(f"args = [{rendered}]")
            env = _resolved_env(server, secrets)
            if env:
                lines.append("")
                lines.append(f"[mcp_servers.{server.name}.env]")
                for key in sorted(env):
                    lines.append(f"{key} = {_toml_string(env[key])}")
            lines.append("")
            continue

        headers = _resolved_headers(server, secrets)
        if headers and not server.bearer_token_env_var:
            skipped.append(
                {
                    "id": server.id,
                    "reason": (
                        "Codex cannot send per-server headers. Set a bearer token "
                        "environment variable on this server, or restrict it to "
                        "Claude Code."
                    ),
                }
            )
            continue
        lines.append(f"[mcp_servers.{server.name}]")
        lines.append(f"url = {_toml_string(server.url)}")
        if server.bearer_token_env_var:
            lines.append(
                f"bearer_token_env_var = {_toml_string(server.bearer_token_env_var)}"
            )
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n" if lines else "", skipped


def materialize(ledger: Ledger, destination: Path) -> dict[str, Any]:
    """Write both harness configurations for the currently enabled servers.

    The files are written ``0600`` and replaced atomically, because they carry
    resolved credentials and because a half-written config read by a starting
    container is a failure that looks like a broken MCP server.
    """
    servers = load_registry(ledger)
    secrets = load_secrets(ledger)

    destination.mkdir(parents=True, exist_ok=True)
    claude_payload = render_claude_config(servers, secrets)
    codex_text, skipped = render_codex_config(servers, secrets)

    _write_private(
        destination / "claude.json", json.dumps(claude_payload, indent=2) + "\n"
    )
    _write_private(destination / "codex.toml", codex_text)

    enabled = [server for server in servers if server.enabled]
    return {
        "path": str(destination),
        "servers": len(servers),
        "enabled": len(enabled),
        "claude": sorted(claude_payload["mcpServers"]),
        "codex": sorted(
            server.name
            for server in enabled
            if server.supports("codex")
            and not any(item["id"] == server.id for item in skipped)
        ),
        "skipped": skipped,
    }


def _write_private(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as stream:
        stream.write(text)
    temporary.replace(path)


# -- importing what the host already has ------------------------------------


def _import_candidates(
    claude_path: Path, codex_path: Path
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    try:
        claude = json.loads(claude_path.read_text())
    except (OSError, ValueError):
        claude = {}
    if isinstance(claude, dict):
        for name, config in (claude.get("mcpServers") or {}).items():
            if isinstance(config, dict):
                candidates.append(_candidate_from_claude(str(name), config))

    try:
        codex = tomllib.loads(codex_path.read_text())
    except (OSError, ValueError):
        codex = {}
    if isinstance(codex, dict):
        for name, config in (codex.get("mcp_servers") or {}).items():
            if isinstance(config, dict):
                candidates.append(_candidate_from_codex(str(name), config))

    return [item for item in candidates if item]


def _candidate_from_claude(name: str, config: dict) -> dict[str, Any]:
    url = str(config.get("url") or "")
    declared = str(config.get("type") or "").strip().lower()
    transport = declared if declared in TRANSPORTS else ("http" if url else "stdio")
    return {
        "id": slugify(name),
        "name": name,
        "transport": transport,
        "command": str(config.get("command") or ""),
        "args": [str(item) for item in (config.get("args") or [])],
        "url": url,
        # Imported values are carried across as-is rather than guessed at. A
        # heuristic that decides which of these is a secret would be wrong in
        # both directions: it would expose credentials it failed to spot, and
        # hide harmless configuration it flagged.
        "env": config.get("env") or {},
        "headers": config.get("headers") or {},
        "runtimes": ["claude_code", "codex"] if transport != "sse" else ["claude_code"],
        "source": "claude",
        "note": "Imported from the host Claude configuration.",
    }


def _candidate_from_codex(name: str, config: dict) -> dict[str, Any]:
    url = str(config.get("url") or "")
    return {
        "id": slugify(name),
        "name": name,
        "transport": "http" if url else "stdio",
        "command": str(config.get("command") or ""),
        "args": [str(item) for item in (config.get("args") or [])],
        "url": url,
        "env": config.get("env") or {},
        "bearer_token_env_var": str(config.get("bearer_token_env_var") or ""),
        "runtimes": ["claude_code", "codex"],
        "source": "codex",
        "note": "Imported from the host Codex configuration.",
    }


def import_discovered(
    ledger: Ledger,
    *,
    claude_path: Path | None = None,
    codex_path: Path | None = None,
    changed_by: str = "mission-control",
) -> dict[str, Any]:
    """Adopt the host's existing MCP servers into the registry.

    Import is additive and never overwrites. An operator who has edited an
    imported entry must not have that edit reverted the next time discovery
    runs, so an id that already exists is reported as skipped rather than
    silently replaced.
    """
    # Resolved by project_workspace so discovery and import always read the
    # same files. In a container these are mounted, not under $HOME.
    from .project_workspace import claude_config_path, codex_config_path

    claude_path = claude_path or claude_config_path()
    codex_path = codex_path or codex_config_path()

    existing = load_registry(ledger)
    taken = {server.id for server in existing}
    added: list[str] = []
    skipped: list[dict[str, str]] = []
    accepted: list[McpServer] = []

    for candidate in _import_candidates(claude_path, codex_path):
        if candidate["id"] in taken:
            skipped.append({"id": candidate["id"], "reason": "already in the registry"})
            continue
        try:
            server = normalize_server(candidate)
        except McpRegistryError as exc:
            skipped.append({"id": candidate.get("id", "?"), "reason": str(exc)})
            continue
        taken.add(server.id)
        accepted.append(server)
        added.append(server.id)

    if accepted:
        save_registry(
            ledger, [*existing, *accepted], changed_by=changed_by, action="imported"
        )

    return {"added": added, "skipped": skipped}


def ensure_seeded(
    ledger: Ledger,
    *,
    claude_path: Path | None = None,
    codex_path: Path | None = None,
) -> dict[str, Any]:
    """Import the host's servers the first time, and never again.

    Without this, turning on the registry would take away every MCP server an
    operator already had, because the rendered configuration replaces the
    inherited one. Seeding once reproduces what they had, after which the
    registry is authoritative and the host files are ignored.
    """
    if ledger.setting(MCP_REGISTRY_SETTING, None) is not None:
        return {"seeded": False, "added": [], "skipped": []}
    result = import_discovered(
        ledger, claude_path=claude_path, codex_path=codex_path, changed_by="bootstrap"
    )
    if not result["added"]:
        # Record the empty registry so a host with no MCP servers is not
        # re-scanned on every start, and so an operator who deliberately
        # removed everything does not get it all back on the next restart.
        save_registry(ledger, [], changed_by="bootstrap", action="seeded")
    return {"seeded": True, **result}


def selected_context(selected: list[str], servers: list[McpServer]) -> str:
    """The prompt half: tell the agents what they can reach.

    This does not grant access; ``materialize`` does. It exists so a planner
    does not ask the operator to configure a server that is already wired up.
    """
    by_id = {server.id: server for server in servers}
    chosen = [by_id[item] for item in selected if item in by_id and by_id[item].enabled]
    if not chosen:
        return "No MCP servers were enabled for this build."
    lines = [
        "MCP servers available to this build:",
        "These are already configured and connected. Use them when relevant; do "
        "not ask the operator to set them up again:",
    ]
    for server in chosen:
        harnesses = ", ".join(
            "Claude Code" if runtime == "claude_code" else "Codex"
            for runtime in server.runtimes
            if server.supports(runtime)
        )
        detail = f"- {server.name} ({server.transport}) via {harnesses}"
        if server.note:
            detail += f" -- {server.note}"
        lines.append(detail)
    return "\n".join(lines)


__all__ = [
    "CODEX_TRANSPORTS",
    "MAX_SERVERS",
    "MCP_REGISTRY_SETTING",
    "MCP_REGISTRY_UPDATED",
    "RUNTIMES",
    "TRANSPORTS",
    "McpRegistryError",
    "McpServer",
    "delete_server",
    "ensure_seeded",
    "import_discovered",
    "load_registry",
    "load_secrets",
    "materialize",
    "normalize_server",
    "render_claude_config",
    "render_codex_config",
    "save_registry",
    "secret_status",
    "secrets_path",
    "selected_context",
    "set_secrets",
    "slugify",
    "upsert_server",
]
