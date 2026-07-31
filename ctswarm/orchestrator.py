"""Build orchestrator: hand it a goal, it runs until done or blocked.

This is the layer that turns the parts into a system. It picks a runtime from
remaining capacity, submits the build to SWE-AF, polls progress, posts periodic
status to Slack, honours pause and stop requests, and runs the committee and
scanner gates on the finished branch before declaring anything complete.

Three behaviors are deliberate and worth stating, because the obvious
implementations of each are wrong:

**Pause is checked, not signalled.** There is no way to interrupt SWE-AF
mid-reasoner, so a pause request is recorded and honoured at the next phase
boundary. Claiming an instant stop would be a lie; what is promised is that no
*new* work starts after a pause, which is the property that actually matters for
cost and blast radius.

**Status updates are throttled and change-driven.** The plan is explicit that the
system must not send progress spam. An update goes out on a timer only if
something changed, plus immediately on phase transitions and terminal states.

**Completion requires evidence, not a return value.** SWE-AF reporting success is
an input to the decision, never the decision itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import httpx

from .capacity import CapacityManager, Runtime
from .ledger import Ledger


class BuildState(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    GATING = "gating"
    COMPLETE = "complete"
    FAILED = "failed"
    PAUSED = "paused"
    STOPPED = "stopped"
    BLOCKED = "blocked"

    @property
    def terminal(self) -> bool:
        return self in (
            BuildState.COMPLETE,
            BuildState.FAILED,
            BuildState.STOPPED,
            BuildState.BLOCKED,
        )


@dataclass
class BuildRecord:
    """Live state of one build."""

    build_id: str
    goal: str
    repo_url: str
    runtime: Runtime
    state: BuildState = BuildState.QUEUED
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_progress_at: float = field(default_factory=time.time)
    phase_detail: str = ""
    execution_id: str = ""
    pr_url: str = ""
    error: str = ""
    gate_results: dict = field(default_factory=dict)
    _progress_fingerprint: str = field(default="", repr=False)

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.started_at

    def to_dict(self) -> dict:
        return {
            "build_id": self.build_id,
            "goal": self.goal,
            "repo_url": self.repo_url,
            "runtime": self.runtime.value,
            "state": self.state.value,
            "execution_id": self.execution_id,
            "phase_detail": self.phase_detail,
            "elapsed_s": int(self.elapsed_s),
            "stalled_s": max(0, int(time.time() - self.last_progress_at)),
            "pr_url": self.pr_url,
            "error": self.error,
            "gate_results": self.gate_results,
        }


# Control signals are stored in the ledger rather than held in memory, so a
# pause survives an orchestrator restart. A pause that evaporates when the
# process restarts is not a pause.
CONTROL_PAUSE = "build_control_pause"
CONTROL_RESUME = "build_control_resume"
CONTROL_STOP = "build_control_stop"


def runtime_model_overrides(runtime: Runtime) -> dict[str, str]:
    """Return model names that are valid for the selected CLI harness.

    The agent containers expose ``SWE_MODEL_*`` aliases for the OpenCode/router
    path. SWE-AF treats those environment variables as global defaults, so an
    explicit per-build override is required when capacity selection chooses a
    different runtime. Otherwise ``claude`` or ``codex`` receives an OpenCode
    model id such as ``ctswarm/med`` and can hang or fail before doing any work.
    """
    if runtime is Runtime.OPEN_CODE:
        return {
            "default": "ctswarm/med",
            "pm": "ctswarm/high",
            "architect": "ctswarm/high",
            "tech_lead": "ctswarm/high",
            "replan": "ctswarm/high",
            "qa_synthesizer": "ctswarm/low",
            "git": "ctswarm/low",
        }
    if runtime is Runtime.CLAUDE_CODE:
        return {
            "default": "sonnet",
            "qa_synthesizer": "haiku",
        }
    auth_mode = os.environ.get("SWE_CODEX_AUTH_MODE", "auto").strip().lower()
    uses_api_key = auth_mode == "api_key" or (
        auth_mode == "auto" and bool(os.environ.get("OPENAI_API_KEY", "").strip())
    )
    # The `-codex` model is API-key-only; ChatGPT-account login needs the base
    # model. This mirrors SWE-AF's own auth-aware default selection.
    return {"default": "gpt-5.3-codex" if uses_api_key else "gpt-5.5"}


HOSTED_ROLES = (
    "pm",
    "architect",
    "tech_lead",
    "sprint_planner",
    "code_reviewer",
    "verifier",
)


def hybrid_role_policy(
    planning_runtime: Runtime,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return provider/model maps for local work plus bounded hosted judgment.

    OpenCode is always the base runtime. A subscription harness, when available,
    is assigned only to planning and independent review roles. If both hosted
    subscriptions are unavailable the same map collapses cleanly to local models.
    """
    models = runtime_model_overrides(Runtime.OPEN_CODE)
    providers = {"default": Runtime.OPEN_CODE.value}
    if planning_runtime is Runtime.OPEN_CODE:
        return providers, models

    hosted_model = runtime_model_overrides(planning_runtime)["default"]
    for role in HOSTED_ROLES:
        providers[role] = planning_runtime.value
        models[role] = hosted_model
    return providers, models


