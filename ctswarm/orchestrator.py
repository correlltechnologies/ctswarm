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

from .capacity import CapacityManager, Runtime, rate_limit_signal
from .execution_mode import subscription_only
from .ledger import Ledger
from .routing_config import (
    LANE_ROLES,
    apply_routing_policy,
    load_routing_policy,
    normalize_routing_policy,
)
from .settings import get_setting


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
    # Which runtimes this build has already reported as exhausted. `poll` runs
    # every few seconds against an unchanging failure message, so without this
    # one rate limit would write hundreds of identical ledger events.
    _rate_limited_seen: set = field(default_factory=set, repr=False)

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
# Overridable because the right value depends on how the agent image is built:
# a container running as a non-root user can use the stricter bypassPermissions,
# and this default is chosen for the image ctswarm actually ships, which is root.
PERMISSION_MODE = os.environ.get("CTSWARM_PERMISSION_MODE", "acceptEdits").strip()

CONTROL_PAUSE = "build_control_pause"
CONTROL_RESUME = "build_control_resume"
CONTROL_STOP = "build_control_stop"


def runtime_model_overrides(
    runtime: Runtime, *, subscriptions_only: bool | None = None
) -> dict[str, str]:
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
    if subscriptions_only is None:
        subscriptions_only = subscription_only()
    auth_mode = os.environ.get("SWE_CODEX_AUTH_MODE", "auto").strip().lower()
    if subscriptions_only:
        # A subscription-only host has no API key by definition, so the
        # API-key-only `-codex` model would fail on the first call. Pin the
        # ChatGPT-login model regardless of what SWE_CODEX_AUTH_MODE says.
        uses_api_key = False
    else:
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
    "issue_writer",
    "code_reviewer",
    "verifier",
)


# Which harness owns which lane when there is no local runtime to fall back on.
#
# The split is not arbitrary. README's committee rule is that independence is by
# *model family*, because models sharing training data are wrong together. Claude
# writes the plan and the code; Codex reviews it and runs QA. That puts a genuine
# vendor boundary between the work and its review, which the previous local
# arrangement (two Qwen variants voting) never had.
#
# Maintenance goes to Codex to keep mechanical git/merge chatter off the same
# subscription window that planning and implementation are drawing down.
SUBSCRIPTION_LANE_RUNTIMES: dict[str, Runtime] = {
    "planning": Runtime.CLAUDE_CODE,
    "implementation": Runtime.CLAUDE_CODE,
    "review": Runtime.CODEX,
    "maintenance": Runtime.CODEX,
}


def subscription_role_policy(
    *, available: set[Runtime] | None = None
) -> tuple[dict[str, str], dict[str, str], Runtime]:
    """Assign every role to a CLI harness, with no local runtime anywhere.

    Returns the provider map, the model map, and the base runtime SWE-AF should
    launch with. The base runtime is the one owning the implementation lane,
    because that is what the `default` role resolves to and therefore what any
    role without an explicit assignment inherits.

    ``available`` narrows the split when only one subscription is usable: with a
    single harness every lane collapses onto it. That is a real degradation --
    review is no longer independent of implementation -- so the caller records
    the reason rather than letting it pass silently.
    """
    usable = available if available else set(SUBSCRIPTION_LANE_RUNTIMES.values())
    fallback = Runtime.CLAUDE_CODE if Runtime.CLAUDE_CODE in usable else Runtime.CODEX

    providers: dict[str, str] = {}
    models: dict[str, str] = {}
    for lane, roles in LANE_ROLES.items():
        runtime = SUBSCRIPTION_LANE_RUNTIMES.get(lane, fallback)
        if runtime not in usable:
            runtime = fallback
        overrides = runtime_model_overrides(runtime, subscriptions_only=True)
        for role in roles:
            providers[role] = runtime.value
            models[role] = overrides.get(role, overrides["default"])

    base = Runtime(providers.get("default", fallback.value))
    return providers, models, base


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


