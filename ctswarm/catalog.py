"""Platform-aware model catalog.

Candidate models for local inference, filtered by what the host can actually hold.
The catalog proposes; ``ctswarm bench`` disposes. Nothing here asserts that a model
is *good* at agent work, only that it is a plausible candidate worth measuring.

Two hard rules encoded here:

1. A model whose weights exceed accelerator memory is not rejected, it is marked
   ``partial_offload``. Mixture-of-experts models with few active parameters remain
   usable when spilled; dense models generally do not. Making this explicit beats
   silently picking something that will crawl.
2. Tool-call capability is a prerequisite, not a bonus. SWE-AF's agents are defined
   by tool sets, so a model without tool support cannot fill an agent role at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .platform_detect import HostProfile


class Tier(str, Enum):
    """SWE-AF's three role tiers.

    high = planning-heavy reasoning (pm, architect, tech_lead, replan)
    med  = coding / review / QA (coder, qa, code_reviewer, verifier, ...)
    low  = mechanical transformation (qa_synthesizer, git)
    """

    HIGH = "high"
    MED = "med"
    LOW = "low"


# SWE-AF role -> tier. Mirrors the tier mapping documented in SWE-AF's
# .env.example so that ctswarm's virtual models line up with the factory's own
# resolution order (runtime defaults -> models.default -> models.<role>).
ROLE_TIERS: dict[str, Tier] = {
    "pm": Tier.HIGH,
    "architect": Tier.HIGH,
    "tech_lead": Tier.HIGH,
    "replan": Tier.HIGH,
    "coder": Tier.MED,
    "qa": Tier.MED,
    "code_reviewer": Tier.MED,
    "sprint_planner": Tier.MED,
    "retry_advisor": Tier.MED,
    "issue_writer": Tier.MED,
    "issue_advisor": Tier.MED,
    "verifier": Tier.MED,
    "merger": Tier.MED,
    "integration_tester": Tier.MED,
    "ci_fixer": Tier.MED,
    "qa_synthesizer": Tier.LOW,
    "git": Tier.LOW,
}


@dataclass(frozen=True)
class ModelSpec:
    """A candidate model, before any measurement has been done."""

    # Backend-native identifier: an ollama tag, an MLX HuggingFace repo, or an
    # OpenRouter model id.
    ref: str
    backend: str
    # Approximate on-disk / in-memory weight size in GiB.
    weight_gb: float
    context: int
    tools: bool
    # Mixture-of-experts models tolerate spilling past accelerator memory far
    # better than dense ones, because only a fraction of parameters are active
    # per token. None means dense.
    active_params_b: float | None = None
    total_params_b: float | None = None
    tiers: tuple[Tier, ...] = ()
    notes: str = ""
    # False when the reference has not been confirmed to resolve on a registry.
    # ``ctswarm doctor`` reports these rather than pretending they exist.
    verified_ref: bool = False
    # Minimum backend version, when the model refuses to pull on older releases.
    # Recorded so doctor can explain a failed pull instead of leaving a gap.
    requires_ollama: str | None = None
    # Excluded from routing regardless of any bench score. Reserved for models
    # that damage the *host*, not merely themselves: a model that wedges the
    # shared inference queue takes every other model down with it, so an
    # occasional good score cannot justify the risk of scheduling it.
    quarantined: bool = False

    @property
    def is_moe(self) -> bool:
        return self.active_params_b is not None

    def fits(self, accel_memory_gb: float, *, headroom: float = 1.2) -> bool:
        """Whether weights plus KV cache headroom fit in accelerator memory."""
        return (self.weight_gb * headroom) <= accel_memory_gb

    def placement(self, host: HostProfile) -> str:
        """How this model will physically run on the given host."""
        # A hosted model occupies none of this machine's memory, so local
        # placement reasoning does not apply to it at all.
        if self.backend == "openrouter":
            return "hosted"
        if self.fits(host.accel_memory_gb):
            return "resident"
        # MoE models with a small active set still produce usable throughput when
        # spilled to system RAM, provided the RAM is there at all.
        if self.is_moe and self.weight_gb <= host.system_memory_gb * 0.7:
            return "partial_offload_moe"
        if self.weight_gb <= host.system_memory_gb * 0.7:
            return "partial_offload_dense"
        return "unavailable"

    def throughput_penalty(self, host: HostProfile) -> float:
        """Multiplier applied to this model's routing score for placement cost.

        These are deliberate, conservative priors used only until the bench has
        measured real latency on this host. Once ``bench/results`` exist, the
        router prefers measured throughput over these values.
        """
        placement = self.placement(host)
        if placement == "hosted":
            # Network latency, but no VRAM contention and no offload penalty.
            return 0.85
        if placement == "resident":
            return 1.0
        if placement == "partial_offload_moe":
            return 0.55
        if placement == "partial_offload_dense":
            return 0.15
        return 0.0


# ---------------------------------------------------------------------------
# Ollama / GGUF candidates (Linux+CUDA, and macOS fallback)
# ---------------------------------------------------------------------------
# Sizes confirmed against the live ollama registry on 2026-07-29. Capability
# flags for models already present locally were read from `ollama show`.

OLLAMA_CANDIDATES: tuple[ModelSpec, ...] = (
    ModelSpec(
        ref="qwen3.5:9b",
        backend="ollama",
        weight_gb=6.6,
        context=262144,
        tools=True,
        total_params_b=9.0,
        tiers=(Tier.MED, Tier.LOW),
        notes="Dense 9B, tools + thinking. Primary resident candidate at 12GB VRAM.",
        verified_ref=True,
    ),
    ModelSpec(
        ref="qwen3.5:4b",
        backend="ollama",
        weight_gb=3.4,
        context=262144,
        tools=True,
        total_params_b=4.0,
        tiers=(Tier.LOW,),
        notes="Fast low-tier candidate for mechanical transforms.",
        verified_ref=True,
    ),
    ModelSpec(
        ref="granite4.1:8b",
        backend="ollama",
        weight_gb=5.3,
        context=131072,
        tools=True,
        total_params_b=8.0,
        tiers=(Tier.MED, Tier.LOW),
        notes="Independent model family. Valuable as a reviewer that does not "
        "share failure modes with the Qwen-family coder.",
        verified_ref=True,
    ),
    ModelSpec(
        ref="laguna-xs-2.1:latest",
        backend="ollama",
        weight_gb=19.0,
        context=262144,
        tools=True,
        active_params_b=3.0,
        total_params_b=33.0,
        tiers=(Tier.HIGH, Tier.MED),
        notes="33B MoE, 3B active, agentic-coding focused. Spills past 12GB VRAM "
        "but the small active set keeps offload viable. Local high-tier candidate. "
        "Requires ollama > 0.31.1 (installed and pulled on 2026-07-29 under "
        "0.32.5).",
        verified_ref=True,
        requires_ollama=">0.31.1",
    ),
    ModelSpec(
        ref="qwen3.6:latest",
        backend="ollama",
        weight_gb=23.0,
        context=262144,
        tools=True,
        active_params_b=3.0,
        total_params_b=36.0,
        tiers=(Tier.HIGH,),
        notes="36B MoE. Heavy offload at 12GB VRAM; bench decides if usable.",
        verified_ref=True,
    ),
    ModelSpec(
        ref="qwen2.5-coder:7b",
        backend="ollama",
        weight_gb=4.7,
        context=32768,
        tools=True,
        total_params_b=7.0,
        tiers=(Tier.LOW,),
        notes="Older generation. Short context is the real limitation for repo-scale work.",
        verified_ref=True,
    ),
    ModelSpec(
        ref="ornith:9b",
        backend="ollama",
        weight_gb=5.6,
        context=262144,
        tools=True,
        total_params_b=9.0,
        tiers=(Tier.MED, Tier.LOW),
        notes="DO NOT USE. Wedges intermittently: enters a runaway generation "
        "that never terminates, pins the GPU, and blocks the entire ollama queue "
        "for every other model until the service is restarted. Observed three "
        "times on 2026-07-29, including once mid-bench where it also blocked "
        "granite4.1:3b. It passed one full bench run cleanly in between, so a "
        "single green result does not clear it.",
        verified_ref=True,
        quarantined=True,
    ),
    ModelSpec(
        ref="granite4.1:3b",
        backend="ollama",
        weight_gb=2.1,
        context=131072,
        tools=True,
        total_params_b=3.0,
        tiers=(Tier.LOW,),
        notes="Smallest viable tool-calling candidate. Fast failover target.",
        verified_ref=True,
    ),
)


# ---------------------------------------------------------------------------
# MLX candidates (macOS + Apple Silicon)
# ---------------------------------------------------------------------------
# MLX references are HuggingFace repo ids served by `mlx_lm.server`. These are
# NOT marked verified: they must be confirmed to resolve on the target Mac before
# use. `ctswarm doctor` performs that check and reports failures rather than
# letting bootstrap assert something untrue. 4-bit quants are the default choice
# because unified memory is shared with the OS.

MLX_CANDIDATES: tuple[ModelSpec, ...] = (
    ModelSpec(
        ref="mlx-community/Qwen3.5-9B-Instruct-4bit",
        backend="mlx",
        weight_gb=5.2,
        context=262144,
        tools=True,
        total_params_b=9.0,
        tiers=(Tier.MED, Tier.LOW),
        notes="MLX counterpart of the primary Linux med-tier candidate.",
    ),
    ModelSpec(
        ref="mlx-community/Qwen3.5-4B-Instruct-4bit",
        backend="mlx",
        weight_gb=2.4,
        context=262144,
        tools=True,
        total_params_b=4.0,
        tiers=(Tier.LOW,),
        notes="Low-tier / fast failover on Apple Silicon.",
    ),
    ModelSpec(
        ref="mlx-community/Qwen3.5-27B-Instruct-4bit",
        backend="mlx",
        weight_gb=15.0,
        context=262144,
        tools=True,
        total_params_b=27.0,
        tiers=(Tier.HIGH, Tier.MED),
        notes="High tier on a 32GB+ Mac. Dense, so it must be resident to be usable.",
    ),
    ModelSpec(
        ref="mlx-community/granite-4.1-8b-4bit",
        backend="mlx",
        weight_gb=4.6,
        context=131072,
        tools=True,
        total_params_b=8.0,
        tiers=(Tier.MED, Tier.LOW),
        notes="Independent family for reviewer independence on Apple Silicon.",
    ),
)


# ---------------------------------------------------------------------------
# OpenRouter candidates
# ---------------------------------------------------------------------------
# Chosen from the live model list on 2026-07-29 with prices read from
# OpenRouter's own endpoint (never hardcoded here; the router fetches them).
#
# Selection criteria, in order:
#   1. Tool-calling support. Non-negotiable for SWE-AF agent roles.
#   2. FAMILY DIVERSITY from the local models. Local eligibility is qwen and
#      granite, so every entry here is a different family. That is what makes a
#      committee vote mean something rather than being one opinion resampled.
#   3. Cost, weighted toward output tokens, because a build emits far more than
#      it consumes per call.
#   4. Context >= 128k, since agents read repo-scale context.
#
# Weight and placement are irrelevant for a hosted model, so weight_gb is 0 and
# placement is always "resident".

OPENROUTER_CANDIDATES: tuple[ModelSpec, ...] = (
    ModelSpec(
        ref="deepseek/deepseek-v4-pro",
        backend="openrouter",
        weight_gb=0.0,
        context=1048576,
        tools=True,
        tiers=(Tier.HIGH, Tier.MED),
        notes="Strong planner, 1M context. Primary high-tier candidate now that "
        "no local model qualifies. Family: deepseek.",
        verified_ref=True,
    ),
    ModelSpec(
        ref="deepseek/deepseek-v4-flash",
        backend="openrouter",
        weight_gb=0.0,
        context=1048576,
        tools=True,
        tiers=(Tier.MED, Tier.LOW),
        notes="Cheap, fast, 1M context. Overflow for coding when local is busy. "
        "Family: deepseek.",
        verified_ref=True,
    ),
    ModelSpec(
        ref="minimax/minimax-m2.5",
        backend="openrouter",
        weight_gb=0.0,
        context=204800,
        tools=True,
        tiers=(Tier.HIGH, Tier.MED),
        notes="SWE-AF's own documented default for the open runtime, so it is "
        "the best-exercised model against this harness. Family: minimax.",
        verified_ref=True,
    ),
    ModelSpec(
        ref="openai/gpt-oss-120b",
        backend="openrouter",
        weight_gb=0.0,
        context=131072,
        tools=True,
        tiers=(Tier.MED,),
        notes="Independent reviewer that shares no failure modes with the "
        "Qwen-family coder. Family: openai.",
        verified_ref=True,
    ),
    ModelSpec(
        ref="mistralai/ministral-8b-2512",
        backend="openrouter",
        weight_gb=0.0,
        context=262144,
        tools=True,
        tiers=(Tier.MED, Tier.LOW),
        notes="Fourth independent family for committee quorum. Family: mistral.",
        verified_ref=True,
    ),
    ModelSpec(
        ref="openai/gpt-oss-20b",
        backend="openrouter",
        weight_gb=0.0,
        context=131072,
        tools=True,
        tiers=(Tier.LOW,),
        notes="Cheap mechanical-tier overflow. Family: openai.",
        verified_ref=True,
    ),
)


@dataclass(frozen=True)
class CatalogEntry:
    """A candidate paired with how it would run on a specific host."""

    spec: ModelSpec
    placement: str
    penalty: float

    @property
    def usable(self) -> bool:
        return self.placement != "unavailable" and self.spec.tools

    def to_dict(self) -> dict:
        return {
            "ref": self.spec.ref,
            "backend": self.spec.backend,
            "weight_gb": self.spec.weight_gb,
            "context": self.spec.context,
            "tiers": [t.value for t in self.spec.tiers],
            "placement": self.placement,
            "penalty": self.penalty,
            "usable": self.usable,
            "verified_ref": self.spec.verified_ref,
            "notes": self.spec.notes,
        }


def candidates_for(
    host: HostProfile, backends: set[str] | None = None
) -> tuple[ModelSpec, ...]:
    """Candidate specs for the backends actually available.

    ``backends`` is the set of backend names the caller can really reach, which
    is NOT the same as which binaries are installed locally. The router runs in a
    container with no ollama binary and reaches the host's server over the docker
    gateway; keying the catalog off local binaries produced an empty catalog
    there, so every request returned "no eligible model" with no exclusions to
    explain why. Reachability is the property that matters, so callers that know
    it should pass it.
    """
    if backends:
        specs: list[ModelSpec] = []
        if "mlx" in backends:
            specs.extend(MLX_CANDIDATES)
        if backends & {"ollama", "lmstudio"}:
            specs.extend(OLLAMA_CANDIDATES)
        if "openrouter" in backends:
            specs.extend(OPENROUTER_CANDIDATES)
        if specs:
            return tuple(specs)

    if host.local_backend == "mlx":
        return MLX_CANDIDATES
    if host.local_backend in ("ollama", "lmstudio"):
        # LM Studio serves GGUF over an OpenAI-compatible API, so the GGUF
        # catalog applies. On Apple Silicon without MLX installed, Ollama-on-Metal
        # is the fallback and the same candidates hold.
        return OLLAMA_CANDIDATES
    return ()


def build_catalog(
    host: HostProfile, backends: set[str] | None = None
) -> tuple[CatalogEntry, ...]:
    """Full catalog for a host, including entries that will not fit.

    Unusable entries are retained deliberately: ``ctswarm doctor`` should be able
    to explain *why* a model was excluded rather than silently omitting it.
    """
    return tuple(
        CatalogEntry(
            spec=spec,
            placement=spec.placement(host),
            penalty=spec.throughput_penalty(host),
        )
        for spec in candidates_for(host, backends)
    )


def usable_for_tier(host: HostProfile, tier: Tier) -> tuple[CatalogEntry, ...]:
    """Usable candidates for a tier, best placement first.

    This is a pre-measurement ordering only. The router overrides it with bench
    results as soon as they exist.
    """
    entries = [
        entry
        for entry in build_catalog(host)
        if entry.usable and tier in entry.spec.tiers
    ]
    return tuple(sorted(entries, key=lambda e: (-e.penalty, e.spec.weight_gb)))


def tier_for_role(role: str) -> Tier:
    """Tier for a SWE-AF role name, defaulting to med for unknown roles.

    Defaulting to med rather than low matters: an unrecognised role is more likely
    to be a new coding/review agent than a mechanical one, and under-provisioning
    a reasoning role fails silently as bad output.
    """
    return ROLE_TIERS.get(role, Tier.MED)
