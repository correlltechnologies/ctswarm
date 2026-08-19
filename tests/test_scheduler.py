"""Durability and concurrency tests for the always-on build scheduler."""

from __future__ import annotations

import asyncio
import time

from ctswarm.capacity import Runtime
from ctswarm.ledger import Ledger
from ctswarm.orchestrator import BuildRecord, BuildState
from ctswarm.routing_config import DEFAULT_ROUTING_POLICY, save_routing_policy
from ctswarm.scheduler import BuildRequest, BuildScheduler


class _Notifier:
    async def post(self, _record) -> bool:
        return False


class _ControlledOrchestrator:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.submitted: list[str] = []
        self.started: list[str] = []
        self.release = asyncio.Event()
        self.stopped: set[str] = set()

    def control_state(self, build_id: str) -> str:
        return "stopped" if build_id in self.stopped else "running"

    async def submit(self, *, build_id: str, goal: str, repo_url: str, **_kwargs):
        self.submitted.append(build_id)
        return BuildRecord(
            build_id=build_id,
            goal=goal,
            repo_url=repo_url,
            runtime=Runtime.OPEN_CODE,
            state=BuildState.PLANNING,
            execution_id=f"exec-{build_id}",
        )

    async def run_until_done(self, record: BuildRecord, **_kwargs):
        self.started.append(record.build_id)
        await self.release.wait()
        record.state = BuildState.COMPLETE
        return record


class _RestartingOrchestrator(_ControlledOrchestrator):
    def __init__(self, ledger: Ledger) -> None:
        super().__init__(ledger)
        self.runs = 0

    async def run_until_done(self, record: BuildRecord, **_kwargs):
        self.started.append(record.build_id)
        self.runs += 1
        if self.runs == 1:
            record.state = BuildState.STOPPED
            record.error = "cancelled_by_control_plane"
        else:
            record.state = BuildState.COMPLETE
        return record


def _request(name: str, tier: str = "full") -> BuildRequest:
    """A queue item for the AgentField path.

    Explicitly the full tier: these tests are about submission, polling,
    restart recovery, and cancellation, all of which only exist on that path.
    The scheduler's default is `scoped`, which runs in-process and has none of
    them.
    """
    return BuildRequest(
        goal=f"build {name}",
        repo_url=f"https://example.invalid/{name}",
        tier=tier,
    )


def test_recent_calls_returns_newest_concrete_inference(tmp_path) -> None:
    ledger = Ledger(tmp_path / "scheduler.db")
    ledger.record_call(
        build_id="build-one",
        role="coder",
        tier="med",
        virtual_model="ctswarm/med",
        backend="ollama",
        model_ref="qwen3.5:4b",
        ok=True,
        prompt_tokens=12,
        output_tokens=34,
    )
    ledger.record_call(
        build_id="build-two",
        role="reviewer",
        tier="high",
        virtual_model="ctswarm/high",
        backend="openrouter",
        model_ref="review-model",
        ok=False,
        failure_kind="timeout",
    )

    assert [row["model_ref"] for row in ledger.recent_calls(limit=2)] == [
        "review-model",
        "qwen3.5:4b",
    ]
    selected = ledger.recent_calls(build_id="build-one")
    assert len(selected) == 1
    assert selected[0]["backend"] == "ollama"
    assert selected[0]["output_tokens"] == 34


def test_queue_survives_a_new_scheduler_instance(tmp_path) -> None:
    ledger = Ledger(tmp_path / "scheduler.db")
    first = BuildScheduler(
        ledger=ledger,
        orchestrator=_ControlledOrchestrator(ledger),
        notifier=_Notifier(),
    )
    build_id = first.enqueue(_request("durable"), build_id="build-durable")

    second = BuildScheduler(
        ledger=ledger,
        orchestrator=_ControlledOrchestrator(ledger),
        notifier=_Notifier(),
    )

    assert build_id == "build-durable"
    assert second.snapshot(build_id)["state"] == "queued"
    assert [item[0] for item in second.pending_requests()] == [build_id]


