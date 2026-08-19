"""What happens when a subscription runs out.

`CapacityManager` has always been able to hold a rate-limited runtime out of
service, and `Orchestrator.submit` has always been able to collapse every role
onto the harness that still has headroom. Neither could ever happen: nothing in
the product called `note_rate_limited`, so the ledger never learned that a
subscription was spent. These tests hold that path open end to end, because it
is invisible until the day it matters and then it is the only thing that
matters.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ctswarm.capacity import CapacityManager, Runtime, rate_limit_signal
from ctswarm.ledger import Ledger
from ctswarm.orchestrator import BuildRecord, Orchestrator


@pytest.fixture(autouse=True)
def both_harnesses_logged_in(monkeypatch) -> None:
    """A host where both subscriptions work, so exhaustion is the only variable.

    `conftest` gives every test a credential-free home, which is right: it stops
    the developer's own login from answering assertions. But an unconfigured
    runtime is already unavailable for a different reason, so without a login
    these tests would pass whether or not the rate-limit path works at all.
    """
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
    codex_auth = Path.home() / ".codex" / "auth.json"
    codex_auth.parent.mkdir(parents=True, exist_ok=True)
    codex_auth.write_text(json.dumps({"tokens": {"access_token": "t"}}), encoding="utf-8")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Claude AI usage limit reached", Runtime.CLAUDE_CODE),
        ("anthropic: 429 Too Many Requests", Runtime.CLAUDE_CODE),
        ("Your credit balance is too low to run Claude", Runtime.CLAUDE_CODE),
        ("codex: you have hit your usage limit for this week", Runtime.CODEX),
        ("openai rate limit exceeded", Runtime.CODEX),
    ],
    ids=[
        "claude usage limit",
        "claude 429",
        "claude credit balance",
        "codex usage limit",
        "openai rate limit",
    ],
)
def test_a_named_harness_is_attributed(message, expected) -> None:
    assert rate_limit_signal(message) is expected


def test_an_unnamed_exhaustion_falls_back_to_the_caller_s_knowledge() -> None:
    """"usage limit reached" with no vendor named is still evidence."""
    assert (
        rate_limit_signal("usage limit reached", default=Runtime.CODEX)
        is Runtime.CODEX
    )


@pytest.mark.parametrize(
    "message",
    [
        "",
        "tests failed: 3 assertions",
        "the coder produced no diff",
        "connection reset by peer",
    ],
)
def test_an_ordinary_failure_is_not_an_exhaustion(message) -> None:
    """Attributing a normal failure would hold a working subscription out."""
    assert rate_limit_signal(message, default=Runtime.CLAUDE_CODE) is None


def test_a_message_naming_both_harnesses_attributes_neither() -> None:
    """Guessing wrong costs a working subscription a whole window.

    Recording nothing costs one more failure, which will name a single harness
    or come back with the same ambiguity. The cheaper mistake is the right
    default.
    """
    assert (
        rate_limit_signal(
            "both claude and codex report usage limits", default=Runtime.CODEX
        )
        is None
    )


def test_a_reported_exhaustion_takes_the_runtime_out_of_service(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    manager = CapacityManager(ledger=ledger)

    assert manager.headroom(Runtime.CODEX).available is True

    manager.note_rate_limited(Runtime.CODEX, detail="usage limit")

    codex = manager.headroom(Runtime.CODEX)
    assert codex.available is False
    assert "rate limited" in codex.reason
    # The other subscription must be untouched, or one exhausted harness would
    # take the whole box down with it.
    assert manager.headroom(Runtime.CLAUDE_CODE).available is True


def test_clearing_returns_the_runtime_to_service(tmp_path) -> None:
    """The window is a configured guess, so the operator has to be able to win."""
    ledger = Ledger(tmp_path / "ledger.db")
    manager = CapacityManager(ledger=ledger)
    manager.note_rate_limited(Runtime.CODEX)

    manager.clear_rate_limited(Runtime.CODEX)

    assert manager.headroom(Runtime.CODEX).available is True
    # Append-only: clearing supersedes the report rather than erasing it.
    assert ledger.events(kind="runtime_rate_limited")


def test_an_exhaustion_after_a_clear_still_counts(tmp_path) -> None:
    """Otherwise one clear would disable the whole mechanism forever."""
    ledger = Ledger(tmp_path / "ledger.db")
    manager = CapacityManager(ledger=ledger)

    manager.clear_rate_limited(Runtime.CODEX)
    manager.note_rate_limited(Runtime.CODEX, detail="spent again")

    assert manager.headroom(Runtime.CODEX).available is False


def _record(runtime: Runtime, error: str) -> BuildRecord:
    record = BuildRecord(
        build_id="build-1",
        goal="ship it",
        repo_url="https://example.invalid/repo.git",
        runtime=runtime,
    )
    record.error = error
    return record


def test_a_failed_build_teaches_capacity_which_harness_is_spent(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    orchestrator = Orchestrator(ledger=ledger)
    record = _record(Runtime.CLAUDE_CODE, "codex: usage limit reached")

    orchestrator._note_any_exhaustion(record)

    assert orchestrator.capacity.headroom(Runtime.CODEX).available is False
    # Attribution came from the message, not from the build's own runtime.
    assert orchestrator.capacity.headroom(Runtime.CLAUDE_CODE).available is True
    assert ledger.events(kind="runtime_exhausted_during_build")


def test_polling_the_same_failure_records_it_once(tmp_path) -> None:
    """`poll` runs every few seconds against an unchanging error message."""
    ledger = Ledger(tmp_path / "ledger.db")
    orchestrator = Orchestrator(ledger=ledger)
    record = _record(Runtime.CODEX, "usage limit reached")

    for _ in range(5):
        orchestrator._note_any_exhaustion(record)

    assert len(ledger.events(kind="runtime_rate_limited")) == 1


def test_an_ordinary_build_failure_records_nothing(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    orchestrator = Orchestrator(ledger=ledger)

    orchestrator._note_any_exhaustion(_record(Runtime.CODEX, "tests failed"))

    assert not ledger.events(kind="runtime_rate_limited")
    assert orchestrator.capacity.headroom(Runtime.CODEX).available is True


# --- the launch gate inside a container ------------------------------------
#
# The scheduler decides whether a build may start. It runs as uid 10001 and the
# credential files it mounts belong to the host account at mode 0600, so it can
# see them and not read them. Written as "parse the file", the gate refused the
# first real build on the Pi with "run `codex login`" on a host where
# `codex login` had already been done.


def _manager(tmp_path, monkeypatch, **env) -> CapacityManager:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return CapacityManager(ledger=Ledger(tmp_path / "gate.db"), env=dict(os.environ))


def test_the_gate_still_reads_the_files_when_nobody_asserts(tmp_path, monkeypatch) -> None:
    """The fallback is the default path; every host outside a container uses it."""
    manager = _manager(tmp_path, monkeypatch)

    assert manager.configured(Runtime.CODEX) is True  # fixture wrote auth.json
    assert manager.configured(Runtime.CLAUDE_CODE) is True


def test_an_unreadable_credential_can_be_vouched_for(monkeypatch, tmp_path) -> None:
    """The container case: the file exists, this process cannot read it."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CTSWARM_CODEX_HOME", str(tmp_path / "nothing-here"))
    manager = _manager(tmp_path, monkeypatch)
    assert manager.configured(Runtime.CODEX) is False

    manager = _manager(tmp_path, monkeypatch, CTSWARM_CODEX_LOGIN="1", CTSWARM_CLAUDE_LOGIN="1")

    assert manager.configured(Runtime.CODEX) is True
    assert manager.configured(Runtime.CLAUDE_CODE) is True


