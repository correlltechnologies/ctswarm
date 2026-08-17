"""Subscriptions-only mode: what it must guarantee, not merely what it sets.

The mode makes three promises. Each has a test here, because each fails silently
if it regresses:

1. No local model path is registered or selectable.
2. No API key is accepted as a credential, so nothing starts metered spend.
3. Every SWE-AF role is assigned to a CLI harness, with review on a different
   vendor from implementation.
"""

from __future__ import annotations

import itertools

import pytest

from ctswarm.backends import build_backends
from ctswarm.capacity import CapacityManager, Runtime
from ctswarm.execution_mode import (
    DEFAULT_MODE,
    HYBRID,
    SUBSCRIPTION_ONLY,
    ExecutionModeError,
    env_pinned,
    load_mode,
    save_mode,
    subscription_only,
)
from ctswarm.ledger import Ledger
from ctswarm.orchestrator import (
    SUBSCRIPTION_LANE_RUNTIMES,
    Orchestrator,
    subscription_role_policy,
)
from ctswarm.platform_detect import Accelerator, HostProfile
from ctswarm.routing_config import (
    LANE_ROLES,
    RoutingPolicyError,
    allowed_targets,
    load_routing_policy,
    normalize_routing_policy,
)


@pytest.fixture
def no_claude_login(monkeypatch):
    """Pretend this host has no Claude subscription login.

    Needed because the credential check consults the macOS Keychain, so a
    developer running these tests on a logged-in machine would otherwise see a
    real login and the "no harness available" cases could never be reached.
    """
    monkeypatch.setattr(
        "ctswarm.capacity._KEYCHAIN_CACHE", {"claude": (float("inf"), False)}
    )
    monkeypatch.setattr("ctswarm.capacity._KEYCHAIN_TTL_S", float("inf"))
    return None


def _host() -> HostProfile:
    return HostProfile(
        os_name="Linux",
        arch="aarch64",
        accelerator=Accelerator.CPU,
        accel_memory_gb=0,
        system_memory_gb=4,
    )


# -- the setting itself ---------------------------------------------------


def test_default_is_subscription_only() -> None:
    """A host with no configuration must land on the mode that works anywhere."""
    assert DEFAULT_MODE == SUBSCRIPTION_ONLY


