"""Regression tests for build submission configuration."""

from __future__ import annotations

import time

from ctswarm.capacity import Runtime
from ctswarm.ledger import Ledger
from ctswarm.orchestrator import (
    HOSTED_ROLES,
    BuildRecord,
    BuildState,
    Orchestrator,
    hybrid_role_policy,
    production_delivery_context,
    runtime_model_overrides,
    update_record_from_execution,
)


def test_production_delivery_context_requires_testable_acceptance() -> None:
    context = production_delivery_context("Ship a readable dashboard")

    assert "Ship a readable dashboard" in context
    assert "requirement-to-evidence matrix" in context
    assert "production build" in context
    assert "browser console errors" in context
    assert "Report success only after all acceptance criteria pass" in context


async def test_submit_sends_production_contract_to_planner(monkeypatch, tmp_path) -> None:
    captured: dict = {}

    class Response:
        status_code = 202
        text = ""

        def json(self):
            return {"execution_id": "exec-production"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, json):
            captured.update({"url": url, "json": json})
            return Response()

    # This test is about the contract sent to the planner, not about capacity.
    # Subscriptions-only is the default mode and refuses to submit when no
    # harness has headroom, which on a host with no logins is every time. Pin
    # hybrid so the assertion below is measuring what it claims to measure.
    monkeypatch.setenv("CTSWARM_EXECUTION_MODE", "hybrid")
    orchestrator = Orchestrator(ledger=Ledger(tmp_path / "ledger.db"))
    monkeypatch.setattr(orchestrator.capacity, "select", lambda **_kwargs: (Runtime.OPEN_CODE, "test"))
    monkeypatch.setattr("ctswarm.orchestrator.httpx.AsyncClient", Client)

    result = await orchestrator.submit(
        goal="Ship the complete app",
        repo_url="https://bitbucket.org/example/repo.git",
        scm_provider="bitbucket",
        source_branch="develop",
        create_pull_request=True,
        mcp_context="Inherited MCP context:\n- vercel via Claude Code (remote)",
    )

    assert result.execution_id == "exec-production"
    contract = captured["json"]["input"]["additional_context"]
    assert "Ship the complete app" in contract
    assert "Acceptance evidence required before success" in contract
    assert "Repository provider: bitbucket" in contract
    assert "Starting branch: develop" in contract
    assert "vercel via Claude Code" in contract
    assert captured["json"]["input"]["config"]["max_verify_fix_cycles"] == 3
    assert captured["json"]["input"]["config"]["enable_github_pr"] is False
    assert captured["json"]["input"]["config"]["github_pr_base"] == "develop"


async def test_submit_uses_explicit_routing_snapshot(monkeypatch, tmp_path) -> None:
    captured: dict = {}

    class Response:
        status_code = 202
        text = ""

        def json(self):
            return {"execution_id": "exec-frozen"}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, json):
            captured.update(json)
            return Response()

    ledger = Ledger(tmp_path / "ledger.db")
    # Routing a lane to a local model is only legal on a hybrid host.
    monkeypatch.setenv("CTSWARM_EXECUTION_MODE", "hybrid")
    orchestrator = Orchestrator(ledger=ledger)
    monkeypatch.setattr(
        orchestrator.capacity,
        "select",
        lambda **_kwargs: (Runtime.CLAUDE_CODE, "test"),
    )
    monkeypatch.setattr("ctswarm.orchestrator.httpx.AsyncClient", Client)

    await orchestrator.submit(
        goal="Use frozen routing",
        repo_url="https://example.invalid/repo.git",
        routing_policy={
            "planning": {"target": "codex", "model": ""},
            "implementation": {"target": "ollama", "model": "qwen3.5:9b"},
            "review": {"target": "codex", "model": ""},
            "maintenance": {"target": "ollama", "model": "granite4.1:8b"},
        },
    )

    config = captured["input"]["config"]
    assert config["providers"]["pm"] == "codex"
    assert config["models"]["pm"] == "gpt-5.5"
    assert config["providers"]["coder"] == "open_code"
    assert config["models"]["coder"] == "ctswarm/ollama:qwen3.5:9b"


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

    # The API-key model only exists on a hybrid host. Subscriptions-only mode
    # has no key by definition, which the next test covers.
    assert runtime_model_overrides(Runtime.CODEX, subscriptions_only=False) == {
        "default": "gpt-5.3-codex"
    }


