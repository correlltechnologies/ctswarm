"""Durability and concurrency tests for the always-on build scheduler."""

from __future__ import annotations

import asyncio

from ctswarm.capacity import Runtime
from ctswarm.ledger import Ledger
from ctswarm.orchestrator import BuildRecord, BuildState
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


def _request(name: str) -> BuildRequest:
    return BuildRequest(
        goal=f"build {name}",
        repo_url=f"https://example.invalid/{name}",
    )


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
