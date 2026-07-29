"""Verification committees: multiple independent models, plus scanners that win.

Section 2 and section 9 of the plan: use committees rather than trusting one
model's judgment, and for security use "multiple independent models **and**
deterministic scanners", because "committee agreement alone cannot establish
security".

Two rules do the real work here, and both are about what *independence* means.

**1. Independence is by model family, not by model count.**
Three Qwen models agreeing is one opinion sampled three times. They share
training data, tokenizer, and failure modes, so they tend to be wrong together
and confidently. A committee that counts votes without checking families
manufactures a consensus that means nothing. So quorum requires distinct
families, and same-family votes collapse to a single vote.

**2. Deterministic scanners are authoritative; models are advisory.**
A model committee cannot vote away a secret-scanner hit or a failing test. For
security decisions the scanner result is a hard gate that a unanimous panel
cannot override. Models can add findings; they cannot subtract them.

The committee never decides whether work is *done*. That is the evidence layer's
job, and it is deterministic. A committee decides questions of judgment where no
deterministic check exists, such as "is this architecture sound" or "does this
diff contain a subtle authorization bug".
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from enum import Enum

from .backends import Backend, ChatRequest
from .router.policy import RoutingTable


class Verdict(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    ABSTAIN = "abstain"


# Model family, derived from the reference. Independence is judged on this, so
# an unrecognised model is deliberately treated as its own family rather than
# being lumped into a default bucket, which would understate independence.
FAMILY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"qwen|qwq", "qwen"),
    (r"granite", "granite"),
    (r"llama|codellama", "llama"),
    (r"mistral|mixtral|codestral", "mistral"),
    (r"deepseek", "deepseek"),
    (r"gemma", "gemma"),
    (r"phi", "phi"),
    (r"claude|anthropic|sonnet|opus|haiku", "anthropic"),
    (r"gpt|o[0-9]|codex|openai", "openai"),
    (r"gemini|google", "google"),
    (r"glm|zhipu|z-ai", "zhipu"),
    (r"minimax", "minimax"),
    (r"ornith", "qwen"),  # qwen35 architecture per `ollama show`
    (r"laguna", "laguna"),
)


def family_of(model_ref: str) -> str:
    """Model family for independence checks."""
    lowered = model_ref.lower()
    for pattern, family in FAMILY_PATTERNS:
        if re.search(pattern, lowered):
            return family
    return f"unknown:{lowered.split(':')[0]}"


@dataclass(frozen=True)
class Vote:
    """One member's opinion."""

    model_ref: str
    family: str
    verdict: Verdict
    confidence: float
    reasoning: str
    findings: tuple[str, ...] = ()
    error: str = ""

    @property
    def counted(self) -> bool:
        return not self.error and self.verdict is not Verdict.ABSTAIN

    def to_dict(self) -> dict:
        return {
            "model_ref": self.model_ref,
            "family": self.family,
            "verdict": self.verdict.value,
            "confidence": round(self.confidence, 2),
            "reasoning": self.reasoning[:400],
            "findings": list(self.findings),
            "error": self.error,
        }


@dataclass(frozen=True)
class ScannerResult:
    """A deterministic check. Not a vote, a fact."""

    name: str
    passed: bool
    detail: str
    findings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "scanner": self.name,
            "passed": self.passed,
            "detail": self.detail[:300],
            "findings": list(self.findings),
        }


class Rule(str, Enum):
    """How votes combine."""

    MAJORITY = "majority"
    UNANIMOUS = "unanimous"
    # Any single reject blocks. For security review, a lone dissenter finding a
    # real vulnerability is far more likely to be right than a majority missing
    # it, because finding a bug is evidence and not finding one is not.
    ANY_REJECT_BLOCKS = "any_reject_blocks"


