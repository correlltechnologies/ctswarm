from __future__ import annotations

import pytest

from ctswarm.ledger import Ledger
from ctswarm.routing_config import (
    DEFAULT_ROUTING_POLICY,
    RoutingPolicyError,
    apply_routing_policy,
    load_routing_policy,
    normalize_routing_policy,
    save_routing_policy,
    validate_policy_availability,
)


def _policy(**overrides):
    value = {lane: dict(assignment) for lane, assignment in DEFAULT_ROUTING_POLICY.items()}
    value.update(overrides)
    return value


def test_policy_round_trips_through_durable_settings(tmp_path) -> None:
    ledger = Ledger(tmp_path / "routing.db")
    expected = _policy(
        planning={"target": "claude_code", "model": ""},
        implementation={"target": "ollama", "model": "qwen3.5:9b"},
    )

    assert load_routing_policy(ledger) == DEFAULT_ROUTING_POLICY
    assert save_routing_policy(ledger, expected) == expected
    assert load_routing_policy(Ledger(tmp_path / "routing.db")) == expected
    event = ledger.events(kind="routing_policy_updated")[-1]
    assert "mission-control" in event["detail"]


def test_http_provider_requires_a_concrete_model() -> None:
    with pytest.raises(RoutingPolicyError, match="choose a concrete ollama model"):
        normalize_routing_policy(
            _policy(implementation={"target": "ollama", "model": ""})
        )


def test_explicit_assignments_become_real_provider_and_model_maps() -> None:
    providers, models = apply_routing_policy(
        _policy(
            planning={"target": "claude_code", "model": ""},
            implementation={"target": "ollama", "model": "qwen3.5:9b"},
            review={"target": "openrouter", "model": "openai/gpt-oss-120b"},
            maintenance={"target": "codex", "model": ""},
        ),
        providers={"default": "open_code", "architect": "claude_code"},
        models={"default": "ctswarm/med", "architect": "sonnet"},
        claude_model="sonnet",
        codex_model="gpt-5.5",
    )

    assert providers["architect"] == "claude_code"
    assert models["architect"] == "sonnet"
    assert providers["coder"] == "open_code"
    assert models["coder"] == "ctswarm/ollama:qwen3.5:9b"
    assert providers["verifier"] == "open_code"
    assert models["verifier"] == "ctswarm/openrouter:openai/gpt-oss-120b"
    assert providers["git"] == "codex"
    assert models["git"] == "gpt-5.5"


def test_unavailable_assignment_is_rejected() -> None:
    policy = normalize_routing_policy(
        _policy(implementation={"target": "ollama", "model": "missing:9b"})
    )
    with pytest.raises(RoutingPolicyError, match="not available"):
        validate_policy_availability(
            policy,
            catalog=[
                {
                    "backend": "ollama",
                    "ref": "qwen3.5:9b",
                    "installed": True,
                    "circuit_open": False,
                }
            ],
            capacity={
                "claude_code": {"available": True},
                "codex": {"available": True},
            },
        )


def test_unavailable_subscription_assignment_is_rejected() -> None:
    policy = normalize_routing_policy(
        _policy(planning={"target": "claude_code", "model": ""})
    )
    with pytest.raises(RoutingPolicyError, match="no credentials"):
        validate_policy_availability(
            policy,
            catalog=[],
            capacity={
                "claude_code": {"available": False, "reason": "no credentials"},
                "codex": {"available": True},
            },
        )