def production_delivery_context(goal: str) -> str:
    """Return the non-negotiable planning and acceptance contract for builds."""
    return f"""PRODUCTION DELIVERY CONTRACT

User goal:
{goal}

Planning requirements:
1. Inspect repository instructions, current architecture, tests, and delivery tooling before proposing work. Preserve compatible behavior and avoid rewrites unless the plan documents why one is necessary.
2. Turn the goal into explicit user flows, edge cases, failure states, accessibility and responsive requirements for UI work, and relevant security, performance, observability, migration, and rollback requirements.
3. Every issue must name its dependencies, likely files or components, observable acceptance criteria, and verification evidence. Acceptance criteria must describe behavior that a reviewer can prove, not implementation activity.
4. Plan integration and end-to-end acceptance as first-class work. Parallel issue completion is not evidence that the integrated application works.

Implementation requirements:
1. Produce complete, maintainable production code. Do not ship placeholders, mock-only paths, TODOs, disabled validation, silent exception handling, or log-only implementations.
2. Preserve user data and compatibility. Validate inputs, handle empty/loading/error/success states, avoid secrets in source or logs, and add instrumentation where failures would otherwise be invisible.
3. Add or update automated tests at the appropriate level. UI changes require keyboard/accessibility checks, responsive layouts, readable non-truncated content, and browser verification of the actual user flows.

Acceptance evidence required before success:
1. Provide a requirement-to-evidence matrix mapping every user requirement and edge case to a passing automated check or a concrete browser/manual artifact.
2. Run the repository's lint, typecheck, unit/integration tests, and production build where those commands exist. Record exact commands and results; a command that did not run is not a pass.
3. Verify the merged integration branch has the intended non-empty diff, no unresolved conflicts, no unintended files or secrets, no browser console errors, and no regressions in adjacent critical flows.
4. Independent review must reject the build when evidence is missing, text or controls are unreadable, error states are unhandled, tests are bypassed, or the integrated application does not satisfy the goal end to end.
5. Report success only after all acceptance criteria pass on the integrated result. Otherwise return a precise blocking failure and repair it within the configured fix cycles.
"""