@dataclass
class CommitteeResult:
    question: str
    votes: list[Vote] = field(default_factory=list)
    scanners: list[ScannerResult] = field(default_factory=list)
    rule: Rule = Rule.MAJORITY
    min_families: int = 2
    approved: bool = False
    reason: str = ""
    needs_human: bool = False

    @property
    def families(self) -> set[str]:
        return {v.family for v in self.votes if v.counted}

    @property
    def independent_votes(self) -> list[Vote]:
        """One vote per family, keeping the least favourable from each.

        Collapsing to the least favourable is deliberate: if two same-family
        models disagree, the family has not established agreement, and taking
        the optimistic one would launder uncertainty into approval.
        """
        by_family: dict[str, Vote] = {}
        for vote in self.votes:
            if not vote.counted:
                continue
            existing = by_family.get(vote.family)
            if existing is None or (
                existing.verdict is Verdict.APPROVE and vote.verdict is Verdict.REJECT
            ):
                by_family[vote.family] = vote
        return list(by_family.values())

    @property
    def all_findings(self) -> list[str]:
        found: list[str] = []
        for scanner in self.scanners:
            found.extend(scanner.findings)
        for vote in self.votes:
            found.extend(vote.findings)
        return found

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "approved": self.approved,
            "reason": self.reason,
            "needs_human": self.needs_human,
            "rule": self.rule.value,
            "families_represented": sorted(self.families),
            "independent_vote_count": len(self.independent_votes),
            "votes": [v.to_dict() for v in self.votes],
            "scanners": [s.to_dict() for s in self.scanners],
            "findings": self.all_findings,
        }


VOTE_SCHEMA_PROMPT = (
    "Respond with ONLY a JSON object, no prose and no code fence:\n"
    '{"verdict": "approve" | "reject" | "abstain", '
    '"confidence": 0.0-1.0, '
    '"reasoning": "one or two sentences", '
    '"findings": ["specific issue", ...]}\n\n'
    'Use "abstain" if you genuinely lack the information to judge. '
    "Abstaining is correct and preferable to guessing; a fabricated judgement "
    "is worse than no judgement."
)


def _parse_vote(model_ref: str, content: str) -> Vote:
    """Parse a member's response, defaulting to abstain on anything unclear.

    Defaulting to abstain rather than approve matters: an unparseable response
    must never become silent consent.
    """
    family = family_of(model_ref)
    text = (content or "").strip()
    if not text:
        return Vote(model_ref, family, Verdict.ABSTAIN, 0.0, "", error="empty response")

    candidate = text
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            candidate = brace.group(0)

    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return Vote(
            model_ref, family, Verdict.ABSTAIN, 0.0, text[:200],
            error="unparseable vote",
        )

    raw = str(parsed.get("verdict", "")).lower()
    try:
        verdict = Verdict(raw)
    except ValueError:
        return Vote(
            model_ref, family, Verdict.ABSTAIN, 0.0, text[:200],
            error=f"unknown verdict {raw!r}",
        )

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    findings = parsed.get("findings") or []
    return Vote(
        model_ref=model_ref,
        family=family,
        verdict=verdict,
        confidence=max(0.0, min(1.0, confidence)),
        reasoning=str(parsed.get("reasoning", ""))[:400],
        findings=tuple(str(f) for f in findings if str(f).strip())[:10],
    )


async def _ask_member(
    backend: Backend, model_ref: str, question: str, context: str
) -> Vote:
    request = ChatRequest(
        messages=[
            {
                "role": "system",
                "content": "You are an independent reviewer on a verification "
                "committee. Other reviewers are judging the same question "
                "separately; you cannot see them. Judge only what the evidence "
                "supports.\n\n" + VOTE_SCHEMA_PROMPT,
            },
            {"role": "user", "content": f"{question}\n\n---\n\n{context}"},
        ],
        model=model_ref,
        temperature=0.0,
        max_tokens=1536,
    )
    try:
        response = await asyncio.wait_for(
            backend.chat(request, model_ref), timeout=240.0
        )
    except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
        return Vote(
            model_ref, family_of(model_ref), Verdict.ABSTAIN, 0.0, "",
            error=f"{type(exc).__name__}",
        )

    if not response.ok:
        return Vote(
            model_ref, family_of(model_ref), Verdict.ABSTAIN, 0.0, "",
            error=response.failure_kind or "backend failure",
        )
    content = (response.body.get("choices") or [{}])[0].get("message", {}).get("content")
    return _parse_vote(model_ref, content or "")


