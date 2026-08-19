"""Scoped builds: one change, one branch, no factory.

This is the small path. It exists because the full SWE-AF pipeline is a
feature-decomposition engine, and most requests are not features. Upstream's own
numbers put a `build` at four to five hundred agent instances and a scoped issue
at four to eight LLM calls, and until now ctswarm only ever asked for the first
one. "Add a button" paid for a product manager, an architect, a tech lead, and a
sprint planner before a line of code existed.

A scoped build is at most three harness invocations: implement, optionally
repair once, review. There is no planner, no issue DAG, no worktree fan-out, no
adaptation tier, and no replanner. Nothing here talks to AgentField, so nothing
here needs the control plane, Postgres, or the two agent containers.

Four rules are load-bearing, and each one is a bug that was found the expensive
way in the full pipeline:

**The change gate runs before the reviewer.** Git decides whether work happened,
not the harness's own report. A coder that claims completion having changed
nothing is rejected without spending a review on it.

**A reviewer that errors is a rejection.** Not an approval, not a skip. The
whole point of an independent reviewer is that it can say no, and a failure to
answer is not a yes.

**A scanner that cannot run has not passed.** `summarize` already treats
`unavailable` as failure; this module does not second-guess it.

**Harness usage is recorded on every call.** `CapacityManager.record_usage` had
no production caller anywhere, so the ledger saw no harness calls, headroom
reported available forever, and the first real signal that a subscription was
spent arrived as an empty verifier response mid-build. Every invocation here
reports what it cost.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .capacity import CapacityManager, Runtime
from .evidence.scanners import ScanOutcome, scan_tests, summarize
from .ledger import Ledger

#: How long one harness invocation may run before it is killed. A scoped change
#: that has not finished in this long is not going to, and on a small host a
#: wedged harness holds memory as well as a subscription slot.
HARNESS_TIMEOUT_S = float(os.environ.get("CTSWARM_SCOPED_HARNESS_TIMEOUT_S", "1800"))

#: Git operations are fast or broken. Cloning a large repository over a slow
#: link is the one exception, so the clone gets its own longer budget.
GIT_TIMEOUT_S = 120.0
CLONE_TIMEOUT_S = float(os.environ.get("CTSWARM_SCOPED_CLONE_TIMEOUT_S", "900"))

#: One repair attempt after a failing test run. Zero would throw away a cheap
#: and usually successful fix; more than one starts rebuilding the repair loop
#: that spent eight hours on sixty-seven non-converging cycles.
DEFAULT_MAX_REPAIRS = int(os.environ.get("CTSWARM_SCOPED_MAX_REPAIRS", "1"))

#: Artifacts the harness may leave behind that are never part of the change.
IGNORED_PREFIXES = (".artifacts/", ".worktrees/", ".claude/", ".codex/")

MODEL_ENV = {
    Runtime.CLAUDE_CODE: ("CTSWARM_SCOPED_CLAUDE_MODEL", "sonnet"),
    Runtime.CODEX: ("CTSWARM_SCOPED_CODEX_MODEL", "gpt-5.5"),
}

HARNESS_BINARY = {Runtime.CLAUDE_CODE: "claude", Runtime.CODEX: "codex"}


def model_for(runtime: Runtime) -> str:
    """The model id to address a harness by, overridable per host.

    Runtime-specific rather than shared: a `ctswarm/med` alias leaking into the
    Claude CLI hung it outright, which is why routing aliases never cross a
    harness boundary.
    """
    env_name, fallback = MODEL_ENV.get(runtime, ("", ""))
    return (os.environ.get(env_name, "") or fallback).strip()


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HarnessRun:
    """What one CLI invocation produced, including what it cost."""

    ok: bool
    role: str
    runtime: Runtime
    text: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    failure_kind: str = ""
    detail: str = ""
    duration_s: float = 0.0

    @property
    def rate_limited(self) -> bool:
        return self.failure_kind == "rate_limited"


@dataclass
class ScopedOutcome:
    """Everything a scoped build produced, successful or not.

    Deliberately verbose. The reason a build stopped is the single most useful
    thing an unattended factory can hand back, and summarising it to a boolean
    is how "it errored out" became the only available description of an hour.
    """

    success: bool = False
    outcome: str = "not_started"
    detail: str = ""
    branch: str = ""
    base_branch: str = ""
    worktree_path: str = ""
    commit: str = ""
    files_changed: list[str] = field(default_factory=list)
    pr_url: str = ""
    runs: list[HarnessRun] = field(default_factory=list)
    scanners: dict = field(default_factory=dict)
    review_approved: bool | None = None
    review_summary: str = ""

    @property
    def harness_calls(self) -> int:
        return len(self.runs)

    @property
    def cost_usd(self) -> float:
        return round(sum(run.cost_usd for run in self.runs), 4)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "outcome": self.outcome,
            "detail": self.detail,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "worktree_path": self.worktree_path,
            "commit": self.commit,
            "files_changed": list(self.files_changed),
            "pr_url": self.pr_url,
            "harness_calls": self.harness_calls,
            "cost_usd": self.cost_usd,
            "review_approved": self.review_approved,
            "review_summary": self.review_summary,
            "scanners": self.scanners,
            "runs": [
                {
                    "role": run.role,
                    "runtime": run.runtime.value,
                    "ok": run.ok,
                    "duration_s": round(run.duration_s, 1),
                    "cost_usd": run.cost_usd,
                    "failure_kind": run.failure_kind,
                    "detail": run.detail[:300],
                }
                for run in self.runs
            ],
        }


# ---------------------------------------------------------------------------
# subprocess seam
# ---------------------------------------------------------------------------


async def run_command(
    command: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: float,
    env: dict | None = None,
) -> tuple[int, str, str]:
    """Run one command and return (code, stdout, stderr).

    Never raises. A missing executable comes back as code -1 and a timeout as
    -2, because both are ordinary conditions on a host that may be missing a
    tool, and neither should take down the build loop with a traceback.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env if env is not None else os.environ.copy(),
        )
    except (OSError, ValueError) as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return -2, "", f"timed out after {timeout:.0f}s"

    return (
        process.returncode or 0,
        (stdout or b"").decode("utf-8", "replace"),
        (stderr or b"").decode("utf-8", "replace"),
    )