def test_mode_round_trips_through_the_ledger(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CTSWARM_EXECUTION_MODE", raising=False)
    ledger = Ledger(tmp_path / "ledger.db")

    assert load_mode(ledger) == SUBSCRIPTION_ONLY
    save_mode(ledger, HYBRID, changed_by="tester")
    assert load_mode(ledger) == HYBRID

    audit = ledger.events(kind="execution_mode_updated")
    assert audit and "tester" in audit[-1]["detail"]


def test_environment_overrides_a_stored_mode(tmp_path, monkeypatch) -> None:
    """The Pi pins the mode before its database exists; that must win."""
    ledger = Ledger(tmp_path / "ledger.db")
    save_mode(ledger, HYBRID)

    monkeypatch.setenv("CTSWARM_EXECUTION_MODE", "subscription_only")
    assert load_mode(ledger) == SUBSCRIPTION_ONLY
    assert env_pinned() is True


def test_a_typo_in_the_environment_falls_back_to_the_safe_mode(
    tmp_path, monkeypatch
) -> None:
    """A misspelling must not quietly enable local models on a host with none."""
    ledger = Ledger(tmp_path / "ledger.db")
    monkeypatch.setenv("CTSWARM_EXECUTION_MODE", "hybrd")
    assert load_mode(ledger) == SUBSCRIPTION_ONLY


def test_unknown_mode_is_rejected_on_save(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.db")
    with pytest.raises(ExecutionModeError):
        save_mode(ledger, "local_only")


# -- promise 1: no local model path ---------------------------------------


def test_no_backends_are_registered() -> None:
    assert build_backends(_host(), {"CTSWARM_OLLAMA_HOST": "http://x.invalid"},
                          subscriptions_only=True) == {}


def test_hybrid_still_registers_backends() -> None:
    backends = build_backends(
        _host(),
        {"CTSWARM_OLLAMA_HOST": "http://x.invalid"},
        subscriptions_only=False,
    )
    assert "ollama" in backends


def test_local_targets_are_not_selectable() -> None:
    assert allowed_targets(subscriptions_only=True) == {
        "auto",
        "claude_code",
        "codex",
    }
    assert "ollama" in allowed_targets(subscriptions_only=False)


def test_assigning_a_local_model_is_refused_with_a_usable_message() -> None:
    policy = {"implementation": {"target": "ollama", "model": "qwen3.5:9b"}}
    with pytest.raises(RoutingPolicyError) as excinfo:
        normalize_routing_policy(policy, subscriptions_only=True)
    # The operator needs to know the way out, not just that it failed.
    assert "hybrid mode" in str(excinfo.value)


def test_a_policy_written_on_the_gpu_box_degrades_rather_than_exploding(
    tmp_path,
) -> None:
    """Moving the database to a Pi must not brick every future build."""
    ledger = Ledger(tmp_path / "ledger.db")
    ledger.set_setting(
        "routing_policy_v1",
        {
            "planning": {"target": "codex", "model": ""},
            "implementation": {"target": "ollama", "model": "qwen3.5:9b"},
            "review": {"target": "claude_code", "model": ""},
            "maintenance": {"target": "ollama", "model": "granite4.1:8b"},
        },
    )

    loaded = load_routing_policy(ledger, subscriptions_only=True)

    # Reachable assignments survive; unreachable ones fall back to auto.
    assert loaded["planning"]["target"] == "codex"
    assert loaded["review"]["target"] == "claude_code"
    assert loaded["implementation"]["target"] == "auto"
    assert loaded["maintenance"]["target"] == "auto"


# -- promise 2: no API key is a credential --------------------------------


def test_an_api_key_is_not_a_claude_credential(
    tmp_path, monkeypatch, no_claude_login
) -> None:
    monkeypatch.setenv("CTSWARM_EXECUTION_MODE", "subscription_only")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-be-ignored")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        "pathlib.Path.home", lambda: tmp_path  # no credentials file exists here
    )

    manager = CapacityManager(
        ledger=Ledger(tmp_path / "ledger.db"),
        env={
            "CTSWARM_EXECUTION_MODE": "subscription_only",
            "ANTHROPIC_API_KEY": "sk-ant-should-be-ignored",
        },
    )
    assert manager.configured(Runtime.CLAUDE_CODE) is False


def test_an_api_key_is_a_claude_credential_in_hybrid_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    manager = CapacityManager(
        ledger=Ledger(tmp_path / "ledger.db"),
        env={"CTSWARM_EXECUTION_MODE": "hybrid", "ANTHROPIC_API_KEY": "sk-ant-x"},
    )
    assert manager.configured(Runtime.CLAUDE_CODE) is True


def test_a_subscription_login_file_counts_as_a_credential(tmp_path, monkeypatch) -> None:
    """The docstring always claimed real artifacts were checked; now they are."""
    credentials = tmp_path / ".claude" / ".credentials.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    manager = CapacityManager(
        ledger=Ledger(tmp_path / "ledger.db"),
        env={"CTSWARM_EXECUTION_MODE": "subscription_only"},
    )
    assert manager.configured(Runtime.CLAUDE_CODE) is True


