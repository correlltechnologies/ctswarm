"""Bench runner: measure models, write the routing table.

Design constraints that come from the failure modes actually observed:

- **Hard per-task timeouts.** A model that never returns must be scored as a
  timeout, not allowed to wedge the harness. This was not hypothetical: a local
  model here accepted a tool-calling request and never completed it.
- **Sequential per model, never concurrent across models.** Two models cannot be
  resident in 12GB at once. Running them in parallel measures swap thrash rather
  than model quality.
- **Cancellation measured explicitly.** SWE-AF's bounded retries and replanning
  require aborting in-flight work. A model whose server wedges on abort breaks
  the control loop even if every other score is perfect.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from ..backends import Backend
from ..backends.base import ChatRequest
from ..router.policy import BenchScore, RoutingTable
from .suite import Task, build_suite


@dataclass
class TaskResult:
    """Outcome of one task attempt."""

    task: str
    category: str
    passed: bool
    detail: str
    latency_ms: int
    output_tokens: int = 0
    failure_kind: Optional[str] = None


@dataclass
class ModelResult:
    """All measurements for one model."""

    model_ref: str
    backend: str
    results: list[TaskResult] = field(default_factory=list)
    cancel_clean: bool = False
    cancel_detail: str = ""
    max_context_ok: int = 0
    tokens_per_s: float = 0.0
    error: Optional[str] = None

    def rate(self, category: str) -> float:
        """Pass rate within a category, or 0.0 when the category never ran.

        Zero rather than 1.0 for an unrun category is deliberate: an unmeasured
        model must never look eligible, because the router treats these as hard
        gates.
        """
        subset = [r for r in self.results if r.category == category]
        if not subset:
            return 0.0
        return sum(1 for r in subset if r.passed) / len(subset)

    @property
    def p50_latency_ms(self) -> float:
        latencies = sorted(r.latency_ms for r in self.results if r.passed)
        return float(latencies[len(latencies) // 2]) if latencies else 0.0

    def to_score(self) -> BenchScore:
        return BenchScore(
            model_ref=self.model_ref,
            backend=self.backend,
            tool_call_rate=self.rate("tool_call"),
            schema_rate=self.rate("schema"),
            long_context_rate=self.rate("long_context"),
            instruction_rate=self.rate("instruction"),
            cancel_clean=self.cancel_clean,
            p50_latency_ms=self.p50_latency_ms,
            tokens_per_s=self.tokens_per_s,
            max_context_ok=self.max_context_ok,
            samples=len(self.results),
        )

    def summary(self) -> dict:
        return {
            "model_ref": self.model_ref,
            "backend": self.backend,
            "tool_call": round(self.rate("tool_call"), 3),
            "schema": round(self.rate("schema"), 3),
            "long_context": round(self.rate("long_context"), 3),
            "instruction": round(self.rate("instruction"), 3),
            "cancel_clean": self.cancel_clean,
            "p50_latency_ms": self.p50_latency_ms,
            "tokens_per_s": round(self.tokens_per_s, 2),
            "eligible": self.to_score().eligible_for_agent_roles,
            "error": self.error,
            "failures": [
                {"task": r.task, "detail": r.detail, "kind": r.failure_kind}
                for r in self.results
                if not r.passed
            ],
        }


async def run_task(backend: Backend, model_ref: str, task: Task) -> TaskResult:
    """Run one task under a hard timeout. Never raises."""
    request = ChatRequest(
        messages=task.messages,
        model=model_ref,
        tools=task.tools,
        temperature=0.0,
        max_tokens=task.max_tokens,
        response_format=task.response_format,
    )

    started = time.perf_counter()
    try:
        response = await asyncio.wait_for(
            backend.chat(request, model_ref), timeout=task.timeout_s
        )
    except asyncio.TimeoutError:
        # The observed failure mode for at least one local model: accepts the
        # request, never completes. Scored, not tolerated.
        return TaskResult(
            task=task.name,
            category=task.category,
            passed=False,
            detail=f"timed out after {task.timeout_s:.0f}s",
            latency_ms=int(task.timeout_s * 1000),
            failure_kind="timeout",
        )
    except Exception as exc:  # noqa: BLE001 - bench must survive any backend bug
        return TaskResult(
            task=task.name,
            category=task.category,
            passed=False,
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=int((time.perf_counter() - started) * 1000),
            failure_kind="harness_error",
        )

    if not response.ok:
        return TaskResult(
            task=task.name,
            category=task.category,
            passed=False,
            detail=response.error_detail[:200] or "backend reported failure",
            latency_ms=response.latency_ms,
            output_tokens=response.output_tokens,
            failure_kind=response.failure_kind,
        )

    passed, detail = task.check(response.body) if task.check else (True, "no checker")
    return TaskResult(
        task=task.name,
        category=task.category,
        passed=passed,
        detail=detail,
        latency_ms=response.latency_ms,
        output_tokens=response.output_tokens,
    )


async def measure_cancellation(backend: Backend, model_ref: str) -> tuple[bool, str]:
    """Start a long generation, abort it, and confirm the backend stays healthy.

    Passing requires two things: the cancel itself unwinds promptly, and the
    backend still answers afterward. A server that survives cancel but wedges its
    model slot fails the second condition, which is the one that matters for a
    control loop that replans mid-build.
    """
    request = ChatRequest(
        messages=[
            {
                "role": "user",
                "content": "Write an exhaustive 5000-word technical specification "
                "for a distributed job queue. Be extremely detailed.",
            }
        ],
        model=model_ref,
        max_tokens=4096,
        temperature=0.0,
    )

    task = asyncio.create_task(backend.chat(request, model_ref))
    # Let generation genuinely start before aborting; cancelling before the
    # request is on the wire would measure nothing.
    await asyncio.sleep(4.0)
    task.cancel()

    try:
        await asyncio.wait_for(asyncio.shield(_swallow(task)), timeout=20.0)
    except asyncio.TimeoutError:
        return False, "cancel did not unwind within 20s"

    # Recovery check: a trivial request must succeed shortly after the abort.
    probe = ChatRequest(
        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
        model=model_ref,
        max_tokens=16,
        temperature=0.0,
    )
    try:
        response = await asyncio.wait_for(backend.chat(probe, model_ref), timeout=90.0)
    except asyncio.TimeoutError:
        return False, "backend unresponsive after cancel"
    except Exception as exc:  # noqa: BLE001
        return False, f"post-cancel probe raised {type(exc).__name__}: {exc}"

    if not response.ok:
        return False, f"post-cancel probe failed: {response.failure_kind}"
    return True, "ok"


async def _swallow(task: asyncio.Task) -> None:
    """Await a cancelled task, absorbing the expected CancelledError."""
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001
        return


async def measure_throughput(backend: Backend, model_ref: str) -> float:
    """Output tokens per second on a short, fixed generation."""
    request = ChatRequest(
        messages=[
            {
                "role": "user",
                "content": "Write a Python function that reverses a linked list. "
                "Include a docstring. Code only.",
            }
        ],
        model=model_ref,
        max_tokens=400,
        temperature=0.0,
    )
    try:
        response = await asyncio.wait_for(backend.chat(request, model_ref), timeout=180.0)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        return 0.0
    if not response.ok or response.latency_ms <= 0 or response.output_tokens <= 0:
        return 0.0
    return response.output_tokens / (response.latency_ms / 1000.0)


async def bench_model(
    backend: Backend,
    model_ref: str,
    *,
    tasks: Optional[tuple[Task, ...]] = None,
    on_progress=None,
) -> ModelResult:
    """Run the full suite against one model, sequentially."""
    tasks = tasks or build_suite()
    result = ModelResult(model_ref=model_ref, backend=backend.name)

    for task in tasks:
        outcome = await run_task(backend, model_ref, task)
        result.results.append(outcome)
        if on_progress:
            on_progress(model_ref, outcome)
        # A model that times out on the very first task is broken enough that
        # spending another ten minutes proving it is waste. Record and move on.
        if (
            outcome.failure_kind == "timeout"
            and len(result.results) == 1
        ):
            result.error = "timed out on first task; remaining tasks skipped"
            break

    if result.error is None:
        result.cancel_clean, result.cancel_detail = await measure_cancellation(
            backend, model_ref
        )
        result.tokens_per_s = await measure_throughput(backend, model_ref)
        needle = next(
            (r for r in result.results if r.task == "long_context_needle"), None
        )
        if needle and needle.passed:
            context_chars = next(
                (
                    t.metadata.get("context_chars", 0)
                    for t in tasks
                    if t.name == "long_context_needle"
                ),
                0,
            )
            result.max_context_ok = int(context_chars / 3.0)

    return result


async def bench_all(
    backend: Backend,
    model_refs: list[str],
    *,
    tasks: Optional[tuple[Task, ...]] = None,
    on_progress=None,
    on_model_done=None,
) -> list[ModelResult]:
    """Bench several models one at a time.

    Strictly sequential. Concurrency here would measure VRAM contention rather
    than model behavior, and would make results non-reproducible.
    """
    results: list[ModelResult] = []
    for model_ref in model_refs:
        # A wedged runner blocks the shared inference queue, so every model
        # benched after it would time out and be recorded as broken. Those
        # numbers would be worse than no numbers: they would permanently gate
        # good models out of the routing table on evidence of someone else's
        # fault. Refuse to measure rather than record a libel.
        blocked = await _blocked_reason(backend, model_ref)
        if blocked:
            results.append(
                ModelResult(model_ref=model_ref, backend=backend.name, error=blocked)
            )
            if on_model_done:
                on_model_done(results[-1])
            continue

        result = await bench_model(
            backend, model_ref, tasks=tasks, on_progress=on_progress
        )
        results.append(result)
        if on_model_done:
            on_model_done(result)
    return results


async def _blocked_reason(backend: Backend, model_ref: str) -> Optional[str]:
    """Why this model cannot be fairly measured right now, or None."""
    wedged_fn = getattr(backend, "wedged_models", None)
    if wedged_fn:
        wedged = await wedged_fn()
        # A model that wedged itself is genuinely broken and worth recording as
        # such. A model waiting behind someone else's wedge is not.
        others = wedged - {model_ref}
        if others:
            return (
                f"backend blocked by wedged runner(s) {sorted(others)}; "
                "restart the inference server before benching"
            )
        if model_ref in wedged:
            return "this model's runner is wedged and will not terminate"

    if not await backend.probe_generation(model_ref, timeout_s=30.0):
        return "backend did not complete a trivial generation within 30s"
    return None


def write_results(
    results: list[ModelResult],
    *,
    routing_path: str | Path = "bench/results/routing.json",
    detail_path: str | Path = "bench/results/detail.json",
) -> RoutingTable:
    """Persist measurements and produce the routing table."""
    table = RoutingTable({r.model_ref: r.to_score() for r in results})
    table.save(routing_path)

    detail_path = Path(detail_path)
    detail_path.parent.mkdir(parents=True, exist_ok=True)
    detail_path.write_text(
        json.dumps(
            {
                "generated_at": time.time(),
                "models": [
                    {**r.summary(), "raw": [asdict(t) for t in r.results]}
                    for r in results
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return table
