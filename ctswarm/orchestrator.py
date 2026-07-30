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
    phase_detail: str = ""
    execution_id: str = ""
    pr_url: str = ""
    error: str = ""
    gate_results: dict = field(default_factory=dict)

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
            "phase_detail": self.phase_detail,
            "elapsed_s": int(self.elapsed_s),
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


class Orchestrator:
    """Drives one build from goal to gated result."""

    def __init__(
        self,
        *,
        agentfield_url: str | None = None,
        approvals_url: str | None = None,
        ledger: Ledger | None = None,
        status_interval_s: float = 900.0,
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

        runtime, why = self.capacity.select(require_strong=require_strong_planning)
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
            },
            build_id=build_id,
        )

        config: dict = {
            "runtime": runtime.value,
            "check_ci": True,
            "max_ci_fix_cycles": max_ci_fix_cycles,
        }
        # Only the open runtime routes through ctswarm's virtual models. The CLI
        # harnesses use their own model selection, so sending ctswarm/* to them
        # would be meaningless at best.
        if runtime is Runtime.OPEN_CODE:
            config["models"] = {
                "default": "ctswarm/med",
                "pm": "ctswarm/high",
                "architect": "ctswarm/high",
                "tech_lead": "ctswarm/high",
                "replan": "ctswarm/high",
                "qa_synthesizer": "ctswarm/low",
                "git": "ctswarm/low",
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

        status = str(body.get("status") or "").lower()
        mapping = {
            "pending": BuildState.QUEUED,
            "queued": BuildState.QUEUED,
            "running": BuildState.EXECUTING,
            "in_progress": BuildState.EXECUTING,
            "completed": BuildState.VERIFYING,
            "success": BuildState.VERIFYING,
            "failed": BuildState.FAILED,
            "error": BuildState.FAILED,
        }
        new_state = mapping.get(status)
        if new_state and new_state is not record.state:
            record.state = new_state
            record.updated_at = time.time()

        result = body.get("result") or body.get("output") or {}
        if isinstance(result, dict):
            record.pr_url = result.get("pr_url") or record.pr_url
            summary = result.get("summary")
            if summary:
                record.phase_detail = str(summary)[:300]
        if body.get("error"):
            reason = body.get("status_reason") or ""
            detail = (body.get("result") or {}).get("detail") if isinstance(body.get("result"), dict) else ""
            record.error = " | ".join(
                str(x) for x in (body["error"], reason, detail) if x
            )[:400]
        return record

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
        return gates

    # -- the loop ----------------------------------------------------------

    async def run_until_done(
        self,
        record: BuildRecord,
        *,
        poll_interval_s: float = 30.0,
        max_hours: float = 12.0,
        on_status=None,
    ) -> BuildRecord:
        """Poll until the build finishes, is paused, or is stopped."""
        deadline = time.time() + max_hours * 3600.0
        last_status_at = 0.0
        last_state = record.state

        while not record.state.terminal:
            control = self.control_state(record.build_id)

            if control == "stopped":
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

            if time.time() > deadline:
                record.state = BuildState.FAILED
                record.error = f"exceeded the {max_hours}h wall-clock limit"
                break

            await self.poll(record)

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
    return record
