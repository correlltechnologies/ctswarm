"""Regression tests for the native scoped build path.

Every test here pins one of the rules that the full pipeline learned the
expensive way: the change gate runs before the reviewer, a reviewer that cannot
answer is a rejection, an unavailable scanner is not a pass, and every harness
call is metered whether or not it succeeded.

Nothing touches git, a harness, or the network. The runner seam is the only
place the module reaches outside itself, so a fake shell covers the whole flow.
"""

from __future__ import annotations

import pytest

from ctswarm.capacity import CapacityManager, Headroom, Runtime
from ctswarm.evidence.scanners import ScanOutcome
from ctswarm.ledger import Ledger
from ctswarm.scoped import (
    ScopedBuild,
    classify_failure,
    parse_claude_json,
    parse_verdict,
)

CLAUDE_OK = (
    0,
    '{"result": "Added the button and a test.", "total_cost_usd": 0.21,'
    ' "usage": {"input_tokens": 900, "output_tokens": 120}}',
    "",
)
APPROVE = (
    0,
    '{"result": "Reads well.\\n\\nVERDICT: APPROVE", "total_cost_usd": 0.05,'
    ' "usage": {"input_tokens": 400, "output_tokens": 40}}',
    "",
)
REJECT = (
    0,
    '{"result": "VERDICT: REJECT the new branch is untested", '
    '"total_cost_usd": 0.05, "usage": {}}',
    "",
)
#: Codex `exec` prints prose rather than JSON, so when it is the independent
#: reviewer its verdict arrives as plain text.
APPROVE_TEXT = (0, "Reads well.\n\nVERDICT: APPROVE", "")
REJECT_TEXT = (0, "VERDICT: REJECT the new branch is untested", "")


