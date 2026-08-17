"""Which classes of runtime a host is allowed to use.

ctswarm was built local-inference-first: `open_code` is the base runtime and the
router resolves `ctswarm/*` virtual models to Ollama, MLX, LM Studio, or
OpenRouter. That assumes a machine with an accelerator and enough memory to hold
a coding-capable model.

A host without one -- a Raspberry Pi, a small VPS, a laptop on battery -- has no
usable local path at all, and pretending otherwise produces a build that stalls
on a model that cannot serve it. Subscription-only mode says so explicitly: every
role runs on a CLI harness (`claude_code` or `codex`) driven by a subscription
login, no local backend is registered, no API key is accepted as a credential,
and the local-model surfaces are hidden rather than shown broken.

The mode is a ledger setting rather than only an environment variable so it
survives a container rebuild and is auditable, but the environment wins when set
so a host can force the correct mode before any database exists.
"""

from __future__ import annotations

import os
from typing import Any

from .ledger import Ledger

EXECUTION_MODE_SETTING = "execution_mode_v1"
EXECUTION_MODE_UPDATED = "execution_mode_updated"
EXECUTION_MODE_ENV = "CTSWARM_EXECUTION_MODE"

SUBSCRIPTION_ONLY = "subscription_only"
HYBRID = "hybrid"

MODES = (SUBSCRIPTION_ONLY, HYBRID)

# Subscription-only is the default because it is the mode that works on the
# widest range of hosts. A machine with local inference opts into `hybrid`
# deliberately; a machine without one should not have to opt out of a broken
# default before it can run anything.
DEFAULT_MODE = SUBSCRIPTION_ONLY

MODE_LABELS = {
    SUBSCRIPTION_ONLY: "Subscriptions only",
    HYBRID: "Subscriptions and local models",
}


class ExecutionModeError(ValueError):
    """Raised when an operator supplies a mode that does not exist."""


def normalize_mode(value: Any) -> str:
    """Validate one mode name."""
    mode = str(value or "").strip().lower()
    if not mode:
        raise ExecutionModeError("execution mode must not be empty")
    if mode not in MODES:
        raise ExecutionModeError(
            f"unknown execution mode: {mode}; expected one of {', '.join(MODES)}"
        )
    return mode


def load_mode(ledger: Ledger | None = None, env: dict | None = None) -> str:
    """Return the effective mode.

    Resolution order is environment, then ledger, then the default. The
    environment wins so a host can be pinned before its database exists -- the
    Pi bootstrap sets it, and no stale stored value can override the machine it
    is actually running on.
    """
    env = env if env is not None else dict(os.environ)
    raw_env = env.get(EXECUTION_MODE_ENV, "").strip()
    if raw_env:
        try:
            return normalize_mode(raw_env)
        except ExecutionModeError:
            # A typo in the environment must not silently enable local models on
            # a host that cannot serve them. Fall back to the safer mode.
            return DEFAULT_MODE
    if ledger is None:
        return DEFAULT_MODE
    try:
        return normalize_mode(ledger.setting(EXECUTION_MODE_SETTING, DEFAULT_MODE))
    except ExecutionModeError:
        return DEFAULT_MODE


def save_mode(
    ledger: Ledger, value: Any, *, changed_by: str = "mission-control"
) -> str:
    """Persist one validated mode and append an audit event."""
    mode = normalize_mode(value)
    ledger.set_setting(EXECUTION_MODE_SETTING, mode)
    ledger.record_event(EXECUTION_MODE_UPDATED, {"changed_by": changed_by, "mode": mode})
    return mode


def env_pinned(env: dict | None = None) -> bool:
    """Whether the environment is forcing the mode.

    The dashboard needs this to explain why the control is read-only rather than
    silently discarding an operator's change on the next restart.
    """
    env = env if env is not None else dict(os.environ)
    return bool(env.get(EXECUTION_MODE_ENV, "").strip())


def subscription_only(ledger: Ledger | None = None, env: dict | None = None) -> bool:
    """Convenience predicate for the common branch."""
    return load_mode(ledger, env) == SUBSCRIPTION_ONLY
