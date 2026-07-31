"""Unified dashboard and AgentField trace normalization tests."""

from __future__ import annotations

from pathlib import Path

import httpx

from ctswarm.observability import AgentFieldTraceClient, _model_is_local


def test_ctswarm_policy_aliases_are_local_before_first_router_call() -> None:
    assert _model_is_local("ctswarm/med", {"open_code", "opencode"})
    assert _model_is_local("qwen3.5:9b", {"ollama"})
    assert not _model_is_local("sonnet", {"claude"})


def _response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


async def test_trace_reports_exact_models_harnesses_and_tasks() -> None:
    detail_calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/executions/exec-root":
            return _response({"execution_id": "exec-root", "run_id": "run-one", "status": "running"})
        if path == "/api/ui/v1/workflows/run-one/dag":
            assert request.url.params["mode"] == "lightweight"
            return _response(
                {
                    "root_workflow_id": "run-one",
                    "workflow_status": "running",
                    "workflow_name": "build",
                    "total_nodes": 3,
                    "max_depth": 2,
                    "timeline": [
                        {
                            "execution_id": "exec-root",
                            "reasoner_id": "build",
                            "status": "running",
                            "started_at": "2026-01-01T00:00:00Z",
                            "workflow_depth": 0,
                        },
                        {
                            "execution_id": "exec-coder",
                            "parent_execution_id": "exec-root",
                            "reasoner_id": "run_coder",
                            "status": "succeeded",
                            "duration_ms": 1234,
                            "workflow_depth": 1,
                        },
                        {
                            "execution_id": "exec-synth",
                            "parent_execution_id": "exec-root",
                            "reasoner_id": "run_qa_synthesizer",
                            "status": "running",
                            "workflow_depth": 2,
                        },
                    ],
                }
            )
        if path.endswith("/details"):
            execution_id = path.split("/")[-2]
            detail_calls[execution_id] = detail_calls.get(execution_id, 0) + 1
            if execution_id == "exec-root":
                return _response(
                    {
                        "execution_id": execution_id,
                        "reasoner_id": "build",
                        "input_data": {
                            "goal": "Build the thing",
                            "config": {
                                "runtime": "claude_code",
                                "models": {
                                    "default": "sonnet",
                                    "qa_synthesizer": "haiku",
                                },
                            },
                        },
                    }
                )
            if execution_id == "exec-coder":
                return _response(
                    {
                        "execution_id": execution_id,
                        "reasoner_id": "run_coder",
                        "input_data": {
                            "ai_provider": "claude",
                            "model": "opus",
                            "issue": {"title": "Repair encounter state machine"},
                        },
                    }
                )
            if execution_id == "exec-synth":
                return _response(
                    {
                        "execution_id": execution_id,
                        "reasoner_id": "run_qa_synthesizer",
                        "input_data": {"ai_provider": "claude"},
                    }
                )
        return httpx.Response(404, json={"path": path})

    client = AgentFieldTraceClient(
        "http://agentfield.invalid",
        transport=httpx.MockTransport(handler),
    )
    trace = await client.build_trace("exec-root")

    assert trace["workflow_id"] == "run-one"
    assert trace["harness"] == "Claude Code"
    assert trace["model_policy"] == {
        "default": "sonnet",
        "qa_synthesizer": "haiku",
    }
    coder = next(node for node in trace["timeline"] if node["execution_id"] == "exec-coder")
    assert coder["role"] == "Coder"
    assert coder["task"] == "Repair encounter state machine"
    assert coder["model"] == "opus"
    assert coder["model_source"] == "explicit"
    assert coder["harness"] == "Claude Code"
    assert coder["provider"] == "claude"
    synthesizer = next(
        node for node in trace["timeline"] if node["execution_id"] == "exec-synth"
    )
    assert synthesizer["model"] == "haiku"
    assert synthesizer["model_source"] == "policy:qa_synthesizer"
    assert trace["summary"]["statuses"] == {"running": 2, "succeeded": 1}
    assert trace["summary"]["models"] == {"sonnet": 1, "opus": 1, "haiku": 1}

    await client.build_trace("exec-root")
    assert detail_calls["exec-coder"] == 1
    assert detail_calls["exec-synth"] == 1


async def test_transient_detail_failure_is_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "not ready"})
        return _response(
            {
                "reasoner_id": "run_coder",
                "input_data": {
                    "model": "sonnet",
                    "issue": {"title": "Retry trace metadata"},
                },
            }
        )

    client = AgentFieldTraceClient(
        "http://agentfield.invalid",
        transport=httpx.MockTransport(handler),
    )

    assert await client._static_metadata("exec-retry") == {}
    assert (await client._static_metadata("exec-retry"))["task"] == "Retry trace metadata"
    assert calls == 2


async def test_metadata_cache_is_bounded_and_omits_full_inputs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        execution_id = request.url.path.split("/")[-2]
        return _response(
            {
                "reasoner_id": "run_coder",
                "input_data": {
                    "model": "sonnet",
                    "issue": {"title": execution_id},
                    "large_prompt": "not retained",
                },
            }
        )

    client = AgentFieldTraceClient(
        "http://agentfield.invalid",
        transport=httpx.MockTransport(handler),
        metadata_cache_size=2,
    )

    for execution_id in ("exec-one", "exec-two", "exec-three"):
        await client._static_metadata(execution_id)

    assert list(client._metadata_cache) == ["exec-two", "exec-three"]
    assert client._metadata_cache["exec-three"] == {
        "model": "sonnet",
        "provider": "",
        "runtime": "",
        "task": "exec-three",
    }


def test_compiled_dashboard_is_packaged_operator_surface() -> None:
    dashboard = Path(__file__).parents[1] / "ctswarm" / "static" / "dashboard"
    html = (dashboard / "index.html").read_text(encoding="utf-8")

    assert "ctswarm mission control" in html.lower()
    assert "/assets/" in html
    assert any((dashboard / "assets").glob("*.js"))
    assert any((dashboard / "assets").glob("*.css"))