def test_enqueue_snapshots_routing_policy_before_dispatch(monkeypatch, tmp_path) -> None:
    # Pinning a lane to a local model is a hybrid-host policy; a
    # subscriptions-only host would legitimately refuse it.
    monkeypatch.setenv("CTSWARM_EXECUTION_MODE", "hybrid")
    ledger = Ledger(tmp_path / "scheduler.db")
    selected = {
        **DEFAULT_ROUTING_POLICY,
        "planning": {"target": "codex", "model": ""},
        "implementation": {"target": "ollama", "model": "qwen3.5:9b"},
    }
    save_routing_policy(ledger, selected)
    scheduler = BuildScheduler(
        ledger=ledger,
        orchestrator=_ControlledOrchestrator(ledger),
        notifier=_Notifier(),
    )

    build_id = scheduler.enqueue(_request("frozen"), build_id="build-frozen")
    save_routing_policy(ledger, DEFAULT_ROUTING_POLICY)

    pending = dict(scheduler.pending_requests())[build_id]
    assert pending.routing_policy == selected
    assert scheduler.snapshot(build_id)["routing_policy"] == selected


async def test_inflight_execution_is_resumed_without_duplicate_submission(
    tmp_path,
) -> None:
    ledger = Ledger(tmp_path / "scheduler.db")
    orchestrator = _ControlledOrchestrator(ledger)
    scheduler = BuildScheduler(
        ledger=ledger,
        orchestrator=orchestrator,
        notifier=_Notifier(),
        poll_interval_s=0.01,
    )
    build_id = scheduler.enqueue(_request("resume"), build_id="build-resume")
    ledger.record_event(
        "build_submitted",
        {
            "goal": "build resume",
            "repo_url": "https://example.invalid/resume",
            "runtime": "claude_code",
        },
        build_id=build_id,
    )
    ledger.record_event(
        "build_started",
        {"execution_id": "exec-existing"},
        build_id=build_id,
    )

    await scheduler.run_once()
    await asyncio.sleep(0)

    assert orchestrator.submitted == []
    assert orchestrator.started == [build_id]
    assert scheduler.snapshot(build_id)["execution_id"] == "exec-existing"

    orchestrator.release.set()
    await asyncio.wait_for(scheduler._tasks[build_id], timeout=1)  # noqa: SLF001
    await scheduler.run_once()
    assert scheduler.snapshot(build_id)["state"] == "complete"
    await scheduler.close()


async def test_active_snapshot_uses_live_watchdog_progress(tmp_path) -> None:
    ledger = Ledger(tmp_path / "scheduler.db")
    orchestrator = _ControlledOrchestrator(ledger)
    scheduler = BuildScheduler(
        ledger=ledger,
        orchestrator=orchestrator,
        notifier=_Notifier(),
        poll_interval_s=0.01,
    )
    build_id = scheduler.enqueue(_request("live"), build_id="build-live")

    await scheduler.run_once()
    await asyncio.sleep(0)
    live = scheduler._active_records[build_id]  # noqa: SLF001
    live.phase_detail = "architect is refining the implementation plan"
    live.last_progress_at = time.time() - 12

    snapshot = scheduler.snapshot(build_id)
    assert snapshot["phase_detail"] == live.phase_detail
    assert snapshot["stalled_s"] >= 11

    orchestrator.release.set()
    await asyncio.wait_for(scheduler._tasks[build_id], timeout=1)  # noqa: SLF001
    await scheduler.run_once()
    assert build_id not in scheduler._active_records  # noqa: SLF001
    await scheduler.close()


