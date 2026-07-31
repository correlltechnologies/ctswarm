"""Durable, concurrency-limited build queue.

The scheduler is the always-on entry point for builds. Queue records and
terminal results live in the shared SQLite ledger, so a container restart does
not lose requests or forget an in-flight AgentField execution. A single worker
defaults to one active build because SWE-AF's build database and the local GPU
are shared resources; operators can raise the limit explicitly after providing
equivalent capacity.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .approvals.status import StatusNotifier
from .approvals.store import ApprovalStore
from .capacity import Runtime
from .ledger import Ledger
from .observability import AgentFieldTraceClient, harness_label, model_overview
from .orchestrator import BuildRecord, BuildState, Orchestrator, load_build

BUILD_ENQUEUED = "build_enqueued"
BUILD_TERMINAL = "build_terminal"
BUILD_INFRASTRUCTURE_RETRY = "build_infrastructure_retry"


def _retryable_infrastructure_stop(record: BuildRecord) -> bool:
    """True when AgentField stopped work because an agent node disappeared.

    An owner stop remains terminal. A control-plane cancellation caused by a
    node/container restart is infrastructure churn, not a verdict on the build,
    and must be resubmitted under the same durable queue record.
    """
    return record.state is BuildState.STOPPED and any(
        marker in record.error.lower()
        for marker in ("cancelled_by_control_plane", "canceled_by_control_plane")
    )


class BuildRequest(BaseModel):
    """Validated queue payload."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    repo_url: str = Field(min_length=1)
    require_strong_planning: bool = True
    max_ci_fix_cycles: int = Field(default=2, ge=0, le=10)
    # Zero means no scheduler deadline; the owner can still pause or stop.
    max_hours: float = Field(default=0.0, ge=0, le=720)


