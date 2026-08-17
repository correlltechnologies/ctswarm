"""Settings: precedence, validation, and the audit trail.

The interesting behaviour is not "a value round-trips". It is what happens when
values disagree -- a stored value against an environment variable, a stored
value that no longer validates, a pinned host. Those are the cases that decide
whether an operator can trust the screen.
"""

from __future__ import annotations

import pytest

from ctswarm.execution_mode import EXECUTION_MODE_SETTING
from ctswarm.ledger import Ledger
from ctswarm.settings import (
    SETTING_SPECS,
    SETTING_UPDATED,
    SettingsError,
    get_setting,
    load_settings,
    resolve,
    save_settings,
)


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "settings.db")


def _by_key(settings: list[dict], key: str) -> dict:
    return next(item for item in settings if item["key"] == key)


# -- precedence -----------------------------------------------------------


def test_an_untouched_host_reports_defaults(ledger) -> None:
    resolved = resolve(ledger, "scheduler.no_progress_timeout_s", env={})

    assert resolved.value == 1800
    assert resolved.source == "default"


def test_the_environment_is_the_boot_floor(ledger) -> None:
    """A fresh Pi must come up configured before anyone opens the dashboard."""
    resolved = resolve(
        ledger,
        "scheduler.no_progress_timeout_s",
        env={"CTSWARM_NO_PROGRESS_TIMEOUT_S": "5400"},
    )

    assert resolved.value == 5400
    assert resolved.source == "env"
    assert "CTSWARM_NO_PROGRESS_TIMEOUT_S" in resolved.notes


def test_an_operator_choice_outranks_the_environment(ledger) -> None:
    """Otherwise a change made in the UI silently reverts on the next restart."""
    env = {"CTSWARM_NO_PROGRESS_TIMEOUT_S": "5400"}
    save_settings(ledger, {"scheduler.no_progress_timeout_s": 2400}, env=env)

    resolved = resolve(ledger, "scheduler.no_progress_timeout_s", env=env)
    assert resolved.value == 2400
    assert resolved.source == "ledger"


def test_execution_mode_pinned_by_the_host_cannot_be_overridden(ledger) -> None:
    """A host that cannot run local models must not be talked into trying."""
    env = {"CTSWARM_EXECUTION_MODE": "subscription_only"}
    ledger.set_setting(EXECUTION_MODE_SETTING, "hybrid")

    resolved = resolve(ledger, EXECUTION_MODE_SETTING, env=env)
    assert resolved.value == "subscription_only"
    assert resolved.pinned is True
    assert resolved.to_dict()["read_only"] is True

    with pytest.raises(SettingsError) as excinfo:
        save_settings(ledger, {EXECUTION_MODE_SETTING: "hybrid"}, env=env)
    assert "pinned" in str(excinfo.value)


# -- validation -----------------------------------------------------------


def test_out_of_range_values_are_refused(ledger) -> None:
    with pytest.raises(SettingsError) as excinfo:
        save_settings(ledger, {"scheduler.max_concurrent_builds": 99})
    assert "at most" in str(excinfo.value)


def test_unknown_settings_are_refused(ledger) -> None:
    with pytest.raises(SettingsError):
        save_settings(ledger, {"scheduler.turbo": True})


def test_a_read_only_setting_cannot_be_written(ledger) -> None:
    """Network exposure is decided when the stack starts, not from a browser."""
    with pytest.raises(SettingsError) as excinfo:
        save_settings(ledger, {"network.bind_mode": "tailnet"})
    assert "host" in str(excinfo.value)


def test_one_bad_key_rejects_the_whole_payload(ledger) -> None:
    """A half-applied config is a state nobody chose and nobody can describe."""
    with pytest.raises(SettingsError):
        save_settings(
            ledger,
            {
                "scheduler.max_concurrent_builds": 2,
                "scheduler.no_progress_timeout_s": -1,
            },
        )

    assert get_setting(ledger, "scheduler.max_concurrent_builds", env={}) == 1


def test_booleans_accept_the_spellings_a_form_actually_sends(ledger) -> None:
    save_settings(ledger, {"evidence.require_browser": "false"})
    assert get_setting(ledger, "evidence.require_browser", env={}) is False


def test_a_stored_value_that_stopped_validating_says_so(ledger) -> None:
    """Silently showing a default would hide that a choice was discarded."""
    ledger.set_setting("scheduler.max_concurrent_builds", 999)

    resolved = resolve(ledger, "scheduler.max_concurrent_builds", env={})
    assert resolved.value == 1
    assert resolved.source == "default"
    assert "999" in resolved.notes and "ignored" in resolved.notes


# -- audit ----------------------------------------------------------------


def test_every_change_records_who_and_what(ledger) -> None:
    save_settings(
        ledger, {"evidence.retention_days": 30}, changed_by="quinn", env={}
    )

    events = ledger.events(kind=SETTING_UPDATED)
    assert len(events) == 1
    detail = events[-1]["detail"]
    assert "quinn" in detail
    assert "evidence.retention_days" in detail
    # The previous value is what makes the trail useful for undoing a change.
    assert '"previous": 14' in detail


# -- shape ----------------------------------------------------------------


def test_every_setting_declares_a_section_that_exists() -> None:
    from ctswarm.settings import SECTION_LABELS

    assert all(spec.section in SECTION_LABELS for spec in SETTING_SPECS.values())


def test_load_reports_provenance_for_every_setting(ledger) -> None:
    settings = load_settings(ledger, env={})

    assert len(settings) == len(SETTING_SPECS)
    assert all(item["source"] in {"ledger", "env", "default"} for item in settings)
    # A screen that shows a number without saying where it came from invites
    # changing the wrong thing.
    assert all(item["label"] and item["description"] for item in settings)


def test_defaults_are_valid_against_their_own_specs() -> None:
    for spec in SETTING_SPECS.values():
        assert spec.coerce(spec.default) == spec.default
