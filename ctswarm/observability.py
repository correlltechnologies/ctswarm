"""Unified AgentField observability for the ctswarm operator console.

AgentField's native dashboard is useful, but it does not know about ctswarm
build IDs, runtime selection, approval state, or the model policy submitted by
the scheduler.  This module joins those layers into one small, stable view
model that the ctswarm dashboard can render without making the browser talk to
multiple services.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter, OrderedDict
from typing import Any

import httpx

from .ledger import Ledger
from .router.policy import RoutingTable

LOCAL_BACKENDS = {"ollama", "mlx", "lmstudio"}


def _model_is_local(name: str, backends: set[str] | list[str]) -> bool:
    """Classify ctswarm aliases as local before their first routed call."""
    return name.startswith("ctswarm/") or bool(backends) and all(
        backend in LOCAL_BACKENDS for backend in backends
    )


ROLE_LABELS = {
    "build": "Build coordinator",
    "plan": "Planning coordinator",
    "execute": "Execution coordinator",
    "run_git_init": "Repository setup",
    "run_product_manager": "Product manager",
    "run_architect": "Architect",
    "run_tech_lead": "Tech lead",
    "run_sprint_planner": "Sprint planner",
    "run_issue_writer": "Issue writer",
    "run_coder": "Coder",
    "run_qa": "QA",
    "run_code_reviewer": "Code reviewer",
    "run_qa_synthesizer": "QA synthesizer",
    "run_issue_advisor": "Issue advisor",
    "run_replanner": "Replanner",
    "run_merger": "Merger",
    "run_integration_tester": "Integration tester",
    "run_verifier": "Acceptance verifier",
    "generate_fix_issues": "Fix planner",
    "run_repo_finalizer": "Repository finalizer",
    "run_github_pr": "Pull request publisher",
}

ROLE_MODEL_KEYS = {
    "run_product_manager": "pm",
    "run_architect": "architect",
    "run_tech_lead": "tech_lead",
    "run_sprint_planner": "sprint_planner",
    "run_issue_writer": "issue_writer",
    "run_coder": "coder",
    "run_qa": "qa",
    "run_code_reviewer": "code_reviewer",
    "run_qa_synthesizer": "qa_synthesizer",
    "run_issue_advisor": "issue_advisor",
    "run_replanner": "replan",
    "run_merger": "merger",
    "run_integration_tester": "integration_tester",
    "run_verifier": "verifier",
    "generate_fix_issues": "verifier",
    "run_repo_finalizer": "git",
    "run_git_init": "git",
    "run_github_pr": "git",
}

HARNESS_LABELS = {
    "claude_code": "Claude Code",
    "codex": "Codex",
    "open_code": "OpenCode",
}

PHASES = {
    "build": "Coordinate",
    "run_git_init": "Prepare",
    "plan": "Plan",
    "run_product_manager": "Plan",
    "run_architect": "Plan",
    "run_tech_lead": "Review",
    "run_sprint_planner": "Plan",
    "run_issue_writer": "Plan",
    "execute": "Build",
    "run_coder": "Build",
    "run_qa": "Test",
    "run_code_reviewer": "Review",
    "run_qa_synthesizer": "Review",
    "run_issue_advisor": "Repair",
    "run_replanner": "Repair",
    "run_merger": "Integrate",
    "run_integration_tester": "Test",
    "run_verifier": "Verify",
    "generate_fix_issues": "Repair",
    "run_repo_finalizer": "Finalize",
    "run_github_pr": "Publish",
}


def harness_label(runtime: str) -> str:
    """Human-readable harness name without hiding the exact runtime id."""
    return HARNESS_LABELS.get(runtime, runtime or "Unknown")


def _task_label(reasoner_id: str, input_data: dict[str, Any]) -> str:
    issue = input_data.get("issue")
    if isinstance(issue, dict):
        return str(issue.get("title") or issue.get("name") or "")
    for key in ("criterion", "goal", "feedback"):
        value = input_data.get(key)
        if isinstance(value, str) and value.strip():
            compact = " ".join(value.split())
            return compact[:140] + ("…" if len(compact) > 140 else "")
    return ROLE_LABELS.get(reasoner_id, reasoner_id.replace("_", " ").title())


def _model_for(
    reasoner_id: str,
    explicit_model: str,
    model_policy: dict[str, str],
) -> tuple[str, str]:
    if explicit_model:
        return explicit_model, "explicit"
    role_key = ROLE_MODEL_KEYS.get(reasoner_id)
    if role_key and model_policy.get(role_key):
        return model_policy[role_key], f"policy:{role_key}"
    if model_policy.get("default"):
        return model_policy["default"], "policy:default"
    return "Unknown", "unreported"


class AgentFieldTraceClient:
    """Fetch and normalize AgentField execution data.

    Input metadata is immutable once an execution is created, so it is cached
    by execution ID. Live status and duration always come from the fresh DAG.
    """

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        metadata_cache_size: int = 512,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.metadata_cache_size = max(1, metadata_cache_size)
        self._metadata_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _remember_metadata(
        self, execution_id: str, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        self._metadata_cache[execution_id] = metadata
        self._metadata_cache.move_to_end(execution_id)
        while len(self._metadata_cache) > self.metadata_cache_size:
            self._metadata_cache.popitem(last=False)
        return metadata

    async def _get(self, path: str) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=20.0,
            transport=self.transport,
        ) as client:
            response = await client.get(path)
            response.raise_for_status()
            payload = response.json()
        return payload if isinstance(payload, dict) else {}

    async def execution_details(self, execution_id: str) -> dict[str, Any]:
        return await self._get(f"/api/ui/v1/executions/{execution_id}/details")

    async def _static_metadata(self, execution_id: str) -> dict[str, Any]:
        cached = self._metadata_cache.get(execution_id)
        if cached is not None:
            self._metadata_cache.move_to_end(execution_id)
            return cached
        try:
            details = await self.execution_details(execution_id)
        except (httpx.HTTPError, ValueError):
            return {}
        input_data = details.get("input_data")
        if not isinstance(input_data, dict):
            input_data = {}
        metadata = {
            "model": str(input_data.get("model") or ""),
            "provider": str(input_data.get("ai_provider") or ""),
            "runtime": str(input_data.get("runtime") or ""),
            "task": _task_label(str(details.get("reasoner_id") or ""), input_data),
        }
        return self._remember_metadata(execution_id, metadata)

    async def build_trace(self, root_execution_id: str) -> dict[str, Any]:
        root = await self._get(f"/api/v1/executions/{root_execution_id}")
        workflow_id = str(root.get("run_id") or root.get("workflow_id") or "")
        if not workflow_id:
            raise ValueError("AgentField execution did not report a workflow id")

        dag, root_details = await asyncio.gather(
            self._get(
                f"/api/ui/v1/workflows/{workflow_id}/dag?mode=lightweight"
            ),
            self.execution_details(root_execution_id),
        )
        root_input = root_details.get("input_data")
        if not isinstance(root_input, dict):
            root_input = {}
        self._remember_metadata(
            root_execution_id,
            {
                "model": str(root_input.get("model") or ""),
                "provider": str(root_input.get("ai_provider") or ""),
                "runtime": str(root_input.get("runtime") or ""),
                "task": _task_label(
                    str(root_details.get("reasoner_id") or "build"), root_input
                ),
            },
        )
        config = root_input.get("config")
        if not isinstance(config, dict):
            config = {}
        model_policy = config.get("models")
        if not isinstance(model_policy, dict):
            model_policy = {}
        model_policy = {
            str(key): str(value)
            for key, value in model_policy.items()
            if isinstance(value, (str, int, float))
        }
        provider_policy = config.get("providers")
        if not isinstance(provider_policy, dict):
            provider_policy = {}
        root_runtime = str(
            config.get("runtime") or root_input.get("runtime") or "unknown"
        )

        raw_timeline = dag.get("timeline")
        if not isinstance(raw_timeline, list):
            raw_timeline = []
        semaphore = asyncio.Semaphore(12)

        async def enrich(raw: Any) -> dict[str, Any]:
            node = raw if isinstance(raw, dict) else {}
            execution_id = str(node.get("execution_id") or "")
            async with semaphore:
                metadata = (
                    await self._static_metadata(execution_id)
                    if execution_id
                    else {"model": "", "provider": "", "runtime": "", "task": ""}
                )
            reasoner_id = str(node.get("reasoner_id") or "unknown")
            model, model_source = _model_for(
                reasoner_id, str(metadata.get("model") or ""), model_policy
            )
            provider = str(metadata.get("provider") or "")
            provider_runtime = {
                "claude": "claude_code",
                "opencode": "open_code",
                "codex": "codex",
            }.get(provider, "")
            runtime = str(metadata.get("runtime") or provider_runtime or root_runtime)
            return {
                "execution_id": execution_id,
                "parent_execution_id": str(
                    node.get("parent_execution_id") or ""
                ),
                "reasoner_id": reasoner_id,
                "role": ROLE_LABELS.get(
                    reasoner_id, reasoner_id.replace("_", " ").title()
                ),
                "phase": PHASES.get(reasoner_id, "Other"),
                "task": str(
                    metadata.get("task")
                    or ROLE_LABELS.get(reasoner_id, reasoner_id)
                ),
                "status": str(node.get("status") or "unknown"),
                "started_at": node.get("started_at"),
                "completed_at": node.get("completed_at"),
                "duration_ms": node.get("duration_ms"),
                "depth": int(node.get("workflow_depth") or 0),
                "model": model,
                "model_source": model_source,
                "runtime": runtime,
                "harness": harness_label(runtime),
                "provider": provider or (
                    "claude" if runtime == "claude_code" else runtime
                ),
            }

        timeline = list(await asyncio.gather(*(enrich(node) for node in raw_timeline)))
        status_counts = Counter(node["status"] for node in timeline)
        role_counts = Counter(node["role"] for node in timeline)
        model_counts = Counter(node["model"] for node in timeline)
        harness_counts = Counter(node["harness"] for node in timeline)
        return {
            "execution_id": root_execution_id,
            "workflow_id": workflow_id,
            "status": str(dag.get("workflow_status") or root.get("status") or ""),
            "name": str(dag.get("workflow_name") or "build"),
            "total_nodes": int(dag.get("total_nodes") or len(timeline)),
            "max_depth": int(dag.get("max_depth") or 0),
            "runtime": root_runtime,
            "harness": harness_label(root_runtime),
            "model_policy": model_policy,
            "provider_policy": provider_policy,
            "summary": {
                "statuses": dict(status_counts),
                "roles": dict(role_counts),
                "models": dict(model_counts),
                "harnesses": dict(harness_counts),
            },
            "timeline": timeline,
        }


async def model_overview(
    *,
    builds: list[dict[str, Any]],
    trace_client: AgentFieldTraceClient,
    ledger: Ledger,
    routing_path: str,
    capacity: dict[str, Any],
    routes: dict[str, Any] | None = None,
    window_hours: float = 168.0,
) -> dict[str, Any]:
    """Fleet-wide model, runtime, role, quality, and cost telemetry."""
    recent = [build for build in builds if build.get("execution_id")][:25]
    semaphore = asyncio.Semaphore(4)

    async def fetch(build: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        async with semaphore:
            try:
                trace = await trace_client.build_trace(str(build["execution_id"]))
            except (httpx.HTTPError, ValueError):
                trace = None
        return build, trace

    traces = await asyncio.gather(*(fetch(build) for build in recent))
    benchmarks = {
        score.model_ref: score.to_dict()
        for score in RoutingTable.load(routing_path).all()
    }
    usage_rows = ledger.model_usage(window_hours * 3600.0)
    window_started_at = time.time() - window_hours * 3600.0
    routing_rejections = sum(
        1
        for event in ledger.events(kind="route_no_candidate")
        if float(event.get("ts") or 0) >= window_started_at
    )

    models: dict[str, dict[str, Any]] = {}
    nodes: dict[str, dict[str, Any]] = {}
    edges: Counter[tuple[str, str]] = Counter()

    def node(node_id: str, label: str, kind: str, local: bool = False) -> None:
        nodes[node_id] = {
            "id": node_id,
            "label": label,
            "kind": kind,
            "local": local,
        }

    def edge(source: str, target: str, value: int = 1) -> None:
        edges[(source, target)] += value

    def model_record(name: str) -> dict[str, Any]:
        return models.setdefault(
            name,
            {
                "name": name,
                "executions": 0,
                "active": 0,
                "live_jobs": 0,
                "succeeded": 0,
                "failed": 0,
                "duration_ms": 0.0,
                "duration_samples": 0,
                "calls": 0,
                "call_successes": 0,
                "tokens": 0,
                "cost_usd": 0.0,
                "latency_weighted_ms": 0.0,
                "roles": Counter(),
                "harnesses": set(),
                "providers": set(),
                "backends": set(),
                "builds": set(),
            },
        )

    execution_count = active_count = failed_count = local_active_count = 0
    execution_roles: Counter[str] = Counter()
    for build, trace in traces:
        if not trace:
            continue
        build_id = str(build.get("build_id") or "")
        for item in trace.get("timeline", []):
            model_name = str(item.get("model") or "Unknown")
            record = model_record(model_name)
            status = str(item.get("status") or "unknown").lower()
            is_active = status in {"running", "pending", "queued"}
            is_live_model_job = is_active and str(item.get("reasoner_id") or "") not in {
                "build",
                "plan",
                "execute",
            }
            duration = item.get("duration_ms")
            record["executions"] += 1
            record["active"] += int(is_active)
            record["succeeded"] += int(status in {"succeeded", "completed", "success"})
            record["failed"] += int(status in {"failed", "error", "cancelled", "canceled"})
            if isinstance(duration, (int, float)):
                record["duration_ms"] += float(duration)
                record["duration_samples"] += 1
            role = str(item.get("role") or "Unknown role")
            execution_roles[role] += 1
            harness = str(item.get("harness") or "Unknown harness")
            provider = str(item.get("provider") or "")
            record["roles"][role] += 1
            record["harnesses"].add(harness)
            if provider:
                record["providers"].add(provider)
                record["backends"].add(provider)
            if build_id:
                record["builds"].add(build_id)

            # Completed inference calls remain the source of truth for usage,
            # latency, and the routing graph. For the live operator view only,
            # resolve active virtual-tier executions through the current policy
            # so an in-flight local job appears under its real model immediately
            # instead of waiting for the router response to finish.
            if is_live_model_job:
                live_record = record
                live_backend = ""
                if model_name.startswith("ctswarm/"):
                    live_route = (routes or {}).get(model_name, {})
                    live_model = str(live_route.get("model") or "")
                    live_backend = str(live_route.get("backend") or "")
                    if live_model:
                        live_record = model_record(live_model)
                live_record["live_jobs"] += 1
                if live_record is not record:
                    live_record["roles"][role] += 1
                    live_record["harnesses"].add(harness)
                    if build_id:
                        live_record["builds"].add(build_id)
                if live_backend:
                    live_record["backends"].add(live_backend)
                local_active_count += int(
                    _model_is_local(str(live_record["name"]), live_record["backends"])
                )

            execution_count += 1
            active_count += int(is_active)
            failed_count += int(status in {"failed", "error", "cancelled", "canceled"})
            # Agent execution counts and inference-call counts are different
            # units. Keep execution relationships out of the concrete routing
            # Sankey; they are reported separately in the role chart/table.

    total_calls = local_calls = total_tokens = total_failures = 0
    total_cost = 0.0
    for row in usage_rows:
        model_name = str(row["model_ref"])
        backend = str(row["backend"])
        virtual = str(row.get("virtual_model") or "")
        role = str(row.get("role") or "")
        calls = int(row["calls"])
        successes = int(row["successes"])
        tokens = int(row["prompt_tokens"]) + int(row["output_tokens"])
        record = model_record(model_name)
        record["calls"] += calls
        record["call_successes"] += successes
        record["tokens"] += tokens
        record["cost_usd"] += float(row["cost_usd"])
        record["latency_weighted_ms"] += float(row["avg_latency_ms"]) * calls
        record["backends"].add(backend)
        if role:
            record["roles"][role] += calls

        is_local = backend in LOCAL_BACKENDS
        backend_id, model_id = f"backend:{backend}", f"model:{model_name}"
        node(backend_id, backend, "backend", is_local)
        node(model_id, model_name, "model", is_local)
        edge(backend_id, model_id, calls)
        if virtual:
            virtual_label = virtual if virtual.startswith("ctswarm/") else f"ctswarm/{virtual}"
            virtual_id = f"virtual:{virtual_label}"
            node(virtual_id, virtual_label, "virtual", True)
            edge(virtual_id, backend_id, calls)
        if role:
            role_id = f"role:{role}"
            node(role_id, role, "role")
            edge(model_id, role_id, calls)
        total_calls += calls
        local_calls += calls if is_local else 0
        total_tokens += tokens
        total_failures += int(row["failures"])
        total_cost += float(row["cost_usd"])

    model_list: list[dict[str, Any]] = []
    for name, record in models.items():
        calls = int(record["calls"])
        duration_samples = int(record.pop("duration_samples"))
        duration_total = float(record.pop("duration_ms"))
        latency_total = float(record.pop("latency_weighted_ms"))
        record["avg_duration_ms"] = duration_total / duration_samples if duration_samples else 0
        record["avg_latency_ms"] = latency_total / calls if calls else 0
        record["call_success_rate"] = record["call_successes"] / calls if calls else None
        record["roles"] = dict(record["roles"])
        record["harnesses"] = sorted(record["harnesses"])
        record["providers"] = sorted(record["providers"])
        record["backends"] = sorted(record["backends"])
        record["build_count"] = len(record.pop("builds"))
        record["local"] = _model_is_local(name, record["backends"])
        record["benchmark"] = benchmarks.get(name)
        model_list.append(record)

    model_list.sort(key=lambda item: (item["calls"], item["executions"]), reverse=True)
    concrete_models = [item for item in model_list if not item["name"].startswith("ctswarm/")]
    return {
        "window_hours": window_hours,
        "summary": {
            "builds": len(recent),
            "executions": execution_count,
            "active_executions": active_count,
            "local_active_executions": local_active_count,
            "failed_executions": failed_count,
            "execution_roles": dict(execution_roles),
            "models": len(concrete_models),
            "router_calls": total_calls,
            "router_failures": total_failures,
            "routing_rejections": routing_rejections,
            "local_calls": local_calls,
            "local_fraction": local_calls / total_calls if total_calls else 0.0,
            "tokens": total_tokens,
            "cost_usd": total_cost,
        },
        "capacity": capacity,
        "models": concrete_models,
        "routes": routes or {},
        "graph": {
            "nodes": list(nodes.values()),
            "edges": [
                {"source": source, "target": target, "value": value}
                for (source, target), value in edges.items()
            ],
        },
    }
