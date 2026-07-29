"""Deterministic approval rules.

This module decides what crosses an authority boundary. It is deliberately
plain code with no model in the loop: an agent must never be able to reason its
way into concluding that its own action does not need approval.

Two failure modes are equally bad and the rules target both:

- **Under-escalation** lets an autonomous system take an irreversible action.
- **Over-escalation** trains the owner to reflex-approve, which is worse than no
  approval gate at all because it looks like oversight while providing none.

So routine recoverable errors are explicitly enumerated as never-escalate, not
merely left out of the escalate list.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Risk(str, Enum):
    """Risk classification, which drives both routing and card presentation."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Decision(str, Enum):
    APPROVE = "approve"
    DENY = "deny"
    MODIFY = "modify"
    PAUSE = "pause"
    # Set by the service when a card expires with no human response. Never
    # equivalent to approve; the build pauses.
    EXPIRED = "expired"


@dataclass(frozen=True)
class ApprovalRule:
    """One condition that forces a human decision."""

    name: str
    risk: Risk
    reversible: bool
    why: str
    patterns: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()

    def matches(self, *, action: str, detail: str) -> bool:
        if action in self.actions:
            return True
        haystack = f"{action}\n{detail}".lower()
        return any(re.search(p, haystack, re.IGNORECASE) for p in self.patterns)


# Events that ALWAYS require approval. Sourced directly from the implementation
# plan's section 6, kept as data so the list is auditable and diffable.
ESCALATE_RULES: tuple[ApprovalRule, ...] = (
    ApprovalRule(
        name="merge_to_main",
        risk=Risk.HIGH,
        reversible=True,
        why="Merging to a protected branch during the pilot is a human decision.",
        actions=("merge_main", "merge_to_main"),
        patterns=(r"merge .*(into |to )?(main|master|release)", r"push .*(main|master)"),
    ),
    ApprovalRule(
        name="production_deploy",
        risk=Risk.CRITICAL,
        reversible=False,
        why="Production deployment is outside the pilot's granted authority.",
        actions=("deploy_production",),
        patterns=(r"deploy.*(prod|production)", r"(prod|production).*(deploy|release)"),
    ),
    ApprovalRule(
        name="destructive_migration",
        risk=Risk.CRITICAL,
        reversible=False,
        why="Schema changes that drop or rewrite data cannot be undone by a revert.",
        actions=("destructive_migration",),
        patterns=(
            r"\bdrop\s+(table|column|database|schema)\b",
            r"\btruncate\b",
            r"\bdelete\s+from\b(?!.*\bwhere\b)",
            r"down\s*migration",
            r"\bdrop\b.*\bmigration\b",
            r"\bmigration\b.*\bdrop\b",
        ),
    ),
    ApprovalRule(
        name="data_deletion",
        risk=Risk.CRITICAL,
        reversible=False,
        why="Deleting data is irreversible without a restore.",
        actions=("delete_data",),
        patterns=(r"\brm\s+-rf\b", r"delete .*(records|rows|users|customers|bucket)"),
    ),
    ApprovalRule(
        name="secret_or_permission_change",
        risk=Risk.CRITICAL,
        reversible=False,
        why="Credential and permission changes alter the security boundary itself.",
        actions=("rotate_secret", "change_permissions"),
        patterns=(
            r"rotate .*(secret|key|token|credential)",
            r"(grant|expand|escalate) .*(permission|scope|role|access)",
            r"branch protection",
            r"(modify|disable|remove) .*(protection|policy|approval)",
        ),
    ),
    ApprovalRule(
        name="spend_above_budget",
        risk=Risk.HIGH,
        reversible=False,
        why="Paid-model spend beyond the configured cap needs an explicit decision.",
        actions=("spend_over_budget", "purchase"),
        patterns=(r"exceeds? .*(budget|cap|limit)", r"purchase|subscribe|upgrade plan"),
    ),
    ApprovalRule(
        name="license_or_legal",
        risk=Risk.HIGH,
        reversible=True,
        why="License compatibility and legal exposure are not engineering calls.",
        actions=("license_uncertainty",),
        patterns=(r"\b(gpl|agpl|license incompat|copyleft|proprietary license)\b",),
    ),
    ApprovalRule(
        name="security_finding_production",
        risk=Risk.CRITICAL,
        reversible=True,
        why="A security finding with production impact must reach a human immediately.",
        actions=("security_finding",),
        patterns=(r"(vulnerabilit|cve-|exploit).*(production|live|customer)",),
    ),
    ApprovalRule(
        name="acceptance_criteria_change",
        risk=Risk.HIGH,
        reversible=True,
        why="Changing the definition of done is changing the agreement, not the work.",
        actions=("change_acceptance_criteria",),
        patterns=(r"(change|relax|remove|weaken) .*(acceptance crit|must-have|requirement)",),
    ),
    ApprovalRule(
        name="debt_violating_must_have",
        risk=Risk.HIGH,
        reversible=True,
        why="Accepting debt that breaks a must-have silently redefines success.",
        actions=("accept_debt_must_have",),
        patterns=(r"technical debt.*must-have", r"defer.*must-have"),
    ),
    ApprovalRule(
        name="external_communication",
        risk=Risk.CRITICAL,
        reversible=False,
        why="Anything sent in the owner's name is unrecoverable once delivered.",
        actions=("send_external",),
        patterns=(r"(send|post|publish|email|tweet).*(customer|client|public|external)",),
    ),
    ApprovalRule(
        name="test_weakened",
        risk=Risk.HIGH,
        reversible=True,
        why="Weakening a test to make a build pass destroys the evidence the "
        "whole system depends on.",
        actions=("weaken_test",),
        patterns=(
            r"(skip|delete|disable|remove|weaken)\w*\s+(a\s+|the\s+)?test",
            r"\.skip\(|\.todo\(|xit\(|xdescribe\(",
            r"@pytest\.mark\.skip",
        ),
    ),
)