def _event_detail(event: dict) -> dict:
    try:
        value = json.loads(event.get("detail") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class BuildScheduler:
    """Dispatch queued builds while enforcing a durable concurrency limit."""

    def __init__(
        self,
        *,
        ledger: Ledger | None = None,
        orchestrator: Orchestrator | None = None,
        notifier: StatusNotifier | None = None,
        max_concurrent: int | None = None,
        poll_interval_s: float | None = None,
    ) -> None:
        self.ledger = ledger or Ledger(
            os.environ.get("CTSWARM_DB", "var/ctswarm.db")
        )
        self.orchestrator = orchestrator or Orchestrator(ledger=self.ledger)
        self.notifier = notifier or StatusNotifier(ledger=self.ledger)
        self.max_concurrent = max(
            1,
            max_concurrent
            if max_concurrent is not None
            else int(os.environ.get("CTSWARM_MAX_CONCURRENT_BUILDS", "1")),
        )
        self.poll_interval_s = (
            poll_interval_s
            if poll_interval_s is not None
            else float(os.environ.get("CTSWARM_SCHEDULER_POLL_SECONDS", "10"))
        )
        self._tasks: dict[str, asyncio.Task] = {}
        self._closed = False

    def enqueue(self, request: BuildRequest, *, build_id: str | None = None) -> str:
        build_id = build_id or f"build-{uuid.uuid4().hex[:10]}"
        if self.snapshot(build_id) is not None:
            raise ValueError(f"build id already exists: {build_id}")
        self.ledger.record_event(
            BUILD_ENQUEUED,
            request.model_dump(),
            build_id=build_id,
        )
        return build_id

    def _terminal_ids(self) -> set[str]:
        return {
            event["build_id"]
            for event in self.ledger.events(kind=BUILD_TERMINAL)
            if event.get("build_id")
        }

    def pending_requests(self) -> list[tuple[str, BuildRequest]]:
        terminal = self._terminal_ids()
        requests: dict[str, BuildRequest] = {}
        for event in self.ledger.events(kind=BUILD_ENQUEUED):
            build_id = event.get("build_id")
            if not build_id or build_id in terminal:
                continue
            try:
                requests[build_id] = BuildRequest.model_validate(
                    _event_detail(event)
                )
            except ValueError:
                self.ledger.record_event(
                    BUILD_TERMINAL,
                    {
                        "build_id": build_id,
                        "state": BuildState.FAILED.value,
                        "error": "invalid persisted queue payload",
                    },
                    build_id=build_id,
                )
        return list(requests.items())

    def snapshot(self, build_id: str) -> dict | None:
        terminal = self.ledger.events(kind=BUILD_TERMINAL, build_id=build_id)
        if terminal:
            return _event_detail(terminal[-1])

        record = load_build(self.ledger, build_id)
        if record is not None:
            return record.to_dict()

        queued = self.ledger.events(kind=BUILD_ENQUEUED, build_id=build_id)
        if not queued:
            return None
        detail = _event_detail(queued[-1])
        return {
            "build_id": build_id,
            "goal": detail.get("goal", ""),
            "repo_url": detail.get("repo_url", ""),
            "state": BuildState.QUEUED.value,
            "execution_id": "",
            "phase_detail": "waiting for a scheduler slot",
            "pr_url": "",
            "error": "",
            "gate_results": {},
        }

    def list_snapshots(self, limit: int = 50) -> list[dict]:
        build_ids: list[str] = []
        for event in self.ledger.events(kind=BUILD_ENQUEUED):
            build_id = event.get("build_id")
            if build_id and build_id not in build_ids:
                build_ids.append(build_id)
        snapshots = [
            snapshot
            for build_id in build_ids[-limit:]
            if (snapshot := self.snapshot(build_id)) is not None
        ]
        return list(reversed(snapshots))

    async def run_once(self) -> None:
        finished = [
            build_id for build_id, task in self._tasks.items() if task.done()
        ]
        for build_id in finished:
            task = self._tasks.pop(build_id)
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                # _run_request records an actionable terminal failure. Retrieving
                # the result here prevents "exception was never retrieved".
                pass

        pending = self.pending_requests()
        for build_id, request in pending:
            if build_id in self._tasks:
                continue
            if self.orchestrator.control_state(build_id) != "stopped":
                continue
            self._record_terminal(
                BuildRecord(
                    build_id=build_id,
                    goal=request.goal,
                    repo_url=request.repo_url,
                    runtime=Runtime.OPEN_CODE,
                    state=BuildState.STOPPED,
                    phase_detail="stopped before dispatch",
                )
            )

        slots = self.max_concurrent - len(self._tasks)
        if slots <= 0:
            return

        for build_id, request in self.pending_requests():
            if slots <= 0:
                break
            if build_id in self._tasks:
                continue
            self._tasks[build_id] = asyncio.create_task(
                self._run_request(build_id, request),
                name=f"ctswarm-build-{build_id}",
            )
            slots -= 1

    async def _run_request(self, build_id: str, request: BuildRequest) -> None:
        try:
            record = load_build(self.ledger, build_id)
            while True:
                if record is None or not record.execution_id:
                    record = await self.orchestrator.submit(
                        goal=request.goal,
                        repo_url=request.repo_url,
                        require_strong_planning=request.require_strong_planning,
                        max_ci_fix_cycles=request.max_ci_fix_cycles,
                        build_id=build_id,
                    )
                if not record.state.terminal:
                    await self.notifier.post(record)
                    record = await self.orchestrator.run_until_done(
                        record,
                        poll_interval_s=self.poll_interval_s,
                        max_hours=request.max_hours,
                        on_status=self.notifier.post,
                    )
                if (
                    _retryable_infrastructure_stop(record)
                    and self.orchestrator.control_state(build_id) != "stopped"
                ):
                    self.ledger.record_event(
                        BUILD_INFRASTRUCTURE_RETRY,
                        {
                            "prior_execution_id": record.execution_id,
                            "reason": record.error,
                        },
                        build_id=build_id,
                    )
                    record.execution_id = ""
                    record.state = BuildState.QUEUED
                    record.error = ""
                    record.phase_detail = "agent node restarted; retrying automatically"
                    await asyncio.sleep(min(self.poll_interval_s, 5.0))
                    continue
                self._record_terminal(record)
                break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - queue items must terminate visibly
            self._record_terminal(
                BuildRecord(
                    build_id=build_id,
                    goal=request.goal,
                    repo_url=request.repo_url,
                    runtime=Runtime.OPEN_CODE,
                    state=BuildState.FAILED,
                    error=f"scheduler failure: {type(exc).__name__}: {exc}",
                )
            )

    def _record_terminal(self, record: BuildRecord) -> None:
        if self.ledger.events(kind=BUILD_TERMINAL, build_id=record.build_id):
            return
        self.ledger.record_event(
            BUILD_TERMINAL,
            record.to_dict(),
            build_id=record.build_id,
        )

    async def run_forever(self) -> None:
        while not self._closed:
            await self.run_once()
            await asyncio.sleep(min(self.poll_interval_s, 5.0))

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def health(self) -> dict:
        pending = self.pending_requests()
        return {
            "ok": True,
            "queued": max(0, len(pending) - len(self._tasks)),
            "active": len(self._tasks),
            "max_concurrent": self.max_concurrent,
        }


scheduler = BuildScheduler()
trace_client = AgentFieldTraceClient(scheduler.orchestrator.agentfield_url)


async def _live_routes() -> dict[str, dict[str, Any]]:
    """Resolve virtual tiers to the concrete model the router would use now."""
    base_url = os.environ.get("CTSWARM_ROUTER_URL", "http://ctswarm-router:8090")
    routes: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        for tier in ("high", "med", "low"):
            alias = f"ctswarm/{tier}"
            try:
                response = await client.get(
                    f"{base_url}/routing/explain",
                    params={"tier": tier, "tools": "true", "context": 8192},
                )
                response.raise_for_status()
                decision = response.json()
                primary = decision.get("primary") or {}
                routes[alias] = {
                    "alias": alias,
                    "backend": primary.get("backend") or "",
                    "model": primary.get("model_ref") or "",
                    "degraded_to": decision.get("degraded"),
                    "reason": primary.get("reason") or "",
                }
            except (httpx.HTTPError, ValueError):
                routes[alias] = {
                    "alias": alias,
                    "backend": "",
                    "model": "",
                    "degraded_to": None,
                    "reason": "router unavailable",
                }
    return routes


def _enrich_trace_routes(trace: dict, routes: dict[str, dict[str, Any]]) -> dict:
    for node in trace.get("timeline", []):
        requested = str(node.get("model") or "")
        node["requested_model"] = requested
        route = routes.get(requested)
        if route:
            node["resolved_model"] = route.get("model") or "not dispatched"
            node["resolved_backend"] = route.get("backend") or "pending"
            node["resolution"] = "live router policy"
        else:
            node["resolved_model"] = requested
            node["resolved_backend"] = node.get("provider") or node.get("runtime") or ""
            node["resolution"] = "direct provider"
    trace["routes"] = routes
    return trace


@asynccontextmanager
async def lifespan(_app: FastAPI):
    worker = asyncio.create_task(scheduler.run_forever(), name="ctswarm-scheduler")
    try:
        yield
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await scheduler.close()


app = FastAPI(title="ctswarm scheduler", version="1.0.0", lifespan=lifespan)
DASHBOARD_DIST = Path(__file__).parent / "static" / "dashboard"
if (DASHBOARD_DIST / "assets").is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=DASHBOARD_DIST / "assets"),
        name="dashboard-assets",
    )


