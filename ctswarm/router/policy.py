"""Routing policy: which model serves which request, and in what fallback order.

The plan this implements calls for policy-based switching rather than random or
round-robin selection. Every candidate is scored on availability, remaining quota,
measured task success, latency, context capacity, tool-call reliability, privacy
class, and cost. A circuit breaker removes models producing repeated malformed
tool calls, timeouts, or rate-limit errors.

The single most important rule here is the **tool-call gate**. SWE-AF's agents are
defined by tool sets and typed output schemas across 400 to 500+ invocations per
build. A model that emits malformed tool calls even a few percent of the time
stalls the DAG rather than merely degrading output quality. So tool-call fidelity
is a hard eligibility filter, not a scoring term that good latency can offset.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..catalog import CatalogEntry, Tier, build_catalog, tier_for_role
from ..ledger import Ledger
from ..platform_detect import HostProfile

# Below this measured tool-call success rate a model may not fill any agent role.
# Set from the DAG-stall reasoning above rather than from a general quality bar.
TOOL_CALL_FLOOR = 0.90

# Below this measured schema-adherence rate a model may not fill a role whose
# output SWE-AF parses into a typed result.
SCHEMA_FLOOR = 0.85


class Privacy(str):
    """Privacy class of a request, controlling whether it may leave the machine."""

    LOCAL_ONLY = "local_only"
    ANY = "any"


@dataclass(frozen=True)
class BenchScore:
    """Measured behavior for one model, produced by ``ctswarm bench``."""

    model_ref: str
    backend: str
    tool_call_rate: float
    schema_rate: float
    long_context_rate: float
    instruction_rate: float
    cancel_clean: bool
    p50_latency_ms: float
    tokens_per_s: float
    max_context_ok: int
    samples: int = 0

    @property
    def eligible_for_agent_roles(self) -> bool:
        """Whether this model may fill a SWE-AF agent role at all."""
        return (
            self.tool_call_rate >= TOOL_CALL_FLOOR
            and self.schema_rate >= SCHEMA_FLOOR
            and self.cancel_clean
        )

    @property
    def quality(self) -> float:
        """Composite quality in [0, 1].

        Weighted toward the behaviors that stall a build rather than the ones
        that merely make output worse.
        """
        return (
            0.40 * self.tool_call_rate
            + 0.30 * self.schema_rate
            + 0.20 * self.long_context_rate
            + 0.10 * self.instruction_rate
        )

    def to_dict(self) -> dict:
        return {
            "model_ref": self.model_ref,
            "backend": self.backend,
            "tool_call_rate": round(self.tool_call_rate, 4),
            "schema_rate": round(self.schema_rate, 4),
            "long_context_rate": round(self.long_context_rate, 4),
            "instruction_rate": round(self.instruction_rate, 4),
            "cancel_clean": self.cancel_clean,
            "p50_latency_ms": round(self.p50_latency_ms, 1),
            "tokens_per_s": round(self.tokens_per_s, 2),
            "max_context_ok": self.max_context_ok,
            "samples": self.samples,
            "eligible": self.eligible_for_agent_roles,
            "quality": round(self.quality, 4),
        }

    @staticmethod
    def from_dict(data: dict) -> BenchScore:
        return BenchScore(
            model_ref=data["model_ref"],
            backend=data["backend"],
            tool_call_rate=float(data.get("tool_call_rate", 0.0)),
            schema_rate=float(data.get("schema_rate", 0.0)),
            long_context_rate=float(data.get("long_context_rate", 0.0)),
            instruction_rate=float(data.get("instruction_rate", 0.0)),
            cancel_clean=bool(data.get("cancel_clean", False)),
            p50_latency_ms=float(data.get("p50_latency_ms", 0.0)),
            tokens_per_s=float(data.get("tokens_per_s", 0.0)),
            max_context_ok=int(data.get("max_context_ok", 0)),
            samples=int(data.get("samples", 0)),
        )


@dataclass(frozen=True)
class Candidate:
    """A concrete (backend, model) pair the router may dispatch to."""

    backend: str
    model_ref: str
    tier: Tier
    score: float
    reason: str
    metered: bool = False
    estimated_cost_per_1k: float = 0.0

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "model_ref": self.model_ref,
            "tier": self.tier.value,
            "score": round(self.score, 4),
            "reason": self.reason,
            "metered": self.metered,
        }


@dataclass(frozen=True)
class RoutingDecision:
    """The ordered dispatch plan for one request."""

    primary: Candidate | None
    fallbacks: tuple[Candidate, ...]
    excluded: tuple[tuple[str, str], ...]  # (model_ref, why)
    tier: Tier
    # Set when no model was eligible at the requested tier and the request was
    # served from a lower one. Surfaced so a build can escalate to a cloud
    # runtime rather than unknowingly planning with an under-provisioned model.
    degraded_from: Tier | None = None

    @property
    def chain(self) -> tuple[Candidate, ...]:
        return ((self.primary,) if self.primary else ()) + self.fallbacks

    def to_dict(self) -> dict:
        return {
            "tier": self.tier.value,
            "primary": self.primary.to_dict() if self.primary else None,
            "fallbacks": [c.to_dict() for c in self.fallbacks],
            "excluded": [{"model": m, "why": w} for m, w in self.excluded],
            "degraded": self.degraded_from.value if self.degraded_from else None,
        }


class RoutingTable:
    """Bench results, keyed by model reference.

    Absent results are not an error. Before the first bench the router falls back
    to catalog priors, which is worse but functional, and says so in the decision
    reason so the behavior is visible rather than silent.
    """

    def __init__(self, scores: dict[str, BenchScore] | None = None) -> None:
        self._scores = dict(scores or {})

    def __contains__(self, model_ref: str) -> bool:
        return model_ref in self._scores

    def get(self, model_ref: str) -> BenchScore | None:
        return self._scores.get(model_ref)

    @property
    def is_empty(self) -> bool:
        return not self._scores

    def all(self) -> tuple[BenchScore, ...]:
        return tuple(self._scores.values())

    @staticmethod
    def load(path: str | Path = "bench/results/routing.json") -> RoutingTable:
        path = Path(path)
        if not path.exists():
            return RoutingTable()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return RoutingTable()
        scores = {}
        for entry in raw.get("models", []):
            try:
                score = BenchScore.from_dict(entry)
            except (KeyError, TypeError, ValueError):
                continue
            scores[score.model_ref] = score
        return RoutingTable(scores)

    def save(self, path: str | Path = "bench/results/routing.json") -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "models": [score.to_dict() for score in self._scores.values()],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class Router:
    """Scores and orders candidates for a request."""

    def __init__(
        self,
        *,
        host: HostProfile,
        ledger: Ledger,
        table: RoutingTable | None = None,
        budget_usd_remaining: float = 0.0,
        prefer_local: bool = True,
        local_only: bool = False,
        backends: set[str] | None = None,
    ) -> None:
        self.host = host
        self.ledger = ledger
        self.table = table or RoutingTable()
        self.budget_usd_remaining = budget_usd_remaining
        self.prefer_local = prefer_local
        self.local_only = local_only
        self._catalog: tuple[CatalogEntry, ...] = build_catalog(host, backends)

    def catalog_snapshot(
        self,
        *,
        installed_by_backend: dict[str, set[str]],
        warm_by_backend: dict[str, set[str]],
    ) -> list[dict]:
        """Explain every configured model's current operational availability.

        This uses the same hard gates as dispatch so the operator catalog cannot
        label a model available while the router would reject it.
        """
        rows: list[dict] = []
        for entry in self._catalog:
            spec = entry.spec
            backend_installed = installed_by_backend.get(spec.backend, set())
            backend_warm = warm_by_backend.get(spec.backend, set())
            installed = spec.ref in backend_installed
            routable_tiers: list[str] = []
            exclusions: dict[str, str] = {}

            for tier in spec.tiers:
                if spec.backend in installed_by_backend and not installed:
                    reason = "not installed on backend"
                else:
                    reason = self._ineligible_reason(
                        entry,
                        tier=tier,
                        needs_tools=True,
                        min_context=8192,
                        privacy=Privacy.ANY,
                        installed=backend_installed,
                        warm=backend_warm,
                    )
                if reason:
                    exclusions[tier.value] = reason
                else:
                    routable_tiers.append(tier.value)

            benchmark = self.table.get(spec.ref)
            row = entry.to_dict()
            row.update(
                {
                    "installed": installed,
                    "warm": spec.ref in backend_warm,
                    "routable": bool(routable_tiers),
                    "routable_tiers": routable_tiers,
                    "exclusions": exclusions,
                    "benchmark": benchmark.to_dict() if benchmark else None,
                    "circuit_open": self.ledger.is_open(spec.ref),
                }
            )
            rows.append(row)

        return sorted(
            rows,
            key=lambda row: (
                not row["routable"],
                row["placement"] == "hosted",
                row["backend"],
                row["ref"],
            ),
        )

    # -- eligibility -------------------------------------------------------

    def _ineligible_reason(
        self,
        entry: CatalogEntry,
        *,
        tier: Tier,
        needs_tools: bool,
        min_context: int,
        privacy: str,
        installed: set[str],
        warm: set[str],
    ) -> str | None:
        """Why this candidate cannot serve the request, or None if it can."""
        spec = entry.spec

        if spec.backend == "openrouter" and (
            self.local_only or privacy == Privacy.LOCAL_ONLY
        ):
            return "hosted backend excluded by local-only execution policy"
        if spec.quarantined:
            return "quarantined: known to wedge the shared inference queue"
        if entry.placement == "unavailable":
            return f"does not fit host memory ({spec.weight_gb}GB)"
        if tier not in spec.tiers:
            return f"not rated for {tier.value} tier"
        if needs_tools and not spec.tools:
            return "no tool-call support"
        if spec.context < min_context:
            return f"context {spec.context} < required {min_context}"
        if installed and spec.ref not in installed:
            return "not installed on backend"
        if self.ledger.is_open(spec.ref):
            return "circuit breaker open"

        score = self.table.get(spec.ref)
        if score is not None:
            if needs_tools and score.tool_call_rate < TOOL_CALL_FLOOR:
                return (
                    f"tool-call rate {score.tool_call_rate:.0%} below "
                    f"{TOOL_CALL_FLOOR:.0%} floor"
                )
            if score.schema_rate < SCHEMA_FLOOR:
                return f"schema rate {score.schema_rate:.0%} below {SCHEMA_FLOOR:.0%} floor"
            if not score.cancel_clean:
                return "fails cancellation cleanly"
            # ``max_context_ok`` is the largest context exercised by the bench,
            # not a measured failure boundary. The default suite runs one
            # 20k-token needle test, so treating that successful sample as a
            # hard ceiling rejects every later tool turn once its transcript
            # grows past 20k—even when the backend advertises 128k+ and the
            # long-context probe passed. The catalog's context limit above is
            # the actual hard capacity gate; this value remains useful evidence
            # in telemetry without inventing a ceiling the bench never tested.
        elif not self.table.is_empty:
            # The table exists but this model is absent, meaning it was never
            # measured. Unmeasured models are not silently trusted once
            # measurement is the norm.
            return "not measured by bench"

        return None

    # -- scoring -----------------------------------------------------------

    def _score(self, entry: CatalogEntry, warm: set[str]) -> tuple[float, str]:
        spec = entry.spec
        bench = self.table.get(spec.ref)
        stats = self.ledger.stats(spec.ref)

        # Quality: measured if available, otherwise a neutral prior that keeps an
        # unmeasured model usable but never preferred over a measured good one.
        quality = bench.quality if bench else 0.5
        reasons = ["measured" if bench else "unmeasured prior"]

        # Live success rate from real builds outranks bench once enough calls
        # exist, because bench tasks are proxies and builds are the real thing.
        if stats.calls >= 20:
            quality = 0.5 * quality + 0.5 * stats.success_rate
            reasons.append(f"live {stats.success_rate:.0%} over {stats.calls}")

        # Placement penalty stands in for throughput until bench measures it.
        throughput = entry.penalty
        if bench and bench.tokens_per_s > 0:
            # Normalize against a reference rate rather than a hard cap so a very
            # fast model is rewarded without dominating quality entirely.
            throughput = min(1.0, bench.tokens_per_s / 60.0)
            reasons.append(f"{bench.tokens_per_s:.0f} tok/s")

        score = 0.65 * quality + 0.25 * throughput

        # Warm models avoid a VRAM swap. On a 12GB card, swapping a large model
        # in evicts whatever was resident, so this is a real cost, not a nicety.
        if spec.ref in warm:
            score += 0.10
            reasons.append("already resident")

        if self.prefer_local and entry.spec.backend not in ("openrouter",):
            score += 0.05
            reasons.append("local")

        return score, ", ".join(reasons)

    # -- selection ---------------------------------------------------------

    def decide(
        self,
        *,
        role: str | None = None,
        tier: Tier | None = None,
        needs_tools: bool = True,
        min_context: int = 8192,
        privacy: str = Privacy.ANY,
        installed: set[str] | None = None,
        warm: set[str] | None = None,
        max_fallbacks: int = 3,
    ) -> RoutingDecision:
        """Produce an ordered dispatch chain for one request."""
        resolved_tier = tier or (tier_for_role(role) if role else Tier.MED)
        installed = installed or set()
        warm = warm or set()

        eligible: list[Candidate] = []
        excluded: list[tuple[str, str]] = []
        degraded_from: Tier | None = None

        # Tiers are ranked priors, not hard partitions, and the catalog's tier
        # assignments are guesses that the bench supersedes. When a tier has no
        # eligible model, serving the request from the next tier down beats
        # returning nothing: an empty high tier would hard-fail every planning
        # role and stall the build before it began.
        #
        # This is a *degradation*, and it is labelled as one. A 9B model that
        # aced the bench is still not evidence of architectural reasoning depth,
        # which the bench does not measure. The decision says so rather than
        # quietly presenting a med-tier model as a high-tier one.
        for attempt_tier in _tier_descent(resolved_tier):
            eligible = []
            attempt_excluded: list[tuple[str, str]] = []

            for entry in self._catalog:
                reason = self._ineligible_reason(
                    entry,
                    tier=attempt_tier,
                    needs_tools=needs_tools,
                    min_context=min_context,
                    privacy=privacy,
                    installed=installed,
                    warm=warm,
                )
                if reason:
                    attempt_excluded.append((entry.spec.ref, reason))
                    continue
                score, why = self._score(entry, warm)
                if attempt_tier is not resolved_tier:
                    why = f"{why}; DEGRADED from {resolved_tier.value} tier"
                eligible.append(
                    Candidate(
                        backend=entry.spec.backend,
                        model_ref=entry.spec.ref,
                        tier=resolved_tier,
                        score=score,
                        reason=why,
                        metered=False,
                    )
                )

            if eligible:
                if attempt_tier is not resolved_tier:
                    degraded_from = attempt_tier
                excluded = attempt_excluded
                break
            excluded = attempt_excluded

        eligible.sort(key=lambda c: c.score, reverse=True)

        # Diversify the fallback chain by backend where possible. Falling back
        # from one Ollama model to another does nothing when the failure was the
        # Ollama process dying, which is exactly what probe 2 simulates.
        chain = _diversify(eligible, limit=max_fallbacks + 1)

        return RoutingDecision(
            primary=chain[0] if chain else None,
            fallbacks=tuple(chain[1:]),
            excluded=tuple(excluded),
            tier=resolved_tier,
            degraded_from=degraded_from,
        )


def _tier_descent(tier: Tier) -> tuple[Tier, ...]:
    """The tier itself, then progressively lower tiers to fall back through.

    Never ascends. Promoting a low-tier model into a planning role would be a
    silent quality regression in the direction that matters most, since planning
    errors propagate across every issue in the build.
    """
    order = (Tier.HIGH, Tier.MED, Tier.LOW)
    return order[order.index(tier):]


def _diversify(candidates: list[Candidate], *, limit: int) -> list[Candidate]:
    """Order candidates so the chain crosses backends as early as possible.

    Keeps the highest-scoring candidate first unconditionally, then prefers the
    best candidate from a backend not yet represented before adding a second
    candidate from an already-used backend.
    """
    if not candidates:
        return []

    chosen = [candidates[0]]
    seen_backends = {candidates[0].backend}
    remaining = candidates[1:]

    while remaining and len(chosen) < limit:
        fresh = next((c for c in remaining if c.backend not in seen_backends), None)
        pick = fresh if fresh is not None else remaining[0]
        chosen.append(pick)
        seen_backends.add(pick.backend)
        remaining.remove(pick)

    return chosen
