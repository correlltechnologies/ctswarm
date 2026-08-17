"""Operator-controlled routing assignments for future builds.

The dashboard speaks in work categories and concrete providers. SWE-AF still
needs its internal per-role provider/model maps, so this module is the single
translation boundary between those two vocabularies.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .ledger import Ledger

ROUTING_POLICY_SETTING = "routing_policy_v1"
ROUTING_POLICY_UPDATED = "routing_policy_updated"

TARGETS = frozenset({"auto", "ollama", "openrouter", "claude_code", "codex"})

# Targets that need a model served over HTTP, whether local or hosted. These are
# exactly the ones a subscriptions-only host cannot reach, because it registers
# no backends and holds no API keys.
MODEL_TARGETS = frozenset({"ollama", "openrouter"})

SUBSCRIPTION_TARGETS = frozenset(TARGETS - MODEL_TARGETS)


def allowed_targets(*, subscriptions_only: bool) -> frozenset[str]:
    """Which provider choices an operator may assign in the current mode."""
    return SUBSCRIPTION_TARGETS if subscriptions_only else TARGETS

LANE_ROLES: dict[str, tuple[str, ...]] = {
    "planning": (
        "pm",
        "architect",
        "tech_lead",
        "sprint_planner",
        "replan",
        "issue_advisor",
        "retry_advisor",
        "issue_writer",
    ),
    "implementation": (
        "default",
        "coder",
        "qa",
        "integration_tester",
        "ci_fixer",
    ),
    "review": ("code_reviewer", "verifier", "qa_synthesizer"),
    "maintenance": ("git", "merger"),
}

DEFAULT_ROUTING_POLICY: dict[str, dict[str, str]] = {
    lane: {"target": "auto", "model": ""} for lane in LANE_ROLES
}


class RoutingPolicyError(ValueError):
    """Raised when an operator policy cannot be applied safely."""


def normalize_routing_policy(
    value: Any, *, subscriptions_only: bool = False
) -> dict[str, dict[str, str]]:
    """Validate and normalize an operator policy payload."""
    if not isinstance(value, dict):
        raise RoutingPolicyError("routing policy must be an object")

    unknown = sorted(set(value) - set(LANE_ROLES))
    if unknown:
        raise RoutingPolicyError(f"unknown work categories: {', '.join(unknown)}")

    permitted = allowed_targets(subscriptions_only=subscriptions_only)
    normalized = deepcopy(DEFAULT_ROUTING_POLICY)
    for lane in LANE_ROLES:
        assignment = value.get(lane, normalized[lane])
        if not isinstance(assignment, dict):
            raise RoutingPolicyError(f"{lane} assignment must be an object")
        target = str(assignment.get("target") or "auto").strip().lower()
        model = str(assignment.get("model") or "").strip()
        if target not in TARGETS:
            raise RoutingPolicyError(f"unsupported provider for {lane}: {target}")
        if target not in permitted:
            raise RoutingPolicyError(
                f"{target} is unavailable for {lane} in subscriptions-only mode; "
                "switch the host to hybrid mode to route to local or hosted models"
            )
        if target in MODEL_TARGETS and not model:
            raise RoutingPolicyError(f"choose a concrete {target} model for {lane}")
        if target not in MODEL_TARGETS:
            model = ""
        normalized[lane] = {"target": target, "model": model}
    return normalized


def load_routing_policy(
    ledger: Ledger, *, subscriptions_only: bool = False
) -> dict[str, dict[str, str]]:
    """Load the current policy, failing back to capacity-aware defaults.

    A stored policy that names a local model is not an error worth failing a
    build over -- it is what the host had before the mode changed. It degrades
    to ``auto``, which the subscription policy then resolves to a real harness.
    """
    raw = ledger.setting(ROUTING_POLICY_SETTING, DEFAULT_ROUTING_POLICY)
    try:
        return normalize_routing_policy(raw, subscriptions_only=subscriptions_only)
    except RoutingPolicyError:
        if not subscriptions_only:
            return deepcopy(DEFAULT_ROUTING_POLICY)
    permitted = allowed_targets(subscriptions_only=True)
    salvaged = deepcopy(DEFAULT_ROUTING_POLICY)
    if isinstance(raw, dict):
        for lane in LANE_ROLES:
            assignment = raw.get(lane)
            if not isinstance(assignment, dict):
                continue
            target = str(assignment.get("target") or "auto").strip().lower()
            if target in permitted:
                salvaged[lane] = {"target": target, "model": ""}
    return salvaged


def save_routing_policy(
    ledger: Ledger,
    value: Any,
    *,
    changed_by: str = "mission-control",
    subscriptions_only: bool = False,
) -> dict[str, dict[str, str]]:
    """Persist one validated policy and append an audit event."""
    policy = normalize_routing_policy(value, subscriptions_only=subscriptions_only)
    ledger.set_setting(ROUTING_POLICY_SETTING, policy)
    ledger.record_event(
        ROUTING_POLICY_UPDATED,
        {"changed_by": changed_by, "policy": policy},
    )
    return policy


def apply_routing_policy(
    policy: dict[str, dict[str, str]],
    *,
    providers: dict[str, str],
    models: dict[str, str],
    claude_model: str,
    codex_model: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Overlay explicit operator assignments on capacity-aware defaults."""
    next_providers = dict(providers)
    next_models = dict(models)
    if all(assignment["target"] == "auto" for assignment in policy.values()):
        return next_providers, next_models

    # Materialize inherited defaults before an explicit implementation choice
    # changes ``default``. Otherwise choosing Claude for implementation would
    # silently move an unrelated auto-configured maintenance role to Claude too.
    default_provider = next_providers.get("default", "open_code")
    default_model = next_models.get("default", "ctswarm/med")
    for roles in LANE_ROLES.values():
        for role in roles:
            if role != "default":
                next_providers.setdefault(role, default_provider)
                next_models.setdefault(role, default_model)

    for lane, roles in LANE_ROLES.items():
        assignment = policy[lane]
        target = assignment["target"]
        if target == "auto":
            continue
        if target in MODEL_TARGETS:
            runtime = "open_code"
            model = f"ctswarm/{target}:{assignment['model']}"
        elif target == "claude_code":
            runtime = "claude_code"
            model = claude_model
        else:
            runtime = "codex"
            model = codex_model

        for role in roles:
            next_providers[role] = runtime
            next_models[role] = model

    return next_providers, next_models


def validate_policy_availability(
    policy: dict[str, dict[str, str]],
    *,
    catalog: list[dict[str, Any]],
    capacity: dict[str, dict[str, Any]],
    subscriptions_only: bool = False,
) -> None:
    """Reject assignments that would make the next build fail immediately."""
    installed = {
        (str(model.get("backend") or ""), str(model.get("ref") or ""))
        for model in catalog
        if model.get("installed") and not model.get("circuit_open")
    }
    for lane, assignment in policy.items():
        target = assignment["target"]
        if target in MODEL_TARGETS:
            if subscriptions_only:
                raise RoutingPolicyError(
                    f"{target} cannot serve {lane} in subscriptions-only mode"
                )
            if (target, assignment["model"]) not in installed:
                raise RoutingPolicyError(
                    f"{assignment['model']} is not available from {target} for {lane}"
                )
        elif target in {"claude_code", "codex"}:
            provider = capacity.get(target) or {}
            if not provider.get("available"):
                reason = str(provider.get("reason") or "provider unavailable")
                raise RoutingPolicyError(f"{target} cannot serve {lane}: {reason}")
