"""Regression tests for build submission configuration."""

from __future__ import annotations

import time

from ctswarm.capacity import Runtime
from ctswarm.orchestrator import (
    BuildRecord,
    BuildState,
    Orchestrator,
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