def classify_failure(text: str) -> str:
    """Map harness output onto the failure taxonomy capacity already reads."""
    lowered = text.lower()
    if any(
        marker in lowered
        for marker in ("rate limit", "usage limit", "quota", "too many requests")
    ):
        return "rate_limited"
    if any(
        marker in lowered
        for marker in ("not logged in", "unauthorized", "authentication", "please log in")
    ):
        return "auth_error"
    if "not found" in lowered and "command" in lowered:
        return "missing_harness"
    return "harness_error"


def parse_claude_json(out: str) -> tuple[str, int, int, float]:
    """Pull content and accounting out of `claude -p --output-format json`.

    `total_cost_usd` is the only usage figure a subscription exposes, which
    makes this the one place the ledger can learn what a build actually spent.
    """
    try:
        payload = json.loads(out)
    except (TypeError, ValueError):
        return out, 0, 0, 0.0
    if isinstance(payload, list):
        payload = next(
            (
                event
                for event in reversed(payload)
                if isinstance(event, dict) and event.get("type") == "result"
            ),
            {},
        )
    if not isinstance(payload, dict):
        return out, 0, 0, 0.0
    usage = payload.get("usage") or {}
    return (
        str(payload.get("result") or payload.get("content") or ""),
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        float(payload.get("total_cost_usd") or 0.0),
    )


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


def implementer_prompt(goal: str, *, repo_path: str, base_branch: str) -> str:
    return "\n".join(
        [
            "You are making one scoped change to an existing repository.",
            "",
            f"Repository: {repo_path}",
            f"Branch: you are already on a fresh branch cut from {base_branch}.",
            "",
            "## The change",
            goal,
            "",
            "## How to work",
            "- Read enough of the surrounding code to match its conventions,",
            "  naming, and test style. Write code that looks like its neighbours.",
            "- Make the smallest change that fully satisfies the request. Do not",
            "  refactor adjacent code, reformat files, or add abstractions that",
            "  the request did not ask for.",
            "- If the repository has tests, add or update the ones this change",
            "  affects, and run the suite.",
            "- Do not edit CI workflows, secrets, or dependency lockfiles unless",
            "  the request is specifically about them.",
            "",
            "## What finishing means",
            "Edit the files. A summary of what you would do is not the change,",
            "and the build is rejected without review if git sees no diff.",
            "You may commit or leave the work in the working tree; either is",
            "detected. Finish by stating in one paragraph what you changed and",
            "how you verified it.",
        ]
    )