class FakeShell:
    """Scripted stand-in for every subprocess the module runs."""

    def __init__(self, *, harness: list[tuple[int, str, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.harness = list(harness or [])
        self.status = " M src/Form.tsx\n"
        self.diff_names = "src/Form.tsx\n"
        self.diff = "diff --git a/src/Form.tsx b/src/Form.tsx\n+  <button/>\n"

    @property
    def harness_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] in ("claude", "codex")]

    async def __call__(self, command, *, cwd=None, timeout=0.0, env=None):
        self.calls.append(list(command))
        head = command[0]

        if head in ("claude", "codex"):
            if not self.harness:
                raise AssertionError(f"unscripted harness call: {command}")
            return self.harness.pop(0)

        if head == "gh":
            return 0, "https://github.com/o/r/pull/7\n", ""

        if head == "git":
            return self._git(command[1:])

        raise AssertionError(f"unexpected command: {command}")

    def _git(self, args):
        verb = args[0]
        if verb == "rev-parse" and "--abbrev-ref" in args:
            return 0, "main\n", ""
        if verb == "rev-parse":
            return 0, "abc1234\n", ""
        if verb == "status":
            return 0, self.status, ""
        if verb == "diff" and "--name-only" in args:
            return 0, self.diff_names, ""
        if verb == "diff":
            return 0, self.diff, ""
        return 0, "", ""


@pytest.fixture
def build(tmp_path, monkeypatch):
    """A ScopedBuild whose harnesses exist and whose subscriptions have room."""
    monkeypatch.setattr("ctswarm.scoped.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        CapacityManager,
        "headroom",
        lambda self, runtime: Headroom(runtime, True, 1.0, 0.0, 0, 0.0, "test"),
    )
    monkeypatch.setattr(
        "ctswarm.scoped.scan_tests",
        lambda path, command=None: ScanOutcome("tests", "passed", "suite passed"),
    )
    monkeypatch.setattr(
        "ctswarm.scoped._non_test_scanners",
        lambda path, base: [
            ScanOutcome("secrets", "passed", "clean"),
            ScanOutcome("anti-slop", "passed", "clean"),
        ],
    )
    ledger = Ledger(tmp_path / "ledger.db")

    def make(shell: FakeShell) -> ScopedBuild:
        return ScopedBuild(
            ledger=ledger,
            capacity=CapacityManager(ledger=ledger),
            workspace_root=tmp_path / "workspaces",
            runner=shell,
        )

    make.ledger = ledger
    return make


# -- the happy path ---------------------------------------------------------


async def test_scoped_build_completes_and_opens_a_draft_pr(build) -> None:
    shell = FakeShell(harness=[CLAUDE_OK, APPROVE_TEXT])
    result = await build(shell).run(
        goal="Add a button that clears the form",
        repo_url="https://github.com/o/r",
        build_id="build-abc123",
    )

    assert result.success is True
    assert result.outcome == "complete"
    assert result.files_changed == ["src/Form.tsx"]
    assert result.review_approved is True
    assert result.pr_url == "https://github.com/o/r/pull/7"
    assert result.branch.startswith("scoped/add-a-button")
    # The whole point of the tier: three harness calls, not four hundred.
    assert result.harness_calls == 2
    assert result.cost_usd == pytest.approx(0.21)
    assert any(c[:3] == ["gh", "pr", "create"] for c in shell.calls)
    assert "--draft" in next(c for c in shell.calls if c[0] == "gh")


async def test_implementer_gets_write_access_and_reviewer_does_not(build) -> None:
    shell = FakeShell(harness=[CLAUDE_OK, APPROVE_TEXT])
    await build(shell).run(goal="Add a button", repo_url="https://github.com/o/r")

    implementer, reviewer = shell.harness_calls
    assert "--permission-mode" in implementer
    assert implementer[implementer.index("--permission-mode") + 1] == "acceptEdits"
    # A reviewer that can change the repository is not a reviewer. Codex
    # says so with a sandbox mode, pinned rather than inherited so a
    # permissive global config cannot hand it write access.
    assert reviewer[0] == "codex"
    assert reviewer[reviewer.index("--sandbox") + 1] == "read-only"


# -- the change gate --------------------------------------------------------


async def test_empty_diff_is_rejected_before_any_review_is_spent(build) -> None:
    shell = FakeShell(harness=[CLAUDE_OK])
    shell.status = ""
    shell.diff_names = ""

    result = await build(shell).run(
        goal="Add a button", repo_url="https://github.com/o/r"
    )

    assert result.success is False
    assert result.outcome == "no_changes"
    # One call, not two. The reviewer was never asked.
    assert result.harness_calls == 1
    assert result.review_approved is None


async def test_committed_work_counts_as_a_change(build) -> None:
    """A coder that committed must not read as having changed nothing."""
    shell = FakeShell(harness=[CLAUDE_OK, APPROVE_TEXT])
    shell.status = ""  # nothing uncommitted
    shell.diff_names = "src/Form.tsx\nsrc/Form.test.tsx\n"

    result = await build(shell).run(
        goal="Add a button", repo_url="https://github.com/o/r"
    )

    assert result.outcome == "complete"
    assert result.files_changed == ["src/Form.test.tsx", "src/Form.tsx"]


# -- review is fail-closed --------------------------------------------------


async def test_reviewer_error_is_a_rejection_not_an_approval(build) -> None:
    shell = FakeShell(harness=[CLAUDE_OK, (1, "", "reviewer crashed")])

    result = await build(shell).run(
        goal="Add a button", repo_url="https://github.com/o/r"
    )

    assert result.success is False
    assert result.outcome == "review_unavailable"
    assert result.review_approved is False
    assert result.pr_url == ""


async def test_missing_verdict_line_is_a_rejection(build) -> None:
    silent = (0, '{"result": "Seems fine to me.", "total_cost_usd": 0.0}', "")
    shell = FakeShell(harness=[CLAUDE_OK, silent])

    result = await build(shell).run(
        goal="Add a button", repo_url="https://github.com/o/r"
    )

    assert result.outcome == "review_rejected"
    assert result.review_approved is False
    assert "no verdict" in result.review_summary


async def test_explicit_rejection_blocks_the_pull_request(build) -> None:
    shell = FakeShell(harness=[CLAUDE_OK, REJECT_TEXT])

    result = await build(shell).run(
        goal="Add a button", repo_url="https://github.com/o/r"
    )

    assert result.outcome == "review_rejected"
    assert result.review_summary == "the new branch is untested"
    assert not any(c[0] == "gh" for c in shell.calls)


# -- scanners decide --------------------------------------------------------


async def test_an_unavailable_scanner_is_not_a_pass(build, monkeypatch) -> None:
    monkeypatch.setattr(
        "ctswarm.scoped._non_test_scanners",
        lambda path, base: [ScanOutcome("secrets", "unavailable", "gitleaks missing")],
    )
    shell = FakeShell(harness=[CLAUDE_OK, APPROVE_TEXT])

    result = await build(shell).run(
        goal="Add a button", repo_url="https://github.com/o/r"
    )

    assert result.success is False
    assert result.outcome == "scanners_failed"
    assert "secrets" in result.detail
    # An approving reviewer cannot vote away a gate that never ran.
    assert result.review_approved is True


# -- the repair loop is bounded ---------------------------------------------


async def test_failing_tests_get_exactly_one_repair_attempt(build, monkeypatch) -> None:
    attempts = {"n": 0}

    def failing(path, command=None):
        attempts["n"] += 1
        return ScanOutcome("tests", "failed", "2 failed", ("Form renders",))

    monkeypatch.setattr("ctswarm.scoped.scan_tests", failing)
    shell = FakeShell(harness=[CLAUDE_OK, CLAUDE_OK])

    result = await build(shell).run(
        goal="Add a button", repo_url="https://github.com/o/r"
    )

    assert result.outcome == "tests_failed"
    # Implement, one repair, and then it stops rather than looping.
    assert result.harness_calls == 2
    assert attempts["n"] == 2
    assert shell.harness_calls[1][0] == "claude"


# -- metering ---------------------------------------------------------------


async def test_every_harness_call_is_recorded_including_failures(build) -> None:
    shell = FakeShell(harness=[CLAUDE_OK, (1, "", "boom")])
    runner = build(shell)

    await runner.run(
        goal="Add a button", repo_url="https://github.com/o/r", build_id="build-meter"
    )

    calls = runner.ledger.recent_calls(build_id="build-meter")
    assert len(calls) == 2
    assert {c["backend"] for c in calls} == {
        "runtime:claude_code",
        "runtime:codex",
    }
    assert sorted(c["ok"] for c in calls) == [0, 1]
    # The subscription's own cost figure is the only usage signal there is.
    assert sum(c["cost_usd"] for c in calls) == pytest.approx(0.21)


async def test_a_spent_subscription_is_reported_as_capacity_not_as_a_bug(build) -> None:
    limited = (1, "", "Claude usage limit reached. Resets at 5pm.")
    shell = FakeShell(harness=[limited])
    runner = build(shell)

    result = await runner.run(
        goal="Add a button", repo_url="https://github.com/o/r", build_id="build-limit"
    )

    assert result.outcome == "capacity_exhausted"
    # The correction signal capacity reads, so the next build knows before
    # it spends an agent timeout finding out the same way.
    limited = runner.ledger.events(kind="runtime_rate_limited")
    assert len(limited) == 1
    assert "usage limit" in limited[0]["detail"]
    # The call still happened and still drew on the window, so it is metered
    # even though it produced nothing.
    assert len(runner.ledger.recent_calls(build_id="build-limit")) == 1


async def test_a_zero_exit_with_no_output_is_not_success(build) -> None:
    """An exhausted subscription arrives here with no output and no error flag."""
    shell = FakeShell(harness=[(0, '{"result": ""}', "")])

    result = await build(shell).run(
        goal="Add a button", repo_url="https://github.com/o/r"
    )

    assert result.success is False
    assert result.outcome == "implementer_failed"


# -- independence -----------------------------------------------------------


async def test_two_subscriptions_give_an_independent_reviewer(build) -> None:
    shell = FakeShell(harness=[CLAUDE_OK, (0, "VERDICT: APPROVE", "")])

    result = await build(shell).run(
        goal="Add a button", repo_url="https://github.com/o/r"
    )

    assert [c[0] for c in shell.harness_calls] == ["claude", "codex"]
    assert "independent review" in result.detail


async def test_one_subscription_says_review_is_not_independent(
    build, monkeypatch
) -> None:
    monkeypatch.setattr(
        CapacityManager,
        "headroom",
        lambda self, runtime: Headroom(
            runtime,
            runtime is Runtime.CLAUDE_CODE,
            1.0,
            0.0,
            0,
            0.0,
            "only claude has room",
        ),
    )
    shell = FakeShell(harness=[CLAUDE_OK, APPROVE])

    result = await build(shell).run(
        goal="Add a button", repo_url="https://github.com/o/r"
    )

    assert [c[0] for c in shell.harness_calls] == ["claude", "claude"]
    assert "NOT independent" in result.detail


async def test_no_available_harness_blocks_before_cloning(build, monkeypatch) -> None:
    monkeypatch.setattr(
        CapacityManager,
        "headroom",
        lambda self, runtime: Headroom(runtime, False, 0.0, 0.0, 0, 0.0, "spent"),
    )
    shell = FakeShell()

    result = await build(shell).run(
        goal="Add a button", repo_url="https://github.com/o/r"
    )

    assert result.outcome == "blocked"
    assert "spent" in result.detail
    # Nothing was cloned, so nothing has to be cleaned up.
    assert shell.calls == []


# -- pure helpers -----------------------------------------------------------


def test_parse_verdict_reads_the_last_line_only() -> None:
    assert parse_verdict("VERDICT: REJECT no\n\nVERDICT: APPROVE")[0] is True
    assert parse_verdict("nothing here") == (
        None,
        "reviewer returned no verdict line",
    )


def test_parse_claude_json_survives_a_stream_of_events() -> None:
    stream = (
        '[{"type": "assistant"}, {"type": "result", "result": "done",'
        ' "total_cost_usd": 1.5, "usage": {"input_tokens": 3, "output_tokens": 4}}]'
    )
    assert parse_claude_json(stream) == ("done", 3, 4, 1.5)


def test_parse_claude_json_falls_back_to_raw_text() -> None:
    assert parse_claude_json("not json at all") == ("not json at all", 0, 0, 0.0)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Claude usage limit reached", "rate_limited"),
        ("429 Too Many Requests", "rate_limited"),
        ("You are not logged in", "auth_error"),
        ("segmentation fault", "harness_error"),
    ],
)
def test_classify_failure(text: str, kind: str) -> None:
    assert classify_failure(text) == kind