def test_an_explicit_no_closes_the_gate(tmp_path, monkeypatch) -> None:
    """The host is authoritative in both directions, not only the useful one."""
    manager = _manager(tmp_path, monkeypatch, CTSWARM_CODEX_LOGIN="0", CTSWARM_CLAUDE_LOGIN="0")

    assert manager.configured(Runtime.CODEX) is False
    assert manager.configured(Runtime.CLAUDE_CODE) is False


@pytest.mark.parametrize("value", ["", "  ", "maybe", "2"])
def test_a_value_that_is_not_an_answer_falls_back(tmp_path, monkeypatch, value) -> None:
    """A typo must not silently disable a harness that works."""
    manager = _manager(tmp_path, monkeypatch, CTSWARM_CODEX_LOGIN=value)

    assert manager.configured(Runtime.CODEX) is True


# --- reaching the ledger that decides --------------------------------------
#
# The scheduler's ledger lives in a Docker volume the host cannot open, so
# `ctswarm capacity` on the host read an empty database and `--rate-limited`
# wrote to one. The operator recorded a spent subscription and the launch gate
# never heard about it. These endpoints are how the CLI reaches the real one,
# the same way pause, resume, and stop always have.


def test_the_capacity_endpoints_are_registered() -> None:
    from ctswarm import scheduler as scheduler_module

    paths = {route.path for route in scheduler_module.app.routes}

    assert "/api/capacity" in paths
    assert "/api/capacity/{runtime}/rate-limited" in paths
    assert "/api/capacity/{runtime}/clear-limit" in paths


def test_an_unknown_runtime_is_rejected_rather_than_ignored() -> None:
    """A typo must not return 200 and silently do nothing to any runtime."""
    from fastapi import HTTPException

    from ctswarm.scheduler import _parse_runtime

    assert _parse_runtime("CODEX ") is Runtime.CODEX

    with pytest.raises(HTTPException) as caught:
        _parse_runtime("gpt")
    assert caught.value.status_code == 400
    assert "codex" in caught.value.detail


# --- probing a harness rather than inferring its state ----------------------