async def test_scheduler_enforces_one_active_build(tmp_path) -> None:
    ledger = Ledger(tmp_path / "scheduler.db")
    orchestrator = _ControlledOrchestrator(ledger)
    scheduler = BuildScheduler(
        ledger=ledger,
        orchestrator=orchestrator,
        notifier=_Notifier(),
        max_concurrent=1,
        poll_interval_s=0.01,
    )
    first = scheduler.enqueue(_request("first"), build_id="build-first")
    second = scheduler.enqueue(_request("second"), build_id="build-second")

    await scheduler.run_once()
    await asyncio.sleep(0)

    assert orchestrator.started == [first]
    assert scheduler.health() == {
        "ok": True,
        "queued": 1,
        "active": 1,
        "max_concurrent": 1,
    }

    orchestrator.release.set()
    await asyncio.wait_for(scheduler._tasks[first], timeout=1)  # noqa: SLF001
    await scheduler.run_once()
    await asyncio.sleep(0)

    assert orchestrator.started == [first, second]
    await asyncio.wait_for(scheduler._tasks[second], timeout=1)  # noqa: SLF001
    await scheduler.run_once()
    assert scheduler.snapshot(first)["state"] == "complete"
    assert scheduler.snapshot(second)["state"] == "complete"
    await scheduler.close()


async def test_stopped_queued_build_finishes_without_waiting_for_a_slot(
    tmp_path,
) -> None:
    ledger = Ledger(tmp_path / "scheduler.db")
    orchestrator = _ControlledOrchestrator(ledger)
    scheduler = BuildScheduler(
        ledger=ledger,
        orchestrator=orchestrator,
        notifier=_Notifier(),
        max_concurrent=1,
        poll_interval_s=0.01,
    )
    active = scheduler.enqueue(_request("active"), build_id="build-active")
    stopped = scheduler.enqueue(_request("stopped"), build_id="build-stopped")

    await scheduler.run_once()
    await asyncio.sleep(0)
    assert orchestrator.started == [active]

    orchestrator.stopped.add(stopped)
    await scheduler.run_once()

    assert scheduler.snapshot(stopped)["state"] == "stopped"
    assert scheduler.snapshot(stopped)["execution_id"] == ""
    assert orchestrator.started == [active]
    orchestrator.release.set()
    await asyncio.wait_for(scheduler._tasks[active], timeout=1)  # noqa: SLF001
    await scheduler.close()


async def test_control_plane_cancellation_is_retried_automatically(tmp_path) -> None:
    ledger = Ledger(tmp_path / "scheduler.db")
    orchestrator = _RestartingOrchestrator(ledger)
    scheduler = BuildScheduler(
        ledger=ledger,
        orchestrator=orchestrator,
        notifier=_Notifier(),
        poll_interval_s=0.0,
    )
    build_id = scheduler.enqueue(_request("restart"), build_id="build-restart")

    await scheduler.run_once()
    await asyncio.wait_for(scheduler._tasks[build_id], timeout=1)  # noqa: SLF001
    await scheduler.run_once()

    assert orchestrator.submitted == [build_id, build_id]
    assert orchestrator.started == [build_id, build_id]
    assert scheduler.snapshot(build_id)["state"] == "complete"
    retries = ledger.events(kind="build_infrastructure_retry", build_id=build_id)
    assert len(retries) == 1
    await scheduler.close()


# ---------------------------------------------------------------------------
# tier routing
# ---------------------------------------------------------------------------


class _TierRecordingOrchestrator(_ControlledOrchestrator):
    """Records which path the scheduler chose for each queue item."""

    def __init__(self, ledger: Ledger) -> None:
        super().__init__(ledger)
        self.scoped: list[str] = []

    async def run_scoped(self, *, build_id: str, goal: str, repo_url: str, **_kwargs):
        self.scoped.append(build_id)
        return BuildRecord(
            build_id=build_id,
            goal=goal,
            repo_url=repo_url,
            runtime=Runtime.CLAUDE_CODE,
            state=BuildState.COMPLETE,
            pr_url="https://example.invalid/pull/1",
        )


def test_a_queue_item_defaults_to_the_scoped_tier() -> None:
    """The default is the cheap path, which is the entire point of having one."""
    assert BuildRequest(goal="add a button", repo_url="r").tier == "scoped"