def repair_prompt(goal: str, *, failure: str) -> str:
    return "\n".join(
        [
            "The change you just made does not pass the repository's own tests.",
            "",
            "## Original request",
            goal,
            "",
            "## Test output",
            failure[:6000],
            "",
            "## What to do",
            "Fix the cause. Do not weaken, skip, or delete a test to make it",
            "pass, and do not widen the change beyond what the original request",
            "asked for. If the failure is pre-existing and unrelated to your",
            "change, say so explicitly instead of editing around it.",
        ]
    )


def reviewer_prompt(goal: str, *, diff: str, files: list[str]) -> str:
    return "\n".join(
        [
            "You are reviewing a proposed change. You did not write it, and you",
            "cannot modify it. Judge it.",
            "",
            "## What was requested",
            goal,
            "",
            f"## Files changed ({len(files)})",
            "\n".join(f"- {name}" for name in files[:60]) or "- none",
            "",
            "## Diff",
            "```diff",
            diff[:120_000],
            "```",
            "",
            "## What to check",
            "- Does the change actually do what was requested?",
            "- Is it correct, including the edge cases the code around it cares",
            "  about?",
            "- Does it match the surrounding conventions?",
            "- Does it quietly do anything that was not asked for?",
            "- Are there security or data-loss consequences?",
            "",
            "## How to answer",
            "Write your assessment, then end with exactly one line, on its own,",
            "in one of these two forms:",
            "",
            "VERDICT: APPROVE",
            "VERDICT: REJECT <one sentence saying what must change>",
            "",
            "Approve only work you would merge. Uncertainty is a rejection.",
        ]
    )


VERDICT_PATTERN = re.compile(r"^VERDICT:\s*(APPROVE|REJECT)\b(.*)$", re.MULTILINE)


def parse_verdict(text: str) -> tuple[bool | None, str]:
    """Read a reviewer's verdict line.

    Returns (None, reason) when no verdict is present. The caller treats that as
    a rejection: a reviewer that did not answer has not approved anything, and
    defaulting an unparseable review to approval is exactly the fallback that
    made reviews decorative in the full pipeline.
    """
    matches = VERDICT_PATTERN.findall(text or "")
    if not matches:
        return None, "reviewer returned no verdict line"
    decision, remainder = matches[-1]
    return decision.upper() == "APPROVE", remainder.strip() or decision.title()


# ---------------------------------------------------------------------------
# the build
# ---------------------------------------------------------------------------


class ScopedBuildError(RuntimeError):
    """A scoped build could not start. Distinct from producing a bad result."""