def workflow_integration_context(
    *, scm_provider: str, source_branch: str, create_pull_request: bool, mcp_context: str
) -> str:
    """Describe operator-selected repository delivery and inherited tools."""
    delivery = (
        "Create a pull request after verification."
        if create_pull_request and scm_provider == "github"
        else (
            "Keep the verified integration branch in the build workspace, report its "
            "exact name, and state clearly that it was not published automatically."
        )
    )
    branch = source_branch or "the repository's remote default branch"
    return f"""WORKFLOW INTEGRATION

Repository provider: {scm_provider.replace('_', ' ')}
Starting branch: {branch}
Delivery: {delivery}

{mcp_context}
"""


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
        # Through the settings registry, so raising the timeout from Mission
        # Control on a slow host actually takes effect. On a Pi a healthy build
        # can sit inside a dependency install for longer than a desktop ever
        # would, and being killed as stalled is the failure that looks like a
        # bug in the factory.
        self.no_progress_timeout_s = (
            float(get_setting(self.ledger, "scheduler.no_progress_timeout_s"))
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
        scm_provider: str = "github",
        source_branch: str = "",
        create_pull_request: bool = True,
        mcp_context: str = "",
        routing_policy: dict[str, dict[str, str]] | None = None,
        build_id: str | None = None,
        fast: bool = False,
    ) -> BuildRecord:
        """Start a SWE-AF build with a capacity-chosen runtime."""
        build_id = build_id or f"build-{uuid.uuid4().hex[:10]}"

        subscriptions_only = subscription_only(self.ledger)
        planning_runtime, why = self.capacity.select(
            require_strong=require_strong_planning,
            prefer_local=not subscriptions_only,
        )
        if subscriptions_only:
            available = {
                harness
                for harness in (Runtime.CLAUDE_CODE, Runtime.CODEX)
                if self.capacity.headroom(harness).available
            }
            if not available:
                # Submitting anyway would send 400+ invocations at a harness
                # already known to refuse them, and each one would burn a full
                # agent timeout before failing. Block here, with the reason.
                blocked = "; ".join(
                    f"{harness.value}: {self.capacity.headroom(harness).reason}"
                    for harness in (Runtime.CLAUDE_CODE, Runtime.CODEX)
                )
                record = BuildRecord(
                    build_id=build_id,
                    goal=goal,
                    repo_url=repo_url,
                    runtime=Runtime.CLAUDE_CODE,
                )
                record.state = BuildState.BLOCKED
                record.error = f"no subscription harness can run this build ({blocked})"
                self.ledger.record_event(
                    "build_blocked",
                    {"reason": record.error, "mode": "subscription_only"},
                    build_id=build_id,
                )
                return record

            providers, models, runtime = subscription_role_policy(available=available)
            if len(available) == 1:
                only = next(iter(available)).value
                why = (
                    f"subscriptions-only mode; {only} is the sole harness with "
                    "headroom, so review is NOT independent of implementation"
                )
                # A degradation the operator can act on is worth a durable
                # record, not just a log line that scrolls away.
                self.ledger.record_event(
                    "build_degraded",
                    {
                        "reason": why,
                        "lost": "independent review",
                        "harness": only,
                    },
                    build_id=build_id,
                )
            else:
                why = "subscriptions-only mode; Claude implements and Codex reviews"
        else:
            runtime = Runtime.OPEN_CODE
            providers, models = hybrid_role_policy(planning_runtime)
        providers, models = apply_routing_policy(
            (
                normalize_routing_policy(
                    routing_policy, subscriptions_only=subscriptions_only
                )
                if routing_policy is not None
                else load_routing_policy(
                    self.ledger, subscriptions_only=subscriptions_only
                )
            ),
            providers=providers,
            models=models,
            claude_model=runtime_model_overrides(
                Runtime.CLAUDE_CODE, subscriptions_only=subscriptions_only
            )["default"],
            codex_model=runtime_model_overrides(
                Runtime.CODEX, subscriptions_only=subscriptions_only
            )["default"],
        )
        # An explicit operator assignment can move the `default` role, and the
        # base runtime must follow it or SWE-AF launches one harness while every
        # unassigned role expects another.
        runtime = Runtime(providers.get("default", runtime.value))
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
            # Without this the harness runs in its interactive permission mode
            # and asks a human who is not there. Claude Code answers "I need
            # permission to write to that file, please approve the Write
            # request" and produces nothing; the coder then reports its task
            # complete having changed no files, and the verifier fails because
            # it could not create its own output file.
            #
            # Not "auto", which the adapter maps to bypassPermissions. The agent
            # containers run as root, and Claude Code refuses that mode outright:
            # "--dangerously-skip-permissions cannot be used with root/sudo
            # privileges for security reasons". acceptEdits carries no such
            # restriction and was verified in the running container to allow both
            # Write and Bash. Codex is unaffected: its wrapper already runs
            # danger-full-access inside this container.
            #
            # An unattended factory cannot answer a prompt by definition, and the
            # container plus the disposable checkout are the boundary that makes
            # not asking safe.
            "permission_mode": PERMISSION_MODE,
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
            "enable_github_pr": bool(
                create_pull_request and scm_provider == "github"
            ),
        }
        if source_branch:
            config["github_pr_base"] = source_branch

        payload = {
            "goal": goal,
            "repo_url": repo_url,
            "additional_context": "\n\n".join(
                (
                    production_delivery_context(goal),
                    workflow_integration_context(
                        scm_provider=scm_provider,
                        source_branch=source_branch,
                        create_pull_request=create_pull_request,
                        mcp_context=mcp_context,
                    ),
                )
            ),
            "config": config,
        }

        if fast:
            # swe-fast has been running in the compose file all along, taking
            # 300MB of a 4GB board, and nothing ever called it. It is a single
            # pass: git init, plan the tasks, do them, verify once, open the PR.
            # No product manager, no architect, no tech lead, no sprint planner,
            # no issue writers, and no repair loop. For a change that is one
            # obvious edit, the full pipeline spends seven planning agents
            # before a line of code exists.
            #
            # Its config model forbids unknown keys and has no per-role provider
            # map, so it gets its own dict rather than a filtered copy of the
            # one above. One runtime for every role is also exactly what is
            # wanted when only one subscription has capacity.
            payload["config"] = {
                "runtime": runtime.value,
                # Same reason as the full path above: nobody is here to approve
                # a Write.
                "permission_mode": PERMISSION_MODE,
                "models": {"default": models.get("default", "")} if models.get("default") else None,
                "enable_github_pr": bool(
                    create_pull_request and scm_provider == "github"
                ),
                # The 600s default is a workstation number. A Pi installing
                # dependencies and running a test suite needs the room, and
                # being killed mid-verify is the failure that looks like a bug
                # in the factory rather than a timeout.
                "build_timeout_seconds": int(
                    os.environ.get("CTSWARM_FAST_BUILD_TIMEOUT_S", "3600")
                ),
                "task_timeout_seconds": int(
                    os.environ.get("CTSWARM_AGENT_TIMEOUT_SECONDS", "900")
                ),
                "repo_url": repo_url,
            }
            if source_branch:
                payload["config"]["github_pr_base"] = source_branch

        target = "swe-fast.build" if fast else "swe-planner.build"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                # Both details verified against the live control plane:
                # the target is dot-separated (`node.reasoner`; a slash 404s),
                # and the body must be wrapped in `input` (a bare payload is
                # rejected with "Missing required field: goal").
                response = await client.post(
                    f"{self.agentfield_url}/api/v1/execute/async/{target}",
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
                workflow_id = str(
                    body.get("run_id") or body.get("workflow_id") or ""
                )
                if workflow_id:
                    workflow_response = await client.get(
                        f"{self.agentfield_url}/api/ui/v1/workflows/"
                        f"{workflow_id}/dag?mode=lightweight"
                    )
                    if workflow_response.status_code == 200:
                        workflow = workflow_response.json()
                        timeline = workflow.get("timeline")
                        if not isinstance(timeline, list):
                            timeline = []
                        # The root execution can remain byte-for-byte unchanged
                        # while children plan, code, and review. Include only
                        # stable child state so those real transitions reset the
                        # watchdog without allowing heartbeat churn to mask a
                        # genuinely wedged workflow.
                        body["_workflow_progress"] = {
                            "status": workflow.get("workflow_status"),
                            "total_nodes": workflow.get("total_nodes"),
                            "nodes": sorted(
                                (
                                    {
                                        "execution_id": node.get("execution_id"),
                                        "reasoner_id": node.get("reasoner_id"),
                                        "status": node.get("status"),
                                    }
                                    for node in timeline
                                    if isinstance(node, dict)
                                ),
                                key=lambda node: str(node["execution_id"] or ""),
                            ),
                        }
        except (httpx.HTTPError, ValueError):
            return record

        record = update_record_from_execution(record, body)
        self._note_any_exhaustion(record)
        return record

    def _note_any_exhaustion(self, record: BuildRecord) -> None:
        """Turn "the build failed because a subscription ran out" into a fact.

        `CapacityManager.headroom` already holds a rate-limited runtime out for
        a full window, and `submit` already reroutes every role onto the other
        harness when only one has headroom. Both were unreachable: nothing in
        the product ever called `note_rate_limited`, so the ledger never learned
        that a subscription was spent and the next build walked into the same
        wall. The build's own runtime is the fallback attribution because a
        message that says only "usage limit reached" is still evidence about
        whoever was running.
        """
        text = " ".join(filter(None, (record.error, record.phase_detail)))
        runtime = rate_limit_signal(text, default=record.runtime)
        if runtime is None or runtime in record._rate_limited_seen:
            return
        record._rate_limited_seen.add(runtime)
        self.capacity.note_rate_limited(runtime, detail=text)
        self.ledger.record_event(
            "runtime_exhausted_during_build",
            {"runtime": runtime.value, "detail": text[:300]},
            build_id=record.build_id,
        )

    async def cancel_execution(self, record: BuildRecord, reason: str) -> bool:
        """Cooperatively stop the complete AgentField workflow tree.

        The scheduler owns the user-selected wall-clock deadline. AgentField's
        SDK watchdog is deliberately configured above the scheduler's maximum
        so it cannot preempt valid work. Cancelling only the root execution does
        not stop already-dispatched planners, coders, or reviewers, so resolve
        the workflow id and use AgentField's bottom-up cancel-tree endpoint.
        Fall back to per-execution cancellation for older control planes.
        """
        if not record.execution_id:
            return False
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                workflow_id = ""
                try:
                    detail = await client.get(
                        f"{self.agentfield_url}/api/v1/executions/"
                        f"{record.execution_id}"
                    )
                    if detail.status_code == 200:
                        payload = detail.json()
                        workflow_id = str(
                            payload.get("run_id")
                            or payload.get("workflow_id")
                            or ""
                        )
                except (httpx.HTTPError, ValueError, TypeError):
                    workflow_id = ""

                if workflow_id:
                    tree_response = await client.post(
                        f"{self.agentfield_url}/api/v1/workflows/"
                        f"{workflow_id}/cancel-tree",
                        json={"reason": reason},
                    )
                    if tree_response.status_code < 400:
                        return True

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

        subscriptions_only = subscription_only(self.ledger)
        if subscriptions_only:
            # No routing table exists on this host, so the panel is the two CLI
            # harnesses. They are two genuine vendor families, which is what the
            # independence rule actually asks for.
            from .backends.cli_harness import CliHarnessBackend

            backend = CliHarnessBackend()
            members = await backend.list_models()
        else:
            members = eligible_members(RoutingTable.load())
            backends = build_backends(subscriptions_only=False)
            backend = backends.get("ollama") or next(iter(backends.values()), None)

        if len(members) < 2 or backend is None:
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
        try:
            diff = _read_diff(repo_path)
            result = await convene(
                question=(
                    "Act as a strict production acceptance board. Approve only "
                    "when the integrated diff fully satisfies every stated user "
                    "flow and acceptance criterion; deterministic lint, type, test, "
                    "and build evidence is present; failure and empty states are "
                    "handled; security, accessibility, responsive behavior, data "
                    "compatibility, and operational risks relevant to the change "
                    "are addressed; and there are no placeholders, bypassed checks, "
                    "unreadable UI, or unverified claims. Any missing evidence or "
                    "material doubt is a rejection. "
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
