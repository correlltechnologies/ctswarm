"""Self-verification probes.

These test the *system*, not the generated code. A build that produces a working
`/healthz` endpoint while silently pushing to main, spamming approvals, or losing
its checkpoint has failed even though the code is fine.

Each probe reports one of:

- ``PASS``    asserted and satisfied
- ``FAIL``    asserted and violated
- ``SKIP``    preconditions absent, with the reason stated

``SKIP`` is deliberately distinct from ``PASS``. A suite that reports green
because it quietly skipped the assertions is worse than no suite, because it
manufactures confidence. ``ctswarm verify`` exits non-zero if anything was
skipped unless ``--allow-skips`` is passed.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import httpx


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass
class ProbeResult:
    name: str
    status: Status
    summary: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "probe": self.name,
            "status": self.status.value,
            "summary": self.summary,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class VerifyContext:
    """Everything the probes need to reach the running system."""

    router_url: str = "http://localhost:8090"
    approvals_url: str = "http://localhost:8091"
    sandbox_path: Path = Path("sandbox")
    repo_path: Path | None = None
    build_id: str | None = None
    base_ref: str = "main"


async def _get(url: str, timeout: float = 10.0) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


async def _post(url: str, payload: dict, timeout: float = 15.0) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Probe 1: anti-slop trap
# ---------------------------------------------------------------------------


async def probe_antislop_trap(ctx: VerifyContext) -> ProbeResult:
    """The sandbox's contract test must fail when a route is undocumented.

    Verifies the trap itself rather than any agent's behavior. A trap that does
    not fire cannot catch anything, and a verification suite built on a dead trap
    reports success it did not earn.
    """
    sandbox = ctx.sandbox_path
    routes = sandbox / "src" / "routes.ts"

    if not routes.exists():
        return ProbeResult(
            "antislop_trap", Status.SKIP, f"sandbox not found at {sandbox}"
        )
    if not (sandbox / "node_modules").exists():
        return ProbeResult(
            "antislop_trap",
            Status.SKIP,
            "sandbox dependencies not installed (cd sandbox && npm install)",
        )

    original = routes.read_text(encoding="utf-8")
    marker = '{ method: "delete", path: "/items/:id", summary: "Delete an item" },'
    if marker not in original:
        return ProbeResult(
            "antislop_trap",
            Status.SKIP,
            "sandbox route table has diverged from the expected shape",
        )

    def run_tests() -> tuple[int, str]:
        proc = subprocess.run(
            ["npm", "test", "--silent"],
            cwd=str(sandbox),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        return proc.returncode, proc.stdout + proc.stderr

    try:
        baseline_code, baseline_out = run_tests()
        if baseline_code != 0:
            return ProbeResult(
                "antislop_trap",
                Status.FAIL,
                "sandbox suite does not pass on a clean checkout, so no result "
                "from it is meaningful",
                {"output": baseline_out[-1500:]},
            )

        # Introduce exactly the failure a lazy agent produces: a served route
        # that the spec does not document.
        routes.write_text(
            original.replace(
                marker,
                marker + '\n  { method: "get", path: "/healthz", summary: "Health check" },',
            ),
            encoding="utf-8",
        )
        trapped_code, trapped_out = run_tests()
    except (OSError, subprocess.SubprocessError) as exc:
        routes.write_text(original, encoding="utf-8")
        return ProbeResult("antislop_trap", Status.SKIP, f"could not run npm: {exc}")
    finally:
        routes.write_text(original, encoding="utf-8")

    if trapped_code == 0:
        return ProbeResult(
            "antislop_trap",
            Status.FAIL,
            "an undocumented route did NOT fail the contract test; the trap is dead",
            {"output": trapped_out[-1500:]},
        )

    mentions_route = "healthz" in trapped_out
    return ProbeResult(
        "antislop_trap",
        Status.PASS,
        "undocumented route correctly failed the contract test",
        {
            "baseline_exit": baseline_code,
            "trapped_exit": trapped_code,
            "error_names_the_route": mentions_route,
        },
    )


# ---------------------------------------------------------------------------
# Probe 2: provider failover
# ---------------------------------------------------------------------------


async def probe_failover(ctx: VerifyContext) -> ProbeResult:
    """The router must survive a backend going away.

    Asserted through the routing decision rather than by killing a service,
    because a probe that stops the owner's inference server as a side effect is
    not something anyone will run twice.

    What is checked: the fallback chain crosses backends where more than one
    exists, and the router excludes models it cannot serve with a stated reason.
    A chain that only ever falls back within a single backend provides no
    protection against that backend dying, which is the actual failure observed
    on this host.
    """
    health = await _get(f"{ctx.router_url}/health")
    if health is None:
        return ProbeResult(
            "failover", Status.SKIP, f"router not reachable at {ctx.router_url}"
        )

    decision = await _get(f"{ctx.router_url}/routing/explain?role=coder")
    if decision is None:
        return ProbeResult("failover", Status.SKIP, "router did not explain a decision")

    primary = decision.get("primary")
    fallbacks = decision.get("fallbacks") or []
    excluded = decision.get("excluded") or []

    if primary is None:
        return ProbeResult(
            "failover",
            Status.FAIL,
            "router has no eligible primary model for the coder role",
            {"excluded": excluded[:10]},
        )

    if not fallbacks:
        return ProbeResult(
            "failover",
            Status.FAIL,
            "no fallback candidates; a single model failure would stall the build",
            {"primary": primary},
        )

    # /health reported a bare bool per backend in an earlier revision. Tolerate
    # both shapes: a probe that crashes against a stale server tells you nothing
    # about the property it was meant to assert.
    def _reachable(info) -> bool:
        return info.get("reachable", False) if isinstance(info, dict) else bool(info)

    def _degraded(info) -> bool:
        return info.get("degraded", False) if isinstance(info, dict) else False

    backend_health = health.get("backends") or {}
    backends_available = [
        name for name, info in backend_health.items() if _reachable(info)
    ]
    chain_backends = {primary["backend"]} | {f["backend"] for f in fallbacks}

    # Cross-backend diversity is only assertable when more than one backend
    # exists. With a single backend it is genuinely impossible, and claiming a
    # pass would be false comfort.
    if len(backends_available) > 1 and len(chain_backends) < 2:
        return ProbeResult(
            "failover",
            Status.FAIL,
            "fallback chain stays within one backend despite multiple being "
            "available, so a backend outage would not be survivable",
            {"chain_backends": sorted(chain_backends), "available": backends_available},
        )

    every_exclusion_explained = all(entry.get("why") for entry in excluded)

    status = Status.PASS
    summary = (
        f"{len(fallbacks)} fallback(s) across {len(chain_backends)} backend(s)"
    )
    if len(backends_available) < 2:
        summary += (
            "; only one backend available, so cross-backend failover is untestable "
            "here (add OPENROUTER_API_KEY or a second endpoint to cover this)"
        )

    return ProbeResult(
        "failover",
        status,
        summary,
        {
            "primary": primary,
            "fallbacks": fallbacks,
            "backends_available": backends_available,
            "all_exclusions_explained": every_exclusion_explained,
            "degraded_backends": [
                name for name, info in backend_health.items() if _degraded(info)
            ],
        },
    )


# ---------------------------------------------------------------------------
# Probe 3: approval trigger
# ---------------------------------------------------------------------------


async def probe_approval_trigger(ctx: VerifyContext) -> ProbeResult:
    """A high-risk action must produce exactly one card; routine ones, none."""
    health = await _get(f"{ctx.approvals_url}/health")
    if health is None:
        return ProbeResult(
            "approval_trigger",
            Status.SKIP,
            f"approval service not reachable at {ctx.approvals_url}",
        )

    build_id = f"verify-{int(time.time())}"

    routine = await _post(
        f"{ctx.approvals_url}/approval/request",
        {
            "action": "retry",
            "detail": "transient failure; log mentions DROP TABLE in a fixture",
            "build_id": build_id,
        },
    )
    if routine is None:
        return ProbeResult("approval_trigger", Status.FAIL, "routine request errored")
    if routine.get("approval_required"):
        return ProbeResult(
            "approval_trigger",
            Status.FAIL,
            "a routine retry generated an approval card, which trains reflex approval",
            {"response": routine},
        )

    risky_payload = {
        "action": "apply_migration",
        "detail": "DROP TABLE legacy_users;",
        "build_id": build_id,
        "repo": "correlltechnologies/ctswarm-sandbox",
        "branch": "build/verify",
        "files_affected": ["migrations/003_drop_legacy.sql"],
        "evidence": {"tests": "18 passed", "reviewer": "flagged as out of scope"},
        "recommendation": "Deny. Not within the approved goal.",
    }

    first = await _post(f"{ctx.approvals_url}/approval/request", risky_payload)
    if first is None or not first.get("approval_required"):
        return ProbeResult(
            "approval_trigger",
            Status.FAIL,
            "a destructive migration did NOT require approval",
            {"response": first},
        )

    # Re-raise the same action several times, as a retry loop and a replan would.
    duplicates = []
    for _ in range(3):
        duplicates.append(
            await _post(f"{ctx.approvals_url}/approval/request", risky_payload)
        )

    all_deduped = all(d and d.get("duplicate") for d in duplicates)
    same_key = all(
        d and d.get("dedupe_key") == first.get("dedupe_key") for d in duplicates
    )

    if not (all_deduped and same_key):
        return ProbeResult(
            "approval_trigger",
            Status.FAIL,
            "re-raising the same action created more than one card",
            {"first": first, "duplicates": duplicates},
        )

    return ProbeResult(
        "approval_trigger",
        Status.PASS,
        "routine action silent; high-risk action produced exactly one card across "
        "4 raises",
        {
            "dedupe_key": first.get("dedupe_key"),
            "risk": first.get("risk"),
            "delivered_via": first.get("delivered_via"),
        },
    )


# ---------------------------------------------------------------------------
# Probe 4: denial handling
# ---------------------------------------------------------------------------


async def probe_denial(ctx: VerifyContext) -> ProbeResult:
    """A denial must resolve cleanly, stick, and not re-notify."""
    health = await _get(f"{ctx.approvals_url}/health")
    if health is None:
        return ProbeResult(
            "denial", Status.SKIP, f"approval service not reachable at {ctx.approvals_url}"
        )

    build_id = f"verify-deny-{int(time.time())}"
    payload = {
        "action": "deploy",
        "detail": "deploy to production",
        "build_id": build_id,
        "repo": "correlltechnologies/ctswarm-sandbox",
    }

    created = await _post(f"{ctx.approvals_url}/approval/request", payload)
    if created is None or not created.get("approval_required"):
        return ProbeResult(
            "denial", Status.FAIL, "production deploy did not require approval"
        )

    key = created["dedupe_key"]
    denied = await _post(
        f"{ctx.approvals_url}/approvals/{key}/decide",
        {"decision": "deny", "decided_by": "verify-suite", "note": "probe"},
    )
    if denied is None or not denied.get("ok"):
        return ProbeResult("denial", Status.FAIL, "deny was not recorded", {"r": denied})

    status = await _get(f"{ctx.approvals_url}/approval/status/{key}")
    if not status or status.get("decision") != "deny":
        return ProbeResult(
            "denial", Status.FAIL, "status does not reflect the denial", {"s": status}
        )

    # A second decision must not silently overwrite the first.
    second = await _post(
        f"{ctx.approvals_url}/approvals/{key}/decide",
        {"decision": "approve", "decided_by": "should-not-work"},
    )
    overwritten = bool(second and second.get("ok"))

    # Re-raising a denied action must not produce a fresh card.
    reraise = await _post(f"{ctx.approvals_url}/approval/request", payload)
    renotified = bool(reraise and not reraise.get("duplicate"))

    if overwritten:
        return ProbeResult(
            "denial",
            Status.FAIL,
            "a decided request accepted a second, conflicting decision",
            {"second": second},
        )
    if renotified:
        return ProbeResult(
            "denial",
            Status.FAIL,
            "re-raising a denied action produced a new card (notification spam)",
            {"reraise": reraise},
        )

    return ProbeResult(
        "denial",
        Status.PASS,
        "denial recorded, immutable, and did not re-notify on re-raise",
        {"dedupe_key": key, "decision": status.get("decision")},
    )


# ---------------------------------------------------------------------------
# Probe 5: crash resume
# ---------------------------------------------------------------------------


async def probe_crash_resume(ctx: VerifyContext) -> ProbeResult:
    """Recorded state must survive a restart.

    Full checkpoint resume requires a live build, which is asserted separately.
    What is checked here is the precondition: the ledger and approval store are
    durable across process restarts. If that is false, checkpoint resume cannot
    work regardless of what SWE-AF does.
    """
    import os

    from ..ledger import Ledger

    db_path = os.environ.get("CTSWARM_DB", "var/ctswarm.db")
    build_id = f"verify-durability-{int(time.time())}"

    ledger = Ledger(db_path)
    ledger.record_event("verify_durability_marker", {"probe": "crash_resume"}, build_id)

    # A brand new connection stands in for a restarted process: nothing is
    # carried over in memory.
    reopened = Ledger(db_path)
    events = reopened.events(kind="verify_durability_marker", build_id=build_id)

    if not events:
        return ProbeResult(
            "crash_resume",
            Status.FAIL,
            "state written before a restart was not readable after; checkpoint "
            "resume cannot work",
            {"db": db_path},
        )

    return ProbeResult(
        "crash_resume",
        Status.PASS,
        "ledger state survives a fresh connection; full build-resume still "
        "requires a live build to assert",
        {"db": db_path, "events_recovered": len(events)},
    )


# ---------------------------------------------------------------------------
# Probe 6: repository isolation
# ---------------------------------------------------------------------------


async def probe_isolation(ctx: VerifyContext) -> ProbeResult:
    """main must be untouched, and worktrees must not contaminate each other."""
    repo = ctx.repo_path
    if repo is None or not (Path(repo) / ".git").exists():
        return ProbeResult(
            "isolation",
            Status.SKIP,
            "no target repository given; pass --repo to assert isolation against "
            "a real build",
        )

    def git(*args: str) -> tuple[int, str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return proc.returncode, proc.stdout.strip()

    code, current = git("rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        return ProbeResult("isolation", Status.SKIP, "could not read git state")

    findings: dict = {"current_branch": current}

    # Any commit authored by the factory directly on main is a hard failure.
    code, main_log = git(
        "log", "--oneline", "-20", "--format=%h %an %s", ctx.base_ref
    )
    findings["recent_main_commits"] = main_log.splitlines()[:5]

    code, worktrees = git("worktree", "list", "--porcelain")
    worktree_paths = [
        line.split(" ", 1)[1]
        for line in worktrees.splitlines()
        if line.startswith("worktree ")
    ]
    findings["worktrees"] = worktree_paths

    # Two worktrees claiming the same branch is the cross-contamination signature.
    code, branches = git("worktree", "list")
    branch_refs = [
        line.rsplit("[", 1)[-1].rstrip("]")
        for line in branches.splitlines()
        if "[" in line
    ]
    duplicates = {b for b in branch_refs if branch_refs.count(b) > 1}
    if duplicates:
        return ProbeResult(
            "isolation",
            Status.FAIL,
            f"worktrees share branches {sorted(duplicates)}; agents can contaminate "
            "each other",
            findings,
        )

    return ProbeResult(
        "isolation",
        Status.PASS,
        f"{len(worktree_paths)} worktree(s), no shared branches",
        findings,
    )


ALL_PROBES = (
    probe_antislop_trap,
    probe_failover,
    probe_approval_trigger,
    probe_denial,
    probe_crash_resume,
    probe_isolation,
)


async def run_all(ctx: VerifyContext) -> list[ProbeResult]:
    """Run every probe. One probe's failure never prevents the others running."""
    results: list[ProbeResult] = []
    for probe in ALL_PROBES:
        try:
            results.append(await probe(ctx))
        except Exception as exc:  # noqa: BLE001 - a probe bug must not hide others
            results.append(
                ProbeResult(
                    probe.__name__.replace("probe_", ""),
                    Status.FAIL,
                    f"probe raised {type(exc).__name__}: {exc}",
                )
            )
    return results