def eligible_members(
    table: RoutingTable, *, exclude: set[str] | None = None
) -> list[str]:
    """Bench-eligible models, one preferred per family.

    Drawing from the routing table means a model that failed the tool-call or
    schema gate cannot sit on a committee. A reviewer that cannot produce
    parseable output is not a reviewer.
    """
    exclude = exclude or set()
    best_per_family: dict[str, tuple[float, str]] = {}
    for score in table.all():
        if not score.eligible_for_agent_roles or score.model_ref in exclude:
            continue
        family = family_of(score.model_ref)
        current = best_per_family.get(family)
        if current is None or score.quality > current[0]:
            best_per_family[family] = (score.quality, score.model_ref)
    return [ref for _, ref in sorted(best_per_family.values(), reverse=True)]


async def convene(
    *,
    question: str,
    context: str,
    backend: Backend,
    members: list[str],
    scanners: list[ScannerResult] | None = None,
    rule: Rule = Rule.MAJORITY,
    min_families: int = 2,
) -> CommitteeResult:
    """Run a committee and combine its votes with deterministic scanners.

    Members are polled sequentially, not concurrently. Two local models cannot be
    resident in a 12GB accelerator at once, so concurrent polling would measure
    swap thrash and could wedge the shared queue.
    """
    scanners = scanners or []
    result = CommitteeResult(
        question=question, scanners=list(scanners), rule=rule, min_families=min_families
    )

    for model_ref in members:
        result.votes.append(await _ask_member(backend, model_ref, question, context))

    # Scanners first, and they are decisive. No amount of model agreement can
    # clear a deterministic failure; that is the difference between evidence and
    # opinion.
    failed_scanners = [s for s in scanners if not s.passed]
    if failed_scanners:
        result.approved = False
        names = ", ".join(s.name for s in failed_scanners)
        result.reason = (
            f"blocked by deterministic scanner(s): {names}. "
            "Model agreement cannot override a scanner result."
        )
        return result

    independent = result.independent_votes
    families = len(result.families)

    if families < min_families:
        # Too few independent perspectives to call it a committee. Escalating is
        # correct: silently accepting a single family's opinion as consensus is
        # exactly the failure this module exists to prevent.
        result.approved = False
        result.needs_human = True
        result.reason = (
            f"only {families} independent model family/families responded "
            f"({sorted(result.families)}); {min_families} required. "
            "Same-family models share failure modes and do not constitute "
            "independent review."
        )
        return result

    rejects = [v for v in independent if v.verdict is Verdict.REJECT]
    approves = [v for v in independent if v.verdict is Verdict.APPROVE]

    if rule is Rule.ANY_REJECT_BLOCKS:
        if rejects:
            result.approved = False
            result.reason = (
                f"{len(rejects)} of {len(independent)} independent reviewers "
                "rejected; under any-reject-blocks a single substantiated "
                "objection is decisive."
            )
        else:
            result.approved = True
            result.reason = f"no objection from {len(independent)} independent families"
        return result

    if rule is Rule.UNANIMOUS:
        result.approved = not rejects and len(approves) == len(independent)
        result.reason = (
            f"{len(approves)}/{len(independent)} approved"
            + ("" if result.approved else "; unanimity required")
        )
        return result

    result.approved = len(approves) > len(rejects)
    result.reason = f"{len(approves)} approve / {len(rejects)} reject across families"
    # A split decision is not a decision. Send it to a human rather than letting
    # a one-vote margin settle a judgement call.
    if len(approves) == len(rejects):
        result.approved = False
        result.needs_human = True
        result.reason = f"tied {len(approves)}-{len(rejects)}; escalating to a human"
    return result
