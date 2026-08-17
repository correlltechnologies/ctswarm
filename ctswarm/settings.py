"""Operator settings: one typed registry, one audit trail.

Before this module the entire product had exactly one durable setting
(``routing_policy_v1``), and everything else that an operator might reasonably
want to change -- concurrency, timeouts, retention, execution mode -- lived only
in environment variables. That is workable on a laptop where you edit `.env` and
restart. It is not workable on a headless box you reach from a phone.

Three decisions shape the design:

**Precedence is ledger, then environment, then default.** The environment is the
boot floor: a host with an empty database still comes up correctly configured,
which is what makes a fresh Pi work before anyone has opened the dashboard. Once
an operator sets a value it wins, because otherwise a change made in the UI would
silently revert on the next restart.

**Every read reports where the value came from.** A settings screen that shows
`1800` without saying whether that is a default, something the host pinned, or
something you chose last week is a screen that invites you to change the wrong
thing.

**Validation is all-or-nothing.** A partially applied settings payload leaves the
system in a state no one asked for and no one can name.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

from .execution_mode import (
    EXECUTION_MODE_ENV,
    EXECUTION_MODE_SETTING,
    HYBRID,
    MODES,
    SUBSCRIPTION_ONLY,
    env_pinned,
)
from .ledger import Ledger

SETTING_UPDATED = "setting_updated"

Kind = Literal["bool", "int", "float", "enum", "str"]
Applies = Literal["new_builds", "immediate", "restart"]


class SettingsError(ValueError):
    """Raised when an operator payload cannot be applied safely."""


@dataclass(frozen=True)
class SettingSpec:
    """One operator-controlled value and the rules that keep it sane."""

    key: str
    kind: Kind
    default: Any
    label: str
    description: str
    section: str
    env_var: str = ""
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    applies_to: Applies = "new_builds"
    #: Surfaced in the API but rejected on write. Used for facts that are
    #: decided at container-start time, where accepting a change from the
    #: browser would be a lie.
    read_only: bool = False

    def coerce(self, value: Any) -> Any:
        """Validate and normalize one incoming value."""
        if self.kind == "bool":
            if isinstance(value, bool):
                return value
            lowered = str(value).strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
            raise SettingsError(f"{self.key} must be true or false")

        if self.kind == "enum":
            candidate = str(value).strip().lower()
            if candidate not in self.choices:
                raise SettingsError(
                    f"{self.key} must be one of {', '.join(self.choices)}"
                )
            return candidate

        if self.kind == "str":
            return str(value).strip()

        try:
            number = int(value) if self.kind == "int" else float(value)
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{self.key} must be a number") from exc
        if self.minimum is not None and number < self.minimum:
            raise SettingsError(f"{self.key} must be at least {self.minimum:g}")
        if self.maximum is not None and number > self.maximum:
            raise SettingsError(f"{self.key} must be at most {self.maximum:g}")
        return number


def _specs() -> dict[str, SettingSpec]:
    entries = [
        SettingSpec(
            key=EXECUTION_MODE_SETTING,
            kind="enum",
            default=SUBSCRIPTION_ONLY,
            choices=tuple(MODES),
            label="Execution mode",
            description=(
                "Subscriptions only runs every role on the Claude Code and Codex "
                "CLIs and registers no local model backends. Choose hybrid on a "
                "host with an accelerator and a benched routing table."
            ),
            section="execution",
            env_var=EXECUTION_MODE_ENV,
        ),
        SettingSpec(
            key="scheduler.max_concurrent_builds",
            kind="int",
            default=1,
            minimum=1,
            maximum=8,
            label="Concurrent builds",
            description=(
                "How many builds may run at once. One is correct on a small host; "
                "the agents are network-bound but the target repository's own "
                "install and test cycle is not."
            ),
            section="limits",
            env_var="CTSWARM_MAX_CONCURRENT_BUILDS",
            applies_to="restart",
        ),
        SettingSpec(
            key="scheduler.no_progress_timeout_s",
            kind="int",
            default=1800,
            minimum=300,
            maximum=86400,
            label="No-progress timeout",
            description=(
                "Seconds without a semantic change before a build is treated as "
                "stalled. Raise it on a slow host, where a healthy build can look "
                "stalled while a dependency install runs."
            ),
            section="limits",
            env_var="CTSWARM_NO_PROGRESS_TIMEOUT_S",
        ),
        SettingSpec(
            key="scheduler.agent_timeout_seconds",
            kind="int",
            default=900,
            minimum=120,
            maximum=7200,
            label="Per-agent timeout",
            description="Seconds one agent invocation may take before it is cut off.",
            section="limits",
            env_var="CTSWARM_AGENT_TIMEOUT_SECONDS",
        ),
        SettingSpec(
            key="evidence.require_browser",
            kind="bool",
            default=True,
            label="Require browser evidence",
            description=(
                "Builds that change a user interface must produce screenshots and "
                "passing scripted flows before they can report success."
            ),
            section="evidence",
        ),
        SettingSpec(
            key="evidence.retention_days",
            kind="int",
            default=14,
            minimum=1,
            maximum=365,
            label="Evidence retention",
            description=(
                "Days to keep screenshots and attached documents after a build "
                "finishes. Storage on a small host is not free."
            ),
            section="evidence",
        ),
        SettingSpec(
            key="evidence.max_artifact_mb",
            kind="int",
            default=200,
            minimum=10,
            maximum=2000,
            label="Artifact budget per build",
            description="Megabytes of screenshots retained per build, newest first.",
            section="evidence",
        ),
        SettingSpec(
            key="documents.max_upload_mb",
            kind="int",
            default=25,
            minimum=1,
            maximum=200,
            label="Max document size",
            description="Largest single reference document accepted at launch.",
            section="documents",
        ),
        SettingSpec(
            key="documents.max_total_mb",
            kind="int",
            default=100,
            minimum=1,
            maximum=500,
            label="Max documents per build",
            description="Total megabytes of reference documents attached to one build.",
            section="documents",
        ),
        SettingSpec(
            key="network.bind_mode",
            kind="enum",
            default="loopback",
            choices=("loopback", "tailnet"),
            label="Network exposure",
            description=(
                "Decided when the stack starts, not from the browser. Shown here "
                "so the audit trail records what the host was actually doing."
            ),
            section="network",
            env_var="CTSWARM_BIND_MODE",
            read_only=True,
            applies_to="restart",
        ),
    ]
    return {spec.key: spec for spec in entries}


SETTING_SPECS: dict[str, SettingSpec] = _specs()

SECTION_LABELS = {
    "execution": "Execution",
    "limits": "Limits and timeouts",
    "evidence": "Evidence",
    "documents": "Documents",
    "network": "Network",
}


@dataclass(frozen=True)
class Resolved:
    """One effective value plus where it came from."""

    spec: SettingSpec
    value: Any
    source: Literal["ledger", "env", "default"]
    pinned: bool = False
    notes: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "key": self.spec.key,
            "value": self.value,
            "source": self.source,
            "pinned": self.pinned,
            "default": self.spec.default,
            "kind": self.spec.kind,
            "choices": list(self.spec.choices),
            "minimum": self.spec.minimum,
            "maximum": self.spec.maximum,
            "label": self.spec.label,
            "description": self.spec.description,
            "section": self.spec.section,
            "applies_to": self.spec.applies_to,
            "read_only": self.spec.read_only or self.pinned,
            "notes": self.notes,
            **self.extra,
        }


def resolve(ledger: Ledger, key: str, env: dict | None = None) -> Resolved:
    """Return one effective value with its provenance."""
    env = env if env is not None else dict(os.environ)
    spec = SETTING_SPECS.get(key)
    if spec is None:
        raise SettingsError(f"unknown setting: {key}")

    # The execution mode is the one setting the environment may pin outright,
    # because a host that cannot run local models must not be talked into
    # trying by a value stored before it moved there.
    if spec.key == EXECUTION_MODE_SETTING and env_pinned(env):
        return Resolved(
            spec=spec,
            value=spec.coerce(env[spec.env_var]),
            source="env",
            pinned=True,
            notes=f"pinned by {spec.env_var} on this host",
        )

    # A stored or inherited value can stop validating: bounds tighten between
    # versions, or a database moves to a host whose environment disagrees. The
    # fallback is deliberate, but it is never silent -- the reason travels with
    # the value so the settings screen can say what was ignored and why.
    rejected: list[str] = []

    stored = ledger.setting(spec.key, None)
    if stored is not None:
        try:
            return Resolved(spec=spec, value=spec.coerce(stored), source="ledger")
        except SettingsError as exc:
            rejected.append(f"stored value {stored!r} ignored: {exc}")

    inherited = env.get(spec.env_var, "").strip() if spec.env_var else ""
    if inherited:
        try:
            return Resolved(
                spec=spec,
                value=spec.coerce(inherited),
                source="env",
                notes="; ".join([*rejected, f"from {spec.env_var}"]),
            )
        except SettingsError as exc:
            rejected.append(f"{spec.env_var}={inherited!r} ignored: {exc}")

    return Resolved(
        spec=spec,
        value=spec.default,
        source="default",
        notes="; ".join(rejected),
    )


def get_setting(ledger: Ledger, key: str, env: dict | None = None) -> Any:
    """Effective value only, for hot paths that do not care about provenance."""
    return resolve(ledger, key, env).value


def load_settings(ledger: Ledger, env: dict | None = None) -> list[dict]:
    """Every setting, resolved, in a stable display order."""
    order = list(SECTION_LABELS)
    resolved = [resolve(ledger, key, env) for key in SETTING_SPECS]
    resolved.sort(key=lambda item: (order.index(item.spec.section), item.spec.key))
    return [item.to_dict() for item in resolved]


def save_settings(
    ledger: Ledger,
    changes: dict[str, Any],
    *,
    changed_by: str = "mission-control",
    env: dict | None = None,
) -> list[dict]:
    """Validate every change, then apply them all.

    Validation happens up front for the whole payload. Applying half of a
    settings change leaves the host in a configuration nobody chose and nobody
    can describe, which is worse than rejecting the request.
    """
    if not isinstance(changes, dict):
        raise SettingsError("settings payload must be an object")

    env = env if env is not None else dict(os.environ)
    validated: dict[str, Any] = {}
    for key, value in changes.items():
        spec = SETTING_SPECS.get(key)
        if spec is None:
            raise SettingsError(f"unknown setting: {key}")
        if spec.read_only:
            raise SettingsError(f"{key} is decided by the host and cannot be set here")
        current = resolve(ledger, key, env)
        if current.pinned:
            raise SettingsError(
                f"{key} is pinned by {spec.env_var} on this host; change the "
                "environment rather than the stored value"
            )
        validated[key] = spec.coerce(value)

    for key, value in validated.items():
        previous = resolve(ledger, key, env).value
        ledger.set_setting(key, value)
        ledger.record_event(
            SETTING_UPDATED,
            {
                "key": key,
                "changed_by": changed_by,
                "previous": previous,
                "value": value,
            },
        )
    return load_settings(ledger, env)


def subscription_only_setting(ledger: Ledger, env: dict | None = None) -> bool:
    """The execution mode as a predicate, resolved through this module."""
    return get_setting(ledger, EXECUTION_MODE_SETTING, env) == SUBSCRIPTION_ONLY


__all__ = [
    "HYBRID",
    "SECTION_LABELS",
    "SETTING_SPECS",
    "SETTING_UPDATED",
    "SUBSCRIPTION_ONLY",
    "Resolved",
    "SettingSpec",
    "SettingsError",
    "get_setting",
    "load_settings",
    "resolve",
    "save_settings",
    "subscription_only_setting",
]