def _fake_cli(tmp_path, name: str, *, stdout: str, exit_code: int) -> None:
    """A stand-in CLI on PATH, so the probe is tested against real subprocesses."""
    script = tmp_path / name
    script.write_text(
        f"#!/bin/sh\ncat <<'OUT'\n{stdout}\nOUT\nexit {exit_code}\n", encoding="utf-8"
    )
    script.chmod(0o755)


@pytest.fixture
def on_path(tmp_path, monkeypatch):
    # The stand-ins come first, but the real /bin stays reachable so the scripts
    # can use ordinary tools. A harness the test never creates is still absent,
    # which is what the missing-binary case needs.
    monkeypatch.setenv("PATH", f"{tmp_path}:/bin:/usr/bin")
    return tmp_path


def test_a_working_harness_is_usable(on_path) -> None:
    from ctswarm.capacity import probe_runtime

    _fake_cli(on_path, "claude", stdout="OK", exit_code=0)

    usable, detail = probe_runtime(Runtime.CLAUDE_CODE, timeout_s=10)

    assert usable is True
    assert detail


def test_a_revoked_token_is_caught_despite_a_zero_exit(on_path) -> None:
    """`claude -p` prints the 401 and exits 0. This is the real observed output."""
    from ctswarm.capacity import probe_runtime

    _fake_cli(
        on_path,
        "claude",
        stdout="Failed to authenticate. API Error: 401 OAuth access token has been revoked.",
        exit_code=0,
    )

    usable, detail = probe_runtime(Runtime.CLAUDE_CODE, timeout_s=10)

    assert usable is False
    assert "revoked" in detail


def test_a_spent_subscription_is_caught(on_path) -> None:
    """Codex's real wording, verbatim, when the weekly allowance is gone."""
    from ctswarm.capacity import probe_runtime

    _fake_cli(
        on_path,
        "codex",
        stdout="ERROR: You've hit your usage limit. Upgrade to Pro or try again later.",
        exit_code=1,
    )

    usable, detail = probe_runtime(Runtime.CODEX, timeout_s=10)

    assert usable is False
    assert "usage limit" in detail


def test_a_missing_binary_reaches_no_verdict(on_path, monkeypatch) -> None:
    """"I could not ask" must not be recorded as "it refused".

    Both CLIs install to ~/.local/bin, which reaches PATH only through
    ~/.profile, so a probe run non-interactively finds neither on a host where
    both work. Returning False there marked two live subscriptions as rate
    limited and blocked the build.
    """
    from ctswarm.capacity import probe_runtime

    monkeypatch.setattr("pathlib.Path.home", lambda: on_path / "empty-home")

    usable, detail = probe_runtime(Runtime.CODEX, timeout_s=10)

    assert usable is None
    assert "not found" in detail


def test_a_cli_outside_path_is_still_found(on_path, monkeypatch) -> None:
    """~/.local/bin is where both CLIs actually live."""
    from ctswarm.capacity import probe_runtime

    home = on_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    _fake_cli(home / ".local" / "bin", "codex", stdout="OK", exit_code=0)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setenv("PATH", "/bin:/usr/bin")

    usable, _ = probe_runtime(Runtime.CODEX, timeout_s=10)

    assert usable is True


# --- a credentials file the CLI has blanked out -----------------------------
#
# When an OAuth refresh fails, Claude Code rewrites this file with empty token
# strings and expiresAt 0, keeping the harmless fields. That is a valid,
# non-empty JSON object describing no login, so the generic credential check
# reported a working subscription while every invocation returned
# "OAuth access token has been revoked".

_WIPED = {
    "claudeAiOauth": {
        "accessToken": "",
        "refreshToken": "",
        "expiresAt": 0,
        "subscriptionType": "max",
    }
}
_LIVE = {
    "claudeAiOauth": {
        "accessToken": "sk-ant-oat01-example",
        "refreshToken": "sk-ant-ort01-example",
        "expiresAt": 1789489539531,
        "subscriptionType": "max",
    }
}


def _write_claude(payload) -> None:
    path = Path.home() / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_blanked_credentials_file_is_not_a_login(monkeypatch) -> None:
    from ctswarm.capacity import _claude_login_present

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    _write_claude(_WIPED)

    assert _claude_login_present({}) is False


def test_a_real_token_is_still_a_login(monkeypatch) -> None:
    from ctswarm.capacity import _claude_login_present

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    _write_claude(_LIVE)

    assert _claude_login_present({}) is True


def test_an_unfamiliar_shape_is_not_treated_as_absent(monkeypatch) -> None:
    """A false negative here blocks every build on a host that works fine."""
    from ctswarm.capacity import _claude_login_present

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    _write_claude({"someOtherShape": {"token": "abc"}})

    assert _claude_login_present({}) is True


def test_doctor_warns_about_a_login_a_build_will_destroy(monkeypatch) -> None:
    from ctswarm.cli import _claude_login_is_the_fragile_kind

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    _write_claude(_LIVE)
    assert _claude_login_is_the_fragile_kind() is True

    # A long-lived token means the file is not what the harnesses use.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-headless")
    assert _claude_login_is_the_fragile_kind() is False