@app.get("/", response_class=FileResponse)
@app.get("/dashboard", response_class=FileResponse)
async def dashboard() -> FileResponse:
    """Serve the compiled shadcn operator console."""
    index = DASHBOARD_DIST / "index.html"
    if not index.is_file():
        raise HTTPException(
            status_code=503,
            detail="dashboard assets are not built; run npm run build in dashboard-ui",
        )
    return FileResponse(index)


@app.get("/health")
async def health() -> dict:
    return scheduler.health()


@app.get("/builds")
async def list_builds(limit: int = 50) -> dict:
    return {"builds": scheduler.list_snapshots(max(1, min(limit, 200)))}


@app.post("/builds", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_build(request: BuildRequest, response: Response) -> dict:
    try:
        build_id = scheduler.enqueue(request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response.headers["Location"] = f"/builds/{build_id}"
    return scheduler.snapshot(build_id) or {"build_id": build_id, "state": "queued"}


@app.get("/builds/{build_id}")
async def get_build(build_id: str) -> dict:
    snapshot = scheduler.snapshot(build_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="unknown build")
    return snapshot


@app.get("/builds/{build_id}/trace")
async def get_build_trace(build_id: str) -> dict:
    """Join one ctswarm build to its full AgentField execution trace."""
    snapshot = scheduler.snapshot(build_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="unknown build")
    execution_id = str(snapshot.get("execution_id") or "")
    if not execution_id:
        runtime = str(snapshot.get("runtime") or "")
        return {
            "execution_id": "",
            "workflow_id": "",
            "status": snapshot.get("state", "queued"),
            "runtime": runtime,
            "harness": harness_label(runtime),
            "model_policy": {},
            "total_nodes": 0,
            "summary": {"statuses": {}, "roles": {}, "models": {}, "harnesses": {}},
            "timeline": [],
        }
    try:
        trace, routes = await asyncio.gather(
            trace_client.build_trace(execution_id), _live_routes()
        )
        return _enrich_trace_routes(trace, routes)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"AgentField trace unavailable: {exc}",
        ) from exc


@app.get("/builds/{build_id}/approvals")
async def get_build_approvals(build_id: str) -> dict:
    if scheduler.snapshot(build_id) is None:
        raise HTTPException(status_code=404, detail="unknown build")
    store = ApprovalStore(os.environ.get("CTSWARM_DB", "var/ctswarm.db"))
    approvals = store.all_for_build(build_id)
    now = time.time()
    for approval in approvals:
        approval["expired"] = bool(
            not approval.get("decision")
            and float(approval.get("expires_at") or 0) < now
        )
    return {"approvals": approvals}


@app.get("/api/dashboard/executions/{execution_id}")
async def get_execution_detail(execution_id: str) -> dict:
    try:
        return await trace_client.execution_details(execution_id)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"AgentField execution detail unavailable: {exc}",
        ) from exc