class Orchestrator:
    """Drives one build from goal to gated result."""

    def __init__(
        self,
        *,
        agentfield_url: str | None = None,
        approvals_url: str | None = None,
        ledger: Ledger | None = None,
        status_interval_s: float = 900.0,
        no_progress_timeout_s: float | None = None,
    ) -> None:
        self.agentfield_url = (
            agentfield_url
            or os.environ.get("CTSWARM_AGENTFIELD_URL")
            or f"http://localhost:{os.environ.get('CTSWARM_CONTROL_PLANE_PORT', '18080')}"
        ).rstrip("/")
        self.approvals_url = (
            approvals_url or os.environ.get("CTSWARM_APPROVALS_URL")
            or "http://localhost:8091"
        ).rstrip("/")
        self.ledger = ledger or Ledger(os.environ.get("CTSWARM_DB", "var/ctswarm.db"))
        self.capacity = CapacityManager(ledger=self.ledger)
        self.status_interval_s = status_interval_s
        self.no_progress_timeout_s = (
            float(os.environ.get("CTSWARM_NO_PROGRESS_TIMEOUT_S", "900"))
            if no_progress_timeout_s is None
            else no_progress_timeout_s
        )

    # -- control -----------------------------------------------------------

    def request_pause(self, build_id: str, who: str = "owner") -> None:
        self.ledger.record_event(CONTROL_PAUSE, {"by": who}, build_id=build_id)

    def request_resume(self, build_id: str, who: str = "owner") -> None:
        self.ledger.record_event(CONTROL_RESUME, {"by": who}, build_id=build_id)

    def request_stop(self, build_id: str, who: str = "owner") -> None:
        self.ledger.record_event(CONTROL_STOP, {"by": who}, build_id=build_id)

    def control_state(self, build_id: str) -> str:
        """Latest control signal: running | paused | stopped.

        Derived from the event log rather than a mutable flag, so the decision is
        reconstructible after a restart and auditable afterwards.
        """
        events = [
            e
            for e in self.ledger.events(build_id=build_id)
            if e["kind"] in (CONTROL_PAUSE, CONTROL_RESUME, CONTROL_STOP)
        ]
        if not events:
            return "running"
        latest = events[-1]["kind"]
        if latest == CONTROL_STOP:
            return "stopped"
        if latest == CONTROL_PAUSE:
            return "paused"
        return "running"

    # -- submission --------------------------------------------------------

    async def submit(
        self,
        *,
        goal: str,
        repo_url: str,
        require_strong_planning: bool = True,
        max_ci_fix_cycles: int = 2,
        build_id: str | None = None,
    ) -> BuildRecord:
        """Start a SWE-AF build with a capacity-chosen runtime."""
        build_id = build_id or f"build-{uuid.uuid4().hex[:10]}"

        planning_runtime, why = self.capacity.select(
            require_strong=require_strong_planning
        )
        runtime = Runtime.OPEN_CODE
        providers, models = hybrid_role_policy(planning_runtime)
        record = BuildRecord(
            build_id=build_id, goal=goal, repo_url=repo_url, runtime=runtime
        )

        self.ledger.record_event(
            "build_submitted",
            {
                "goal": goal,
                "repo_url": repo_url,
                "runtime": runtime.value,
                "runtime_reason": why,
                "planning_runtime": planning_runtime.value,
                "provider_policy": providers,
            },
            build_id=build_id,
        )

        config: dict = {
            "runtime": runtime.value,
            "providers": providers,
            "check_ci": True,
            "max_ci_fix_cycles": max_ci_fix_cycles,
            # Bound every individual agent call as well as the build-level
            # semantic watchdog. The old upstream default was 45 minutes,
            # allowing two no-output coder attempts to waste roughly 90 minutes.
            "agent_timeout_seconds": int(
                os.environ.get("CTSWARM_AGENT_TIMEOUT_SECONDS", "900")
            ),
            # Acceptance failures must have enough room to become repair work.
            # A single retry is routinely consumed by the first cross-feature
            # browser failure on UI builds.
            "max_verify_fix_cycles": 3,
            "continuous_repair": True,
            "models": models,
        }

        payload = {
            "goal": goal,
            "repo_url": repo_url,
            "enable_github_pr": True,
            "config": config,
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                # Both details verified against the live control plane:
                # the target is dot-separated (`node.reasoner`; a slash 404s),
                # and the body must be wrapped in `input` (a bare payload is
                # rejected with "Missing required field: goal").
                response = await client.post(
                    f"{self.agentfield_url}/api/v1/execute/async/swe-planner.build",
                    json={"input": payload},
                )
            if response.status_code >= 400:  # 202 Accepted is the success case
                record.state = BuildState.FAILED
                record.error = f"submit failed HTTP {response.status_code}: {response.text[:300]}"
                return record
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            record.state = BuildState.FAILED
            record.error = f"could not reach the control plane: {exc}"
            return record

        record.execution_id = str(
            body.get("execution_id") or body.get("id") or body.get("request_id") or ""
        )
        if not record.execution_id:
            record.state = BuildState.FAILED
            record.error = f"control plane returned no execution id: {str(body)[:200]}"
            return record
        record.state = BuildState.PLANNING
        self.ledger.record_event(
            "build_started",
            {"execution_id": record.execution_id},
            build_id=build_id,
        )
        return record

    async def poll(self, record: BuildRecord) -> BuildRecord:
        """Refresh a build's state from the control plane."""
        if not record.execution_id:
            return record
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{self.agentfield_url}/api/v1/executions/{record.execution_id}"
                )
            body = response.json() if response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return record

        return update_record_from_execution(record, body)

    async def cancel_execution(self, record: BuildRecord, reason: str) -> bool:
        """Cooperatively stop the AgentField reasoner behind a terminal build.

        The scheduler owns the user-selected wall-clock deadline. AgentField's
        SDK watchdog is deliberately configured above the scheduler's maximum
        so it cannot preempt valid work; this cancellation closes the other
        half of that contract and prevents timed-out builds from running in the
        background after the queue has released their slot.
        """
        if not record.execution_id:
            return False
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.agentfield_url}/api/v1/executions/"
                    f"{record.execution_id}/cancel",
                    json={"reason": reason},
                )
        except httpx.HTTPError:
            return False
        return response.status_code < 400

    # -- gating ------------------------------------------------------------

    async def run_gates(self, record: BuildRecord, repo_path: str | Path) -> dict:
        """Run deterministic scanners and the committee on the finished work.

        Runs against the *integrated* branch, matching the plan's requirement
        that the final verifier reviews the merged result rather than isolated
        issue branches. Scanners run first because they are cheap, decisive, and
        cannot be argued with.
        """
        from .backends import build_backends
        from .committee import Rule, convene, eligible_members
        from .evidence.scanners import run_all, summarize
        from .router.policy import RoutingTable

        record.state = BuildState.GATING
        outcomes = run_all(repo_path, include_tests=True)
        scan_summary = summarize(outcomes)

        gates: dict = {"scanners": scan_summary}

        # A scanner failure is decisive; convening a committee to discuss it
        # would only create an opportunity to override a fact.
        if not scan_summary["passed"]:
            gates["committee"] = {"skipped": "deterministic gates already failed"}
            record.gate_results = gates
            record.state = BuildState.BLOCKED
            record.error = "deterministic completion gates failed or could not run"
            return gates

        members = eligible_members(RoutingTable.load())
        if len(members) < 2:
            gates["committee"] = {
                "skipped": (
                    f"only {len(members)} independent model family/families "
                    "available; a single-family panel is not a committee"
                ),
                "needs_human": True,
            }
            record.gate_results = gates
            record.state = BuildState.BLOCKED
            record.error = "completion committee quorum is unavailable"
            return gates

        backends = build_backends()
        backend = backends.get("ollama") or next(iter(backends.values()))
        try:
            diff = _read_diff(repo_path)
            result = await convene(
                question=(
                    "Does this change fully and correctly implement the stated "
                    "goal, without introducing a defect or security issue? "
                    f"GOAL: {record.goal}"
                ),
                context=diff,
                backend=backend,
                members=members,
                scanners=[o.to_committee() for o in outcomes],
                rule=Rule.ANY_REJECT_BLOCKS,
                min_families=2,
            )
            gates["committee"] = result.to_dict()
        finally:
            for backend_obj in backends.values():
                await backend_obj.close()

        record.gate_results = gates
        if result.approved and not result.needs_human:
            record.state = BuildState.COMPLETE
            record.error = ""
        else:
            record.state = BuildState.BLOCKED
            record.error = "completion committee did not approve the integrated change"
        return gates

    # -- the loop ----------------------------------------------------------

    async def run_until_done(
        self,
        record: BuildRecord,
        *,
        poll_interval_s: float = 30.0,
        max_hours: float = 0.0,
        on_status=None,
    ) -> BuildRecord:
        """Poll until the build finishes, is paused, or is stopped."""
        deadline = time.time() + max_hours * 3600.0 if max_hours > 0 else None
        last_status_at = 0.0
        last_state = record.state

        while not record.state.terminal:
            control = self.control_state(record.build_id)

            if control == "stopped":
                await self.cancel_execution(record, "stopped by owner")
                record.state = BuildState.STOPPED
                record.phase_detail = "stopped by owner"
                self.ledger.record_event("build_stopped", {}, build_id=record.build_id)
                break

            if control == "paused":
                if record.state is not BuildState.PAUSED:
                    record.state = BuildState.PAUSED
                    record.phase_detail = (
                        "paused by owner; no new work will start. "
                        "In-flight agent calls finish first."
                    )
                    self.ledger.record_event(
                        "build_paused", {}, build_id=record.build_id
                    )
                    if on_status:
                        await on_status(record)
                await asyncio.sleep(poll_interval_s)
                continue

            if record.state is BuildState.PAUSED:
                record.state = BuildState.EXECUTING
                self.ledger.record_event("build_resumed", {}, build_id=record.build_id)

            if deadline is not None and time.time() > deadline:
                reason = f"exceeded the {max_hours}h wall-clock limit"
                cancelled = await self.cancel_execution(record, reason)
                record.state = BuildState.FAILED
                record.error = reason
                self.ledger.record_event(
                    "build_deadline_exceeded",
                    {
                        "max_hours": max_hours,
                        "execution_cancelled": cancelled,
                    },
                    build_id=record.build_id,
                )
                break

            await self.poll(record)

            # A running status is not progress. If the control plane reports the
            # same semantic execution state for too long, fail closed and free
            # the worker instead of letting a dead agent consume hours silently.
            stalled_s = time.time() - record.last_progress_at
            if (
                not record.state.terminal
                and self.no_progress_timeout_s > 0
                and stalled_s >= self.no_progress_timeout_s
            ):
                reason = (
                    "no semantic build progress for "
                    f"{int(stalled_s)}s (limit {int(self.no_progress_timeout_s)}s)"
                )
                cancelled = await self.cancel_execution(record, reason)
                record.state = BuildState.FAILED
                record.error = reason
                record.phase_detail = "stalled execution cancelled"
                self.ledger.record_event(
                    "build_no_progress_timeout",
                    {
                        "stalled_s": int(stalled_s),
                        "limit_s": int(self.no_progress_timeout_s),
                        "execution_cancelled": cancelled,
                    },
                    build_id=record.build_id,
                )
                break

            # Update on a phase change immediately, or on the timer if something
            # is still moving. Never on a fixed schedule regardless of change,
            # which is how status updates become noise people stop reading.
            changed = record.state is not last_state
            due = (time.time() - last_status_at) >= self.status_interval_s
            if on_status and (changed or due):
                await on_status(record)
                last_status_at = time.time()
                last_state = record.state

            if record.state.terminal:
                break
            await asyncio.sleep(poll_interval_s)

        if on_status:
            await on_status(record)
        return record