def test_an_empty_placeholder_is_not_a_login(tmp_path, monkeypatch, no_claude_login) -> None:
    """bootstrap.sh writes `{}` here so the container bind mount stays a file.

    Treating that as a credential would launch a build against a harness that
    refuses on its first call, which is the failure this whole mode exists to
    make impossible.
    """
    credentials = tmp_path / ".claude" / ".credentials.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("{}", encoding="utf-8")
    codex = tmp_path / ".codex"
    codex.mkdir()
    (codex / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    manager = CapacityManager(
        ledger=Ledger(tmp_path / "ledger.db"),
        env={"CTSWARM_EXECUTION_MODE": "subscription_only"},
    )
    assert manager.configured(Runtime.CLAUDE_CODE) is False
    assert manager.configured(Runtime.CODEX) is False

    credentials.write_text('{"accessToken": "real"}', encoding="utf-8")
    assert manager.configured(Runtime.CLAUDE_CODE) is True


def test_a_credentials_directory_is_not_a_login(tmp_path, monkeypatch, no_claude_login) -> None:
    """Docker creates a directory when a bind-mount source is missing."""
    credentials = tmp_path / ".claude" / ".credentials.json"
    credentials.mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    manager = CapacityManager(
        ledger=Ledger(tmp_path / "ledger.db"),
        env={"CTSWARM_EXECUTION_MODE": "subscription_only"},
    )
    assert manager.configured(Runtime.CLAUDE_CODE) is False


def test_local_runtime_reports_a_truthful_reason(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    manager = CapacityManager(
        ledger=Ledger(tmp_path / "ledger.db"),
        env={"CTSWARM_EXECUTION_MODE": "subscription_only"},
    )
    head = manager.headroom(Runtime.OPEN_CODE)

    assert head.available is False
    # "no credentials configured" would send debugging in the wrong direction.
    assert "subscriptions-only" in head.reason


# -- promise 3: every role runs on a harness, review stays independent -----


def test_every_role_is_assigned() -> None:
    providers, models, _ = subscription_role_policy()
    every_role = set(itertools.chain.from_iterable(LANE_ROLES.values()))

    assert every_role <= set(providers), "a role with no provider inherits the base runtime"
    assert every_role <= set(models)
    assert all(value in {"claude_code", "codex"} for value in providers.values())
    assert all(not model.startswith("ctswarm/") for model in models.values())


def test_review_runs_on_a_different_vendor_from_implementation() -> None:
    """With no local committee, cross-harness review is the only independence left."""
    providers, _, _ = subscription_role_policy()

    assert providers["coder"] != providers["code_reviewer"]
    assert providers["coder"] != providers["verifier"]
    assert SUBSCRIPTION_LANE_RUNTIMES["implementation"] is not (
        SUBSCRIPTION_LANE_RUNTIMES["review"]
    )


def test_base_runtime_follows_the_implementation_lane() -> None:
    """`default` is what any unmapped role inherits, so it must not be open_code."""
    providers, _, base = subscription_role_policy()

    assert base.value == providers["default"]
    assert base is not Runtime.OPEN_CODE


def test_a_single_available_harness_collapses_every_lane_onto_it() -> None:
    providers, _, base = subscription_role_policy(available={Runtime.CODEX})

    assert set(providers.values()) == {"codex"}
    assert base is Runtime.CODEX


async def test_submit_refuses_when_no_harness_has_headroom(
    tmp_path, monkeypatch, no_claude_login
) -> None:
    """Launching anyway would burn a full agent timeout per invocation."""
    monkeypatch.setenv("CTSWARM_EXECUTION_MODE", "subscription_only")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    def _explode(*_args, **_kwargs):
        raise AssertionError("submit must not reach the control plane")

    monkeypatch.setattr("ctswarm.orchestrator.httpx.AsyncClient", _explode)

    ledger = Ledger(tmp_path / "ledger.db")
    orchestrator = Orchestrator(ledger=ledger)
    record = await orchestrator.submit(
        goal="anything", repo_url="https://example.invalid/repo.git"
    )

    assert record.state.value == "blocked"
    assert "no subscription harness" in record.error
    assert ledger.events(kind="build_blocked")


async def test_single_harness_degradation_is_recorded(tmp_path, monkeypatch) -> None:
    """A silent loss of independent review is exactly what must not happen."""
    monkeypatch.setenv("CTSWARM_EXECUTION_MODE", "subscription_only")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    class _Response:
        status_code = 202
        text = ""

        def json(self) -> dict:
            return {"execution_id": "exec-1"}

    class _Client:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, json):
            return _Response()

    monkeypatch.setattr("ctswarm.orchestrator.httpx.AsyncClient", _Client)

    ledger = Ledger(tmp_path / "ledger.db")
    orchestrator = Orchestrator(ledger=ledger)
    monkeypatch.setattr(
        orchestrator.capacity,
        "headroom",
        lambda runtime: type(
            "H", (), {"available": runtime is Runtime.CLAUDE_CODE, "reason": "test",
                      "fraction_remaining": 1.0}
        )(),
    )

    await orchestrator.submit(
        goal="ship it", repo_url="https://example.invalid/repo.git"
    )

    degraded = ledger.events(kind="build_degraded")
    assert degraded, "collapsing onto one harness must be recorded, not assumed"
    assert "independent review" in degraded[-1]["detail"]


def test_subscription_only_predicate_matches_the_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CTSWARM_EXECUTION_MODE", raising=False)
    ledger = Ledger(tmp_path / "ledger.db")

    assert subscription_only(ledger) is True
    save_mode(ledger, HYBRID)
    assert subscription_only(ledger) is False
