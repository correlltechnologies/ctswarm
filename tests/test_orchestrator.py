"""Regression tests for build submission configuration."""

from __future__ import annotations

import time

from ctswarm.capacity import Runtime
from ctswarm.orchestrator import (
    HOSTED_ROLES,
    BuildRecord,
    BuildState,
    Orchestrator,
    hybrid_role_policy,
    runtime_model_overrides,
    update_record_from_execution,
)


def test_open_code_uses_router_virtual_models() -> None:
    models = runtime_model_overrides(Runtime.OPEN_CODE)

    assert models["default"] == "ctswarm/med"
    assert models["pm"] == "ctswarm/high"
    assert models["qa_synthesizer"] == "ctswarm/low"


def test_claude_code_overrides_open_code_container_environment() -> None:
    models = runtime_model_overrides(Runtime.CLAUDE_CODE)

    assert models == {"default": "sonnet", "qa_synthesizer": "haiku"}
    assert all(not model.startswith("ctswarm/") for model in models.values())


def test_codex_chatgpt_auth_overrides_open_code_environment(monkeypatch) -> None:
    monkeypatch.setenv("SWE_CODEX_AUTH_MODE", "chatgpt")
    models = runtime_model_overrides(Runtime.CODEX)

    assert models == {"default": "gpt-5.5"}
    assert all(not model.startswith("ctswarm/") for model in models.values())


def test_codex_api_key_auth_uses_codex_model(monkeypatch) -> None:
    monkeypatch.setenv("SWE_CODEX_AUTH_MODE", "api_key")

    assert runtime_model_overrides(Runtime.CODEX) == {
        "default": "gpt-5.3-codex"
    }


def test_codex_auto_auth_tracks_api_key_presence(monkeypatch) -> None:
    monkeypatch.setenv("SWE_CODEX_AUTH_MODE", "auto")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert runtime_model_overrides(Runtime.CODEX)["default"] == "gpt-5.5"

    monkeypatch.setenv("OPENAI_API_KEY", "present")
    assert (
        runtime_model_overrides(Runtime.CODEX)["default"] == "gpt-5.3-codex"
    )


def test_hybrid_policy_keeps_execution_local() -> None:
    providers, models = hybrid_role_policy(Runtime.CLAUDE_CODE)

    assert providers["default"] == "open_code"
    assert providers["pm"] == "claude_code"
    assert providers["verifier"] == "claude_code"
    assert "coder" not in providers
    assert models["default"] == "ctswarm/med"
    assert models["pm"] == "sonnet"
    assert set(providers) == {"default", *HOSTED_ROLES}


def test_hybrid_policy_survives_hosted_quota_exhaustion() -> None:
    providers, models = hybrid_role_policy(Runtime.OPEN_CODE)

    assert providers == {"default": "open_code"}
    assert models["pm"] == "ctswarm/high"
    assert models["default"] == "ctswarm/med"


def test_succeeded_execution_completes_polling() -> None:
    record = BuildRecord(
        build_id="build-test",
        goal="test",
        repo_url="https://example.invalid/repo",
        runtime=Runtime.OPEN_CODE,
        state=BuildState.EXECUTING,
    )

    update_record_from_execution(
        record,
        {
            "status": "succeeded",
            "result": {
                "success": True,
                "summary": "all work complete",
                "pr_url": "https://example.invalid/pr/1",
            },
        },
    )

    assert record.state is BuildState.COMPLETE
    assert record.phase_detail == "all work complete"
    assert record.pr_url.endswith("/pr/1")


def test_succeeded_execution_with_failed_result_fails_closed() -> None:
    record = BuildRecord(
        build_id="build-test",
        goal="test",
        repo_url="https://example.invalid/repo",
        runtime=Runtime.OPEN_CODE,
        state=BuildState.EXECUTING,
    )

    update_record_from_execution(
        record,
        {
            "status": "succeeded",
            "result": {
                "success": False,
                "summary": "verification failed",
                "error_message": "no verified integration branch",
            },
        },
    )

    assert record.state is BuildState.FAILED
    assert record.error == "no verified integration branch"


def test_succeeded_execution_without_explicit_success_fails_closed() -> None:
    record = BuildRecord(
        build_id="build-test",
        goal="test",
        repo_url="https://example.invalid/repo",
        runtime=Runtime.OPEN_CODE,
        state=BuildState.EXECUTING,
    )

    update_record_from_execution(
        record,
        {
            "status": "succeeded",
            "result": {"summary": "reasoner exited without a build verdict"},
        },
    )

    assert record.state is BuildState.FAILED
    assert record.error == "reasoner exited without a build verdict"


async def test_deadline_cancels_agentfield_execution(monkeypatch) -> None:
    orchestrator = Orchestrator()
    record = BuildRecord(
        build_id="build-timeout",
        goal="test",
        repo_url="https://example.invalid/repo",
        runtime=Runtime.OPEN_CODE,
        state=BuildState.EXECUTING,
        execution_id="exec-timeout",
    )
    cancellations: list[tuple[str, str]] = []

    async def cancel(current: BuildRecord, reason: str) -> bool:
        cancellations.append((current.execution_id, reason))
        return True

    monkeypatch.setattr(orchestrator, "cancel_execution", cancel)
    clock_calls = 0

    def fake_time() -> float:
        nonlocal clock_calls
        clock_calls += 1
        return 1_000_000.0 if clock_calls == 1 else 1_000_001.0

    monkeypatch.setattr(time, "time", fake_time)

    result = await orchestrator.run_until_done(
        record,
        max_hours=0.000001,
        poll_interval_s=0,
    )

    assert result.state is BuildState.FAILED
    assert cancellations == [
        ("exec-timeout", "exceeded the 1e-06h wall-clock limit")
    ]


async def test_no_progress_watchdog_cancels_stalled_execution(monkeypatch) -> None:
    orchestrator = Orchestrator(no_progress_timeout_s=60)
    record = BuildRecord(
        build_id="build-stalled",
        goal="test",
        repo_url="https://example.invalid/repo",
        runtime=Runtime.OPEN_CODE,
        state=BuildState.EXECUTING,
        execution_id="exec-stalled",
        last_progress_at=1_000.0,
    )
    cancellations: list[str] = []

    async def poll_unchanged(current: BuildRecord) -> BuildRecord:
        return current

    async def cancel(_current: BuildRecord, reason: str) -> bool:
        cancellations.append(reason)
        return True

    monkeypatch.setattr(orchestrator, "poll", poll_unchanged)
    monkeypatch.setattr(orchestrator, "cancel_execution", cancel)
    monkeypatch.setattr(time, "time", lambda: 1_061.0)

    result = await orchestrator.run_until_done(record, poll_interval_s=0)

    assert result.state is BuildState.FAILED
    assert result.phase_detail == "stalled execution cancelled"
    assert result.error == "no semantic build progress for 61s (limit 60s)"
    assert cancellations == [result.error]


def test_progress_fingerprint_ignores_heartbeat_only_updates() -> None:
    record = BuildRecord(
        build_id="build-progress",
        goal="test",
        repo_url="https://example.invalid/repo",
        runtime=Runtime.OPEN_CODE,
        state=BuildState.EXECUTING,
    )

    update_record_from_execution(
        record,
        {"status": "running", "phase": "coding", "updated_at": "first"},
    )
    first_progress_at = record.last_progress_at
    update_record_from_execution(
        record,
        {"status": "running", "phase": "coding", "updated_at": "second"},
    )

    assert record.last_progress_at == first_progress_at