def test_codex_auto_auth_tracks_api_key_presence(monkeypatch) -> None:
    monkeypatch.setenv("SWE_CODEX_AUTH_MODE", "auto")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert (
        runtime_model_overrides(Runtime.CODEX, subscriptions_only=False)["default"]
        == "gpt-5.5"
    )

    monkeypatch.setenv("OPENAI_API_KEY", "present")
    assert (
        runtime_model_overrides(Runtime.CODEX, subscriptions_only=False)["default"]
        == "gpt-5.3-codex"
    )


def test_subscription_mode_ignores_a_stray_api_key(monkeypatch) -> None:
    """A key left in the environment must not silently start metered spend.

    Subscriptions-only mode promises the work is billed to a subscription. The
    API-key-only `-codex` model would break that promise, and would also fail
    outright on a host whose codex login is a ChatGPT account.
    """
    monkeypatch.setenv("SWE_CODEX_AUTH_MODE", "api_key")
    monkeypatch.setenv("OPENAI_API_KEY", "present")

    assert runtime_model_overrides(Runtime.CODEX, subscriptions_only=True) == {
        "default": "gpt-5.5"
    }


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


async def test_cancel_execution_stops_the_complete_workflow_tree(
    monkeypatch, tmp_path
) -> None:
    requests: list[tuple[str, str, dict | None]] = []

    class Response:
        def __init__(self, status_code: int, payload: dict | None = None) -> None:
            self.status_code = status_code
            self.payload = payload or {}

        def json(self) -> dict:
            return self.payload

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url):
            requests.append(("GET", url, None))
            return Response(200, {"run_id": "run-complete-tree"})

        async def post(self, url, json):
            requests.append(("POST", url, json))
            return Response(200)

    orchestrator = Orchestrator(ledger=Ledger(tmp_path / "ledger.db"))
    record = BuildRecord(
        build_id="build-tree-cancel",
        goal="test",
        repo_url="https://example.invalid/repo",
        runtime=Runtime.OPEN_CODE,
        state=BuildState.EXECUTING,
        execution_id="exec-root",
    )
    monkeypatch.setattr("ctswarm.orchestrator.httpx.AsyncClient", Client)

    assert await orchestrator.cancel_execution(record, "owner requested stop")
    assert requests == [
        (
            "GET",
            "http://localhost:18080/api/v1/executions/exec-root",
            None,
        ),
        (
            "POST",
            "http://localhost:18080/api/v1/workflows/run-complete-tree/cancel-tree",
            {"reason": "owner requested stop"},
        ),
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


def test_child_status_transition_counts_as_semantic_progress(monkeypatch) -> None:
    record = BuildRecord(
        build_id="build-child-progress",
        goal="test",
        repo_url="https://example.invalid/repo",
        runtime=Runtime.OPEN_CODE,
        state=BuildState.EXECUTING,
    )
    clock = iter((1_000.0, 1_100.0))
    monkeypatch.setattr(time, "time", lambda: next(clock))

    update_record_from_execution(
        record,
        {
            "status": "running",
            "_workflow_progress": {
                "nodes": [{"execution_id": "child", "status": "running"}]
            },
        },
    )
    update_record_from_execution(
        record,
        {
            "status": "running",
            "_workflow_progress": {
                "nodes": [{"execution_id": "child", "status": "succeeded"}]
            },
        },
    )

    assert record.last_progress_at == 1_100.0