@app.get("/api/dashboard/models")
async def get_model_overview(window_hours: float = 168.0) -> dict:
    """Fleet-wide graph and statistics across builds and concrete routes."""
    routes = await _live_routes()
    return await model_overview(
        builds=scheduler.list_snapshots(200),
        trace_client=trace_client,
        ledger=scheduler.ledger,
        routing_path=os.environ.get("CTSWARM_ROUTING", "bench/results/routing.json"),
        capacity=scheduler.orchestrator.capacity.report(),
        routes=routes,
        window_hours=max(1.0, min(window_hours, 24.0 * 90.0)),
    )


async def _dashboard_stream_snapshot(build_id: str | None) -> dict:
    """Assemble one compact live dashboard frame.

    AgentField remains the source of truth for coordination. The inference
    ledger is included separately because external harness calls do not always
    carry a build id, and hiding those calls would under-report local work.
    """
    builds = scheduler.list_snapshots(200)
    payload: dict[str, Any] = {
        "sequence": time.time_ns(),
        "generated_at": time.time(),
        "builds": builds,
        "inference_calls": scheduler.ledger.recent_calls(limit=50),
    }
    if not build_id:
        return payload
    snapshot = scheduler.snapshot(build_id)
    if snapshot is None:
        payload["missing_build"] = build_id
        return payload
    payload["build"] = snapshot
    try:
        trace, approvals = await asyncio.gather(
            get_build_trace(build_id),
            get_build_approvals(build_id),
        )
        payload["trace"] = trace
        payload["approvals"] = approvals.get("approvals", [])
    except HTTPException as exc:
        payload["stream_error"] = str(exc.detail)
    return payload


@app.get("/api/dashboard/stream")
async def dashboard_stream(request: Request, build_id: str | None = None) -> StreamingResponse:
    """Continuously stream build, agent, and concrete model activity over SSE."""

    async def events():
        while not await request.is_disconnected():
            try:
                payload = await _dashboard_stream_snapshot(build_id)
                yield f"event: snapshot\ndata: {json.dumps(payload, default=str)}\n\n"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep telemetry failures from ending the stream
                error = json.dumps({"message": str(exc), "generated_at": time.time()})
                yield f"event: stream-error\ndata: {error}\n\n"
            await asyncio.sleep(2.0)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/dashboard/approvals/{dedupe_key}/decide")
async def decide_dashboard_approval(
    dedupe_key: str,
    request: Request,
) -> JSONResponse:
    """Same-origin approval action for the unified dashboard."""
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{scheduler.orchestrator.approvals_url}/approvals/"
                f"{dedupe_key}/decide",
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"approval service unavailable: {exc}",
        ) from exc
    try:
        body = response.json()
    except ValueError:
        body = {"error": response.text[:300]}
    return JSONResponse(body, status_code=response.status_code)


def _control(build_id: str, action: Callable[[str, str], Any]) -> dict:
    if scheduler.snapshot(build_id) is None:
        raise HTTPException(status_code=404, detail="unknown build")
    action(build_id, "scheduler-api")
    return {"build_id": build_id, "control": scheduler.orchestrator.control_state(build_id)}


@app.post("/builds/{build_id}/pause")
async def pause_build(build_id: str) -> dict:
    return _control(build_id, scheduler.orchestrator.request_pause)


@app.post("/builds/{build_id}/resume")
async def resume_build(build_id: str) -> dict:
    return _control(build_id, scheduler.orchestrator.request_resume)


@app.post("/builds/{build_id}/stop")
async def stop_build(build_id: str) -> dict:
    return _control(build_id, scheduler.orchestrator.request_stop)