def _read_diff(repo_path: str | Path, base_ref: str = "main", limit: int = 60_000) -> str:
    """The integrated diff, truncated to something a model can hold."""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "diff", f"{base_ref}...HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(could not read diff: {exc})"
    diff = proc.stdout
    if len(diff) > limit:
        # Say so explicitly. A silently truncated diff would let a reviewer
        # approve code it never saw.
        return diff[:limit] + (
            f"\n\n[TRUNCATED: diff is {len(diff)} chars, showing first {limit}. "
            "Reviewers have NOT seen the remainder.]"
        )
    return diff or "(no changes)"


def update_record_from_execution(record: BuildRecord, body: dict) -> BuildRecord:
    """Apply one AgentField execution response to a build record.

    AgentField reports a successfully *executed reasoner* as ``succeeded`` even
    when the reasoner's structured result says ``success: false``. Both layers
    matter: terminal transport status ends polling, while the result decides
    whether the build completed or failed.
    """
    fingerprint = _execution_progress_fingerprint(body)
    if fingerprint and fingerprint != record._progress_fingerprint:
        now = time.time()
        record._progress_fingerprint = fingerprint
        record.last_progress_at = now
        record.updated_at = now

    result = body.get("result") or body.get("output") or {}
    if isinstance(result, dict):
        record.pr_url = result.get("pr_url") or record.pr_url
        summary = result.get("summary")
        if summary:
            record.phase_detail = str(summary)[:300]

    status = str(body.get("status") or "").lower()
    if status in {"pending", "queued"}:
        new_state = BuildState.QUEUED
    elif status in {"running", "in_progress"}:
        new_state = BuildState.EXECUTING
    elif status in {"completed", "success", "succeeded"}:
        if not isinstance(result, dict) or result.get("success") is not True:
            new_state = BuildState.FAILED
            record.error = str(
                (result.get("error_message") if isinstance(result, dict) else "")
                or (result.get("error") if isinstance(result, dict) else "")
                or (result.get("summary") if isinstance(result, dict) else "")
                or "build reasoner returned success=false"
            )[:400]
        else:
            new_state = BuildState.COMPLETE
    elif status in {"cancelled", "canceled"}:
        new_state = BuildState.STOPPED
    elif status in {"failed", "error"}:
        new_state = BuildState.FAILED
    else:
        new_state = None

    if new_state and new_state is not record.state:
        record.state = new_state
        record.updated_at = time.time()

    if body.get("error"):
        reason = body.get("status_reason") or ""
        detail = result.get("detail") if isinstance(result, dict) else ""
        record.error = " | ".join(
            str(value) for value in (body["error"], reason, detail) if value
        )[:400]
    return record