# Events that must NEVER generate a card. Enumerated explicitly so that adding a
# noisy escalation later requires deleting a line here, which is a visible act.
NEVER_ESCALATE: frozenset[str] = frozenset(
    {
        "retry",
        "model_substitution",
        "worktree_recreate",
        "dependency_reinstall",
        "test_data_reset",
        "additional_research",
        "issue_decomposition",
        "browser_retest",
        "code_review_revision",
        "checkpoint_resume",
        "failover",
        "rate_limit_backoff",
    }
)


@dataclass(frozen=True)
class ApprovalRequest:
    """A structured approval card.

    Field set mirrors the plan's "Approval Card Contents": what is requested and
    why it cannot proceed safely, the exact blast radius, risk and reversibility,
    the evidence already collected, and a recommendation.
    """

    action: str
    detail: str
    build_id: str
    risk: Risk
    reversible: bool
    why_blocked: str
    rule_name: str
    repo: str = ""
    branch: str = ""
    pull_request: str = ""
    environment: str = ""
    files_affected: tuple[str, ...] = ()
    evidence: dict = field(default_factory=dict)
    alternatives: tuple[str, ...] = ()
    recommendation: str = ""
    estimated_cost_usd: float = 0.0
    retry_history: tuple[str, ...] = ()

    @property
    def dedupe_key(self) -> str:
        """Stable identity for this request.

        The pilot criterion is that an injected high-risk action produces
        *exactly one* actionable card. Retries and replans re-surface the same
        action repeatedly, so identity must be content-based rather than
        per-occurrence.
        """
        material = f"{self.build_id}|{self.rule_name}|{self.action}|{self.detail}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "detail": self.detail,
            "build_id": self.build_id,
            "risk": self.risk.value,
            "reversible": self.reversible,
            "why_blocked": self.why_blocked,
            "rule_name": self.rule_name,
            "repo": self.repo,
            "branch": self.branch,
            "pull_request": self.pull_request,
            "environment": self.environment,
            "files_affected": list(self.files_affected),
            "evidence": self.evidence,
            "alternatives": list(self.alternatives),
            "recommendation": self.recommendation,
            "estimated_cost_usd": self.estimated_cost_usd,
            "retry_history": list(self.retry_history),
            "dedupe_key": self.dedupe_key,
        }


def classify(
    *, action: str, detail: str = "", **card_fields
) -> Optional[ApprovalRequest]:
    """Decide whether an action needs approval.

    Returns None when the action may proceed autonomously. The never-escalate
    list is checked first so a routine event whose free-text detail happens to
    contain an alarming word does not generate noise. This ordering is what keeps
    "retry after a failed test that mentions DROP TABLE" from paging anyone.
    """
    if action in NEVER_ESCALATE:
        return None

    for rule in ESCALATE_RULES:
        if rule.matches(action=action, detail=detail):
            return ApprovalRequest(
                action=action,
                detail=detail,
                risk=rule.risk,
                reversible=rule.reversible,
                why_blocked=rule.why,
                rule_name=rule.name,
                build_id=card_fields.pop("build_id", ""),
                **card_fields,
            )
    return None


def is_terminal(decision: Decision) -> bool:
    """Whether a decision ends the request rather than continuing the build."""
    return decision in (Decision.DENY, Decision.PAUSE, Decision.EXPIRED)