def test_the_legacy_fast_flag_still_selects_the_fast_tier() -> None:
    assert BuildRequest(goal="g", repo_url="r", fast=True).tier == "fast"
    # An explicit tier is the caller's stated intent and outranks the old flag.
    assert BuildRequest(goal="g", repo_url="r", fast=True, tier="full").tier == "full"


async def test_scoped_builds_never_reach_agentfield(tmp_path) -> None:
    ledger = Ledger(tmp_path / "scheduler.db")
    orchestrator = _TierRecordingOrchestrator(ledger)
    scheduler = BuildScheduler(
        ledger=ledger,
        orchestrator=orchestrator,
        notifier=_Notifier(),
        poll_interval_s=0.01,
    )
    build_id = scheduler.enqueue(
        _request("scoped", tier="scoped"), build_id="build-scoped"
    )

    await scheduler.run_once()
    await asyncio.wait_for(scheduler._tasks[build_id], timeout=1)  # noqa: SLF001
    await scheduler.run_once()

    assert orchestrator.scoped == [build_id]
    # No submission, no polling, no execution to recover.
    assert orchestrator.submitted == []
    assert orchestrator.started == []
    snapshot = scheduler.snapshot(build_id)
    assert snapshot["state"] == "complete"
    assert snapshot["pr_url"] == "https://example.invalid/pull/1"
    await scheduler.close()


async def test_full_tier_still_goes_through_submission(tmp_path) -> None:
    ledger = Ledger(tmp_path / "scheduler.db")
    orchestrator = _TierRecordingOrchestrator(ledger)
    scheduler = BuildScheduler(
        ledger=ledger,
        orchestrator=orchestrator,
        notifier=_Notifier(),
        poll_interval_s=0.01,
    )
    build_id = scheduler.enqueue(_request("full", tier="full"), build_id="build-full")

    await scheduler.run_once()
    await asyncio.sleep(0)
    assert orchestrator.submitted == [build_id]
    assert orchestrator.scoped == []

    orchestrator.release.set()
    await asyncio.wait_for(scheduler._tasks[build_id], timeout=1)  # noqa: SLF001
    await scheduler.close()


def test_snapshot_reports_the_tier_a_build_ran_on(tmp_path) -> None:
    ledger = Ledger(tmp_path / "scheduler.db")
    scheduler = BuildScheduler(
        ledger=ledger,
        orchestrator=_ControlledOrchestrator(ledger),
        notifier=_Notifier(),
    )
    scoped = scheduler.enqueue(_request("s", tier="scoped"), build_id="build-s")
    full = scheduler.enqueue(_request("f", tier="full"), build_id="build-f")

    assert scheduler.snapshot(scoped)["tier"] == "scoped"
    assert scheduler.snapshot(full)["tier"] == "full"


def test_a_build_from_before_tiers_existed_reads_as_the_full_factory(tmp_path) -> None:
    """Otherwise the ledger understates what older builds actually cost."""
    ledger = Ledger(tmp_path / "scheduler.db")
    scheduler = BuildScheduler(
        ledger=ledger,
        orchestrator=_ControlledOrchestrator(ledger),
        notifier=_Notifier(),
    )
    for build_id, detail in (
        ("build-old", {"goal": "g", "repo_url": "r"}),
        ("build-old-fast", {"goal": "g", "repo_url": "r", "fast": True}),
    ):
        ledger.record_event("build_enqueued", detail, build_id=build_id)

    assert scheduler.snapshot("build-old")["tier"] == "full"
    assert scheduler.snapshot("build-old-fast")["tier"] == "fast"


def test_mission_control_defaults_to_the_scoped_tier() -> None:
    """The phone is the client that most needs the cheap path to be the default."""
    from ctswarm.scheduler import SwarmLaunchRequest

    assert SwarmLaunchRequest(goal="add a button").tier == "scoped"
    assert SwarmLaunchRequest(goal="build it all", tier="full").tier == "full"