class ScopedBuild:
    """Run one scoped change end to end, locally.

    Every external dependency is reached through a small overridable seam so the
    whole flow is testable on a host with no git, no harness, and no network.
    """

    def __init__(
        self,
        *,
        ledger: Ledger | None = None,
        capacity: CapacityManager | None = None,
        workspace_root: str | Path | None = None,
        max_repairs: int = DEFAULT_MAX_REPAIRS,
        harness_timeout_s: float = HARNESS_TIMEOUT_S,
        runner: Callable[..., object] | None = None,
    ) -> None:
        self.ledger = ledger or Ledger(os.environ.get("CTSWARM_DB", "var/ctswarm.db"))
        self.capacity = capacity or CapacityManager(ledger=self.ledger)
        self.workspace_root = Path(
            workspace_root
            or os.environ.get("CTSWARM_SCOPED_WORKSPACES", "var/scoped")
        )
        self.max_repairs = max(0, int(max_repairs))
        self.harness_timeout_s = harness_timeout_s
        self._run = runner or run_command

    # -- harness -----------------------------------------------------------

    def _harness_command(
        self, runtime: Runtime, *, prompt: str, writable: bool
    ) -> list[str]:
        model = model_for(runtime)
        if runtime is Runtime.CODEX:
            # Pinned rather than inherited: a permissive global codex config
            # must not be able to hand a reviewer write access.
            return [
                "codex",
                "exec",
                "--model",
                model,
                "--sandbox",
                "workspace-write" if writable else "read-only",
                "--skip-git-repo-check",
                prompt,
            ]
        command = [
            "claude",
            "-p",
            prompt,
            "--model",
            model,
            "--output-format",
            "json",
        ]
        if writable:
            # Nobody is here to approve a Write. Not bypassPermissions: Claude
            # Code refuses that mode outright when the process is root, and this
            # runs in a container that may be either.
            command += ["--permission-mode", "acceptEdits"]
        else:
            command += ["--allowed-tools", "Read,Grep,Glob"]
        return command

    async def invoke(
        self,
        runtime: Runtime,
        *,
        role: str,
        prompt: str,
        cwd: str | Path,
        writable: bool,
        build_id: str = "",
    ) -> HarnessRun:
        """Run one harness invocation and record what it cost.

        Never raises, and always writes a usage record, including for failures.
        A call that failed still consumed a slot against the subscription
        window, and a ledger that only sees successes cannot tell you that you
        are out.
        """
        started = time.monotonic()
        binary = HARNESS_BINARY[runtime]

        if shutil.which(binary) is None:
            run = HarnessRun(
                ok=False,
                role=role,
                runtime=runtime,
                failure_kind="missing_harness",
                detail=f"{binary} is not installed on this host",
                duration_s=time.monotonic() - started,
            )
            self._record(run, build_id)
            return run

        code, out, err = await self._run(
            self._harness_command(runtime, prompt=prompt, writable=writable),
            cwd=cwd,
            timeout=self.harness_timeout_s,
        )
        duration = time.monotonic() - started
        combined = f"{err}\n{out}".strip()

        if code == -2:
            run = HarnessRun(
                ok=False, role=role, runtime=runtime, failure_kind="timeout",
                detail=err or "harness timed out", duration_s=duration,
            )
            self._record(run, build_id)
            return run
        if code != 0:
            run = HarnessRun(
                ok=False, role=role, runtime=runtime,
                failure_kind=classify_failure(combined),
                detail=(combined or f"exit code {code}")[:600],
                duration_s=duration,
            )
            self._record(run, build_id)
            return run

        if runtime is Runtime.CLAUDE_CODE:
            text, prompt_tokens, output_tokens, cost = parse_claude_json(out)
        else:
            text, prompt_tokens, output_tokens, cost = out, 0, 0, 0.0

        if not text.strip():
            # A zero exit from a harness that answered nothing is not success.
            # An exhausted subscription reaches here with no output and no error
            # flag, and reading that as "the code failed acceptance" is what fed
            # the verifier's own breakage back in as work to do.
            run = HarnessRun(
                ok=False, role=role, runtime=runtime,
                failure_kind=classify_failure(combined) if combined else "empty_response",
                detail=(combined or "harness returned no content")[:600],
                duration_s=duration,
            )
            self._record(run, build_id)
            return run

        run = HarnessRun(
            ok=True, role=role, runtime=runtime, text=text, cost_usd=cost,
            input_tokens=prompt_tokens, output_tokens=output_tokens,
            duration_s=duration,
        )
        self._record(run, build_id)
        return run

    def _record(self, run: HarnessRun, build_id: str) -> None:
        self.capacity.record_usage(
            run.runtime,
            cost_usd=run.cost_usd,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            ok=run.ok,
            failure_kind=run.failure_kind or None,
            build_id=build_id or None,
        )
        if run.rate_limited:
            self.capacity.note_rate_limited(run.runtime, detail=run.detail)

    # -- git ---------------------------------------------------------------

    async def _git(
        self, *args: str, cwd: str | Path, timeout: float = GIT_TIMEOUT_S
    ) -> tuple[int, str, str]:
        return await self._run(["git", *args], cwd=cwd, timeout=timeout)

    async def prepare_workspace(
        self, *, build_id: str, repo_url: str, source_branch: str
    ) -> tuple[Path, str]:
        """Clone the target into a disposable directory and return (path, base).

        A local path is cloned rather than used in place. The projects mount is
        read-only by design, and a factory that edits the operator's own
        checkout is a different and much worse product.
        """
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        path = self.workspace_root / build_id
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

        source = str(repo_url)
        local = Path(source)
        if local.exists():
            source = str(local.resolve())

        clone = ["clone"]
        if source_branch:
            clone += ["--branch", source_branch]
        clone += [source, str(path)]

        code, _out, err = await self._run(
            ["git", *clone], cwd=self.workspace_root, timeout=CLONE_TIMEOUT_S
        )
        if code != 0:
            raise ScopedBuildError(f"clone failed: {err.strip()[:400]}")

        code, out, err = await self._git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)
        if code != 0:
            raise ScopedBuildError(f"could not resolve base branch: {err.strip()[:400]}")
        return path, out.strip() or source_branch or "main"

    async def create_branch(self, path: Path, *, goal: str, build_id: str) -> str:
        branch = f"scoped/{_slug(goal)}-{build_id.rsplit('-', 1)[-1]}"
        code, _out, err = await self._git("checkout", "-b", branch, cwd=path)
        if code != 0:
            raise ScopedBuildError(f"could not create branch: {err.strip()[:400]}")
        return branch

    async def changed_files(self, path: Path, *, base_branch: str) -> list[str]:
        """What git says changed, committed or not.

        Both halves matter. Uncommitted work is invisible to a diff against the
        base, and committed work is invisible to `status`; checking only one is
        how a coder that did commit was recorded as having changed nothing.
        """
        changed: set[str] = set()

        code, out, _err = await self._git("status", "--porcelain", cwd=path)
        if code == 0:
            for line in out.splitlines():
                name = line[3:].strip()
                if " -> " in name:
                    name = name.split(" -> ", 1)[1]
                if name and not name.startswith(IGNORED_PREFIXES):
                    changed.add(name)

        code, out, _err = await self._git(
            "diff", "--name-only", f"{base_branch}...HEAD", cwd=path
        )
        if code == 0:
            changed.update(
                name.strip()
                for name in out.splitlines()
                if name.strip() and not name.strip().startswith(IGNORED_PREFIXES)
            )
        return sorted(changed)

    async def commit_if_needed(self, path: Path, *, goal: str) -> str:
        """Commit whatever the harness left uncommitted, then return the SHA.

        Committing here rather than asking the harness to do it removes an
        entire failure mode: work that was done, was correct, and was thrown
        away because the agent forgot the last step.
        """
        code, out, _err = await self._git("status", "--porcelain", cwd=path)
        if code == 0 and out.strip():
            await self._git("add", "-A", cwd=path)
            await self._git(
                "commit", "--no-verify", "-m", _commit_message(goal), cwd=path
            )
        code, out, _err = await self._git("rev-parse", "HEAD", cwd=path)
        return out.strip() if code == 0 else ""

    async def diff_against(self, path: Path, *, base_branch: str) -> str:
        code, out, _err = await self._git(
            "diff", f"{base_branch}...HEAD", cwd=path, timeout=GIT_TIMEOUT_S
        )
        return out if code == 0 else ""

    async def open_pull_request(
        self, path: Path, *, branch: str, base_branch: str, goal: str
    ) -> tuple[str, str]:
        """Push the branch and open a draft PR. Returns (url, detail)."""
        if shutil.which("gh") is None:
            return "", "gh is not installed; branch left unpushed"

        code, _out, err = await self._git(
            "push", "--set-upstream", "origin", branch, cwd=path, timeout=CLONE_TIMEOUT_S
        )
        if code != 0:
            return "", f"push failed: {err.strip()[:300]}"

        code, out, err = await self._run(
            [
                "gh", "pr", "create",
                "--draft",
                "--base", base_branch,
                "--head", branch,
                "--title", _commit_message(goal),
                "--body", _pr_body(goal),
            ],
            cwd=path,
            timeout=GIT_TIMEOUT_S,
        )
        if code != 0:
            return "", f"gh pr create failed: {(err or out).strip()[:300]}"
        url = next(
            (line.strip() for line in out.splitlines() if line.strip().startswith("http")),
            "",
        )
        return url, "draft pull request opened"

    # -- roles -------------------------------------------------------------

    def choose_roles(self) -> tuple[Runtime, Runtime | None, str]:
        """Pick an implementer and, if one is free, an independent reviewer.

        Independence is by vendor, not by call count. Reviewing Claude's work
        with Claude is one opinion sampled twice, so when only one subscription
        has headroom the review is reported as not independent rather than
        quietly presented as one.
        """
        available = [
            runtime
            for runtime in (Runtime.CLAUDE_CODE, Runtime.CODEX)
            if self.capacity.headroom(runtime).available
        ]
        if not available:
            raise ScopedBuildError(
                "; ".join(
                    f"{runtime.value}: {self.capacity.headroom(runtime).reason}"
                    for runtime in (Runtime.CLAUDE_CODE, Runtime.CODEX)
                )
                or "no subscription harness is available"
            )
        implementer = available[0]
        if len(available) > 1:
            return implementer, available[1], "independent review"
        return implementer, implementer, "review is NOT independent of implementation"

    # -- the run -----------------------------------------------------------

    async def run(
        self,
        *,
        goal: str,
        repo_url: str,
        build_id: str = "",
        source_branch: str = "",
        create_pull_request: bool = True,
        note: Callable[[str], None] | None = None,
    ) -> ScopedOutcome:
        """Implement one scoped change and return everything about it."""
        build_id = build_id or f"scoped-{uuid.uuid4().hex[:10]}"
        result = ScopedOutcome()

        def say(message: str) -> None:
            if note is not None:
                note(message)

        try:
            implementer, reviewer, independence = self.choose_roles()
        except ScopedBuildError as exc:
            result.outcome = "blocked"
            result.detail = str(exc)
            self.ledger.record_event(
                "scoped_blocked", {"reason": result.detail}, build_id=build_id
            )
            return result

        self.ledger.record_event(
            "scoped_started",
            {
                "goal": goal,
                "repo_url": repo_url,
                "implementer": implementer.value,
                "reviewer": reviewer.value if reviewer else "",
                "independence": independence,
            },
            build_id=build_id,
        )

        try:
            path, base_branch = await self.prepare_workspace(
                build_id=build_id, repo_url=repo_url, source_branch=source_branch
            )
            branch = await self.create_branch(path, goal=goal, build_id=build_id)
        except ScopedBuildError as exc:
            result.outcome = "workspace_failed"
            result.detail = str(exc)
            return result

        result.worktree_path = str(path)
        result.base_branch = base_branch
        result.branch = branch
        say(f"workspace ready on {branch} from {base_branch}")

        # 1. implement
        say(f"implementing with {implementer.value}")
        run = await self.invoke(
            implementer,
            role="implementer",
            prompt=implementer_prompt(goal, repo_path=str(path), base_branch=base_branch),
            cwd=path,
            writable=True,
            build_id=build_id,
        )
        result.runs.append(run)
        if not run.ok:
            result.outcome = (
                "capacity_exhausted" if run.rate_limited else "implementer_failed"
            )
            result.detail = run.detail
            return self._finish(result, build_id)

        # 2. change gate, before any review is spent
        result.files_changed = await self.changed_files(path, base_branch=base_branch)
        if not result.files_changed:
            result.outcome = "no_changes"
            result.detail = (
                "the harness reported completion but git sees no change; "
                "rejected before review"
            )
            self.ledger.record_event(
                "scoped_change_gate_rejected", {"reason": result.detail},
                build_id=build_id,
            )
            return self._finish(result, build_id)

        result.commit = await self.commit_if_needed(path, goal=goal)
        say(f"{len(result.files_changed)} file(s) changed")

        # 3. the repository's own tests, and at most one repair
        tests = await asyncio.to_thread(scan_tests, path)
        for _attempt in range(self.max_repairs):
            if tests.status != "failed":
                break
            say("tests failed; one repair attempt")
            repair = await self.invoke(
                implementer,
                role="repair",
                prompt=repair_prompt(goal, failure=_scan_text(tests)),
                cwd=path,
                writable=True,
                build_id=build_id,
            )
            result.runs.append(repair)
            if not repair.ok:
                break
            result.files_changed = await self.changed_files(
                path, base_branch=base_branch
            )
            result.commit = await self.commit_if_needed(path, goal=goal)
            tests = await asyncio.to_thread(scan_tests, path)

        if tests.status == "failed":
            result.outcome = "tests_failed"
            result.detail = _scan_text(tests)[:600]
            result.scanners = summarize([tests])
            return self._finish(result, build_id)

        # 4. independent review of the diff
        diff = await self.diff_against(path, base_branch=base_branch)
        say(f"reviewing with {reviewer.value} ({independence})")
        review = await self.invoke(
            reviewer,
            role="reviewer",
            prompt=reviewer_prompt(goal, diff=diff, files=result.files_changed),
            cwd=path,
            writable=False,
            build_id=build_id,
        )
        result.runs.append(review)
        if not review.ok:
            # A reviewer that could not answer is a rejection. Treating it as a
            # skip is how a review becomes decoration.
            result.review_approved = False
            result.review_summary = f"review could not run: {review.detail[:200]}"
            result.outcome = (
                "capacity_exhausted" if review.rate_limited else "review_unavailable"
            )
            result.detail = result.review_summary
            return self._finish(result, build_id)

        approved, reason = parse_verdict(review.text)
        result.review_approved = bool(approved)
        result.review_summary = reason
        if not approved:
            result.outcome = "review_rejected"
            result.detail = reason
            return self._finish(result, build_id)

        # 5. deterministic scanners. Models advise; these decide.
        outcomes: list[ScanOutcome] = [tests]
        outcomes.extend(
            await asyncio.to_thread(_non_test_scanners, path, base_branch)
        )
        result.scanners = summarize(outcomes)
        if not result.scanners.get("passed"):
            result.outcome = "scanners_failed"
            result.detail = "; ".join(
                result.scanners.get("failed", []) + result.scanners.get("unavailable", [])
            )
            return self._finish(result, build_id)

        # 6. deliver
        if create_pull_request:
            url, detail = await self.open_pull_request(
                path, branch=branch, base_branch=base_branch, goal=goal
            )
            result.pr_url = url
            if not url:
                say(detail)

        result.success = True
        result.outcome = "complete"
        result.detail = (
            f"{len(result.files_changed)} file(s) on {branch}; {independence}"
        )
        return self._finish(result, build_id)

    def _finish(self, result: ScopedOutcome, build_id: str) -> ScopedOutcome:
        self.ledger.record_event(
            "scoped_finished",
            {
                "outcome": result.outcome,
                "success": result.success,
                "harness_calls": result.harness_calls,
                "cost_usd": result.cost_usd,
                "branch": result.branch,
                "pr_url": result.pr_url,
                "detail": result.detail[:500],
            },
            build_id=build_id,
        )
        return result


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _non_test_scanners(path: Path, base_branch: str) -> list[ScanOutcome]:
    """Everything except the suite, which has already been run once."""
    from .evidence.scanners import scan_antislop, scan_dependencies, scan_secrets

    return [
        scan_secrets(path),
        scan_antislop(path, base_branch),
        scan_dependencies(path),
    ]


def _scan_text(outcome: ScanOutcome) -> str:
    return "\n".join([outcome.detail, *outcome.findings]).strip()


def _slug(goal: str, *, limit: int = 40) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")
    return (words[:limit].rstrip("-")) or "change"


def _commit_message(goal: str) -> str:
    first = goal.strip().splitlines()[0].strip() if goal.strip() else "Scoped change"
    return first[:72]


def _pr_body(goal: str) -> str:
    return "\n".join(
        [
            "## Requested",
            "",
            goal.strip(),
            "",
            "## How this was produced",
            "",
            "A ctswarm scoped build: one implementer, the repository's own test",
            "suite, one independent reviewer, and the deterministic scanners.",
            "No planning tier and no issue decomposition.",
            "",
            "Opened as a draft. Review before merging.",
        ]
    )
