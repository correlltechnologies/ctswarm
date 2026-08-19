"""Tests for the credential detection in bootstrap.sh.

bootstrap.sh writes an empty `{}` placeholder at the Claude credentials path so
Docker bind mounts stay files rather than becoming directories. It then reports
which runtimes are configured. Those two facts collided once already: the report
used a bare `-f` test, so it saw the placeholder the script itself had just
written and announced a login that did not exist, which in turn dropped the one
genuinely blocking step from the summary.

These tests pin the predicate to the same rule ctswarm/capacity.py applies.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPOSITORY_ROOT / "bootstrap.sh"


def _credentials_present_source() -> str:
    """The shell function as it is actually shipped, not a copy of it."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    match = re.search(
        r"^credentials_present\(\) \{\n.*?^\}\n", text, re.MULTILINE | re.DOTALL
    )
    assert match, "credentials_present() is no longer defined in bootstrap.sh"
    return match.group(0)


def _accepts(path: Path) -> bool:
    completed = subprocess.run(
        ["bash", "-c", f'{_credentials_present_source()}\ncredentials_present "$1"', "_", str(path)],
        capture_output=True,
        cwd=REPOSITORY_ROOT,
    )
    return completed.returncode == 0


def test_a_real_login_is_recognized(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps({"tokens": {"access_token": "sk-ant-oat01-example"}}),
        encoding="utf-8",
    )
    assert _accepts(path)


def test_the_placeholder_bootstrap_writes_is_not_a_login(tmp_path: Path) -> None:
    """This is the regression. `{}` is the placeholder, not a credential."""
    path = tmp_path / ".credentials.json"
    path.write_text("{}\n", encoding="utf-8")
    assert not _accepts(path)


@pytest.mark.parametrize("payload", ["", "   ", "not json", "[]", "null", '"token"'])
def test_nothing_unusable_counts_as_a_login(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "auth.json"
    path.write_text(payload, encoding="utf-8")
    assert not _accepts(path)


def test_a_missing_file_is_not_a_login(tmp_path: Path) -> None:
    assert not _accepts(tmp_path / "absent.json")


def test_a_directory_is_not_a_login(tmp_path: Path) -> None:
    """What the bind mount leaves behind when the host file was never created."""
    path = tmp_path / "auth.json"
    path.mkdir()
    assert not _accepts(path)


def _fill_if_empty_source() -> str:
    """The shell function as it is actually shipped, not a copy of it."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    match = re.search(r"^fill_if_empty\(\) \{\n.*?^\}\n", text, re.MULTILINE | re.DOTALL)
    assert match, "fill_if_empty() is no longer defined in bootstrap.sh"
    return match.group(0)


def _fill(env_text: str, key: str, value: str, tmp_path: Path) -> tuple[bool, str]:
    (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    completed = subprocess.run(
        ["bash", "-c", f'{_fill_if_empty_source()}\nfill_if_empty "$1"', "_", key],
        capture_output=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "CTSWARM_FILL_VALUE": value},
    )
    return completed.returncode == 0, (tmp_path / ".env").read_text(encoding="utf-8")


def test_an_empty_key_is_filled(tmp_path: Path) -> None:
    """The case that matters: `gh auth login` happened after the first bootstrap."""
    filled, text = _fill("GH_TOKEN=\nOTHER=1\n", "GH_TOKEN", "gho_example", tmp_path)

    assert filled
    assert "GH_TOKEN=gho_example\n" in text
    assert "OTHER=1\n" in text


def test_an_existing_value_is_never_overwritten(tmp_path: Path) -> None:
    """The operator's own token outranks anything this script can discover."""
    filled, text = _fill("GH_TOKEN=mine\n", "GH_TOKEN", "gho_example", tmp_path)

    assert not filled
    assert text == "GH_TOKEN=mine\n"


def test_a_key_that_is_not_there_is_not_appended(tmp_path: Path) -> None:
    """.env.example defines the keys; inventing one hides a typo in the name."""
    filled, text = _fill("OTHER=1\n", "GH_TOKEN", "gho_example", tmp_path)

    assert not filled
    assert "GH_TOKEN" not in text


def test_nothing_happens_without_a_value(tmp_path: Path) -> None:
    """`gh auth token` can succeed and print nothing."""
    filled, text = _fill("GH_TOKEN=\n", "GH_TOKEN", "", tmp_path)

    assert not filled
    assert text == "GH_TOKEN=\n"


def test_a_commented_key_is_left_alone(tmp_path: Path) -> None:
    """A commented line is the operator's note, not a slot to fill."""
    filled, text = _fill("# GH_TOKEN=\n", "GH_TOKEN", "gho_example", tmp_path)

    assert not filled
    assert text == "# GH_TOKEN=\n"