_VOLATILE_PROGRESS_KEYS = frozenset(
    {
        "created_at",
        "updated_at",
        "timestamp",
        "last_heartbeat",
        "heartbeat_at",
        "elapsed",
        "elapsed_s",
        "duration",
        "duration_ms",
    }
)


def _execution_progress_fingerprint(body: dict) -> str:
    """Hash semantic execution state while ignoring heartbeat-only churn.

    The control plane may refresh timestamps on every poll. Treating those as
    progress recreated the exact failure mode this watchdog prevents: a wedged
    coder could look alive forever while producing no output or phase change.
    """

    def stable(value):
        if isinstance(value, dict):
            return {
                str(key): stable(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
                if str(key).lower() not in _VOLATILE_PROGRESS_KEYS
            }
        if isinstance(value, list):
            return [stable(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    try:
        payload = json.dumps(stable(body), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_build(ledger: Ledger, build_id: str) -> BuildRecord | None:
    """Reconstruct a build from the event log, for status after a restart."""
    events = ledger.events(build_id=build_id)
    submitted = next((e for e in events if e["kind"] == "build_submitted"), None)
    if not submitted:
        return None
    try:
        detail = json.loads(submitted["detail"])
    except (ValueError, TypeError):
        detail = {}

    record = BuildRecord(
        build_id=build_id,
        goal=detail.get("goal", ""),
        repo_url=detail.get("repo_url", ""),
        runtime=Runtime(detail.get("runtime", "open_code")),
        started_at=submitted["ts"],
    )
    for event in events:
        if event["kind"] == "build_started":
            try:
                record.execution_id = json.loads(event["detail"]).get("execution_id", "")
            except (ValueError, TypeError):
                pass
        elif event["kind"] == "build_stopped":
            record.state = BuildState.STOPPED
        elif event["kind"] == "build_paused":
            record.state = BuildState.PAUSED
        elif event["kind"] == "build_resumed":
            record.state = BuildState.EXECUTING
        elif event["kind"] == "build_infrastructure_retry":
            record.state = BuildState.QUEUED
            record.execution_id = ""
            record.error = ""
            record.phase_detail = "agent node restarted; retrying automatically"
        elif event["kind"] == "build_status":
            try:
                status = json.loads(event["detail"])
                record.state = BuildState(status.get("state", record.state.value))
                record.phase_detail = status.get("detail") or record.phase_detail
                record.pr_url = status.get("pr_url") or record.pr_url
            except (ValueError, TypeError):
                pass
        elif event["kind"] == "build_terminal":
            try:
                terminal = json.loads(event["detail"])
                record.state = BuildState(terminal["state"])
                record.phase_detail = terminal.get("phase_detail", "")
                record.pr_url = terminal.get("pr_url", "")
                record.error = terminal.get("error", "")
                record.execution_id = (
                    terminal.get("execution_id") or record.execution_id
                )
            except (KeyError, ValueError, TypeError):
                pass
    return record
