"""Deterministic scanners.

These produce facts, not opinions. The committee treats their results as
authoritative precisely because nothing here asks a model anything: a secret is
a secret whether or not a panel of reviewers agrees, and a failing test is a
failing test.

Design rules:

- **A scanner that cannot run reports ``unavailable``, never ``passed``.** A
  missing tool must not read as a clean bill of health. This is the single most
  important property in the file: silently passing when `npm audit` is absent
  would turn the security gate into decoration.
- **Findings are specific.** "Security issue detected" is unactionable. Every
  finding names a file, a package, or a rule.
- **No network calls except where the tool itself needs them.** Scanners run
  inside the build loop; a slow registry lookup must not stall the DAG.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..committee import ScannerResult
from .antislop import check_repo


@dataclass(frozen=True)
class ScanOutcome:
    """Internal result, richer than the committee's view."""

    name: str
    status: str  # passed | failed | unavailable
    detail: str
    findings: tuple[str, ...] = ()

    def to_committee(self) -> ScannerResult:
        """Convert for committee consumption.

        ``unavailable`` maps to *failed*. A gate that cannot be evaluated has not
        been satisfied, and treating it as passed is how a security review
        quietly becomes a no-op on a machine missing one tool.
        """
        return ScannerResult(
            name=self.name,
            passed=self.status == "passed",
            detail=(
                self.detail
                if self.status != "unavailable"
                else f"COULD NOT RUN: {self.detail}"
            ),
            findings=self.findings,
        )


def _run(
    cmd: list[str], cwd: str | Path, timeout: int = 180
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return -1, "", "tool not installed"
    except subprocess.TimeoutExpired:
        return -2, "", f"timed out after {timeout}s"
    except OSError as exc:
        return -3, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------

# Credential shapes worth blocking on. Deliberately conservative: a false
# positive costs one `ctswarm:allow` comment, a false negative leaks a key.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("anthropic key", re.compile(r"sk-ant-(?:api|oat)\d{2}-[A-Za-z0-9_\-]{20,}")),
    ("openrouter key", re.compile(r"sk-or-v1-[A-Za-z0-9]{32,}")),
    ("openai key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("github token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("slack token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{20,}")),
    ("aws access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("google api key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("stripe key", re.compile(r"[rs]k_live_[0-9A-Za-z]{24,}")),
)

SECRET_SKIP_DIRS = frozenset(
    {"node_modules", ".git", "dist", "build", ".next", "vendor", "__pycache__",
     ".venv", "venv", "coverage"}
)


def scan_secrets(repo: str | Path) -> ScanOutcome:
    """Search the working tree for credential-shaped literals.

    Uses `gitleaks` when present because it also inspects history, and falls back
    to a pattern sweep of the working tree otherwise. The fallback is weaker (it
    cannot see a secret that was committed and later removed) and says so, rather
    than implying equivalent coverage.
    """
    repo = Path(repo)

    if shutil.which("gitleaks"):
        code, out, err = _run(
            ["gitleaks", "detect", "--no-banner", "--redact", "-f", "json", "-r", "-"],
            repo,
        )
        if code in (0, 1):
            findings: list[str] = []
            try:
                for item in json.loads(out or "[]"):
                    findings.append(
                        f"{item.get('File')}:{item.get('StartLine')} "
                        f"{item.get('RuleID')}"
                    )
            except (ValueError, TypeError):
                pass
            if findings:
                return ScanOutcome(
                    "secret-scan", "failed",
                    f"gitleaks found {len(findings)} secret(s) including history",
                    tuple(findings[:20]),
                )
            return ScanOutcome(
                "secret-scan", "passed", "gitleaks found no secrets (history included)"
            )

    findings = []
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(repo)
        if any(part in SECRET_SKIP_DIRS for part in relative.parts):
            continue
        if path.stat().st_size > 2_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line} {label}")

    if findings:
        return ScanOutcome(
            "secret-scan", "failed",
            f"{len(findings)} credential-shaped literal(s) in the working tree",
            tuple(findings[:20]),
        )
    return ScanOutcome(
        "secret-scan", "passed",
        "no secrets in the working tree (pattern sweep; install gitleaks to "
        "also cover git history)",
    )


# ---------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------


def scan_dependencies(repo: str | Path) -> ScanOutcome:
    """Audit declared dependencies for known advisories."""
    repo = Path(repo)

    if (repo / "package.json").exists():
        if not shutil.which("npm"):
            return ScanOutcome(
                "dependency-audit", "unavailable", "npm is not installed"
            )
        code, out, _ = _run(["npm", "audit", "--json"], repo, timeout=300)
        if code == -1:
            return ScanOutcome("dependency-audit", "unavailable", "npm not found")
        try:
            report = json.loads(out or "{}")
        except ValueError:
            return ScanOutcome(
                "dependency-audit", "unavailable", "npm audit returned unparseable JSON"
            )
        meta = (report.get("metadata") or {}).get("vulnerabilities") or {}
        # Only high and critical block. Blocking on every low advisory in a
        # transitive dependency makes the gate impossible to keep green, and a
        # gate people routinely bypass protects nothing.
        blocking = int(meta.get("high", 0)) + int(meta.get("critical", 0))
        findings = [
            f"{name}: {info.get('severity')} via {','.join(info.get('via', [])[:2]) if isinstance(info.get('via'), list) else info.get('via')}"
            for name, info in list((report.get("vulnerabilities") or {}).items())[:15]
            if info.get("severity") in ("high", "critical")
        ]
        if blocking:
            return ScanOutcome(
                "dependency-audit", "failed",
                f"{blocking} high/critical advisories "
                f"({meta.get('low',0)} low, {meta.get('moderate',0)} moderate ignored)",
                tuple(str(f) for f in findings),
            )
        return ScanOutcome(
            "dependency-audit", "passed",
            f"no high/critical advisories ({meta.get('total', 0)} total)",
        )

    if (repo / "pyproject.toml").exists() or (repo / "requirements.txt").exists():
        if not shutil.which("pip-audit"):
            return ScanOutcome(
                "dependency-audit", "unavailable",
                "pip-audit is not installed (pip install pip-audit)",
            )
        code, out, _ = _run(["pip-audit", "-f", "json"], repo, timeout=300)
        try:
            report = json.loads(out or "{}")
        except ValueError:
            return ScanOutcome(
                "dependency-audit", "unavailable", "pip-audit returned unparseable JSON"
            )
        deps = report.get("dependencies") or report or []
        findings = [
            f"{d.get('name')} {d.get('version')}: {len(d.get('vulns') or [])} advisories"
            for d in deps
            if isinstance(d, dict) and d.get("vulns")
        ]
        if findings:
            return ScanOutcome(
                "dependency-audit", "failed",
                f"{len(findings)} vulnerable package(s)", tuple(findings[:15]),
            )
        return ScanOutcome("dependency-audit", "passed", "no known advisories")

    return ScanOutcome(
        "dependency-audit", "unavailable", "no recognised dependency manifest"
    )


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def scan_tests(repo: str | Path, command: list[str] | None = None) -> ScanOutcome:
    """Run the target repository's own test suite.

    The most authoritative scanner there is: it is the project's own definition
    of working, written by people rather than inferred.
    """
    repo = Path(repo)
    if command is None:
        if (repo / "package.json").exists():
            command = ["npm", "test", "--silent"]
        elif (repo / "pyproject.toml").exists():
            command = ["pytest", "-q"]
        else:
            return ScanOutcome("tests", "unavailable", "no recognised test runner")

    code, out, err = _run(command, repo, timeout=900)
    combined = f"{out}\n{err}"
    if code == -1:
        return ScanOutcome("tests", "unavailable", f"{command[0]} is not installed")
    if code == -2:
        return ScanOutcome("tests", "failed", "test suite timed out")
    if code != 0:
        failures = re.findall(r"(?:FAIL|✗|×)\s+(.+)", combined)[:12]
        return ScanOutcome(
            "tests", "failed", f"test suite exited {code}",
            tuple(f.strip()[:140] for f in failures) or ("see build log",),
        )
    return ScanOutcome("tests", "passed", "test suite passed")


# ---------------------------------------------------------------------------
# anti-slop, as a scanner
# ---------------------------------------------------------------------------


def scan_antislop(repo: str | Path, base_ref: str | None = None) -> ScanOutcome:
    report = check_repo(repo, base_ref=base_ref)
    if report.passed:
        return ScanOutcome(
            "anti-slop", "passed",
            f"clean ({len(report.warnings)} non-blocking warnings)",
        )
    return ScanOutcome(
        "anti-slop", "failed",
        f"{len(report.blockers)} blocking violation(s)",
        tuple(
            f"{f.file}:{f.line} {f.check}" for f in report.blockers[:20]
        ),
    )


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def run_all(
    repo: str | Path,
    *,
    base_ref: str | None = None,
    include_tests: bool = True,
) -> list[ScanOutcome]:
    """Run every scanner. Order is cheapest-first so a secret leak is caught
    before spending fifteen minutes on a test suite."""
    outcomes = [
        scan_secrets(repo),
        scan_antislop(repo, base_ref),
        scan_dependencies(repo),
    ]
    if include_tests:
        outcomes.append(scan_tests(repo))
    return outcomes


def summarize(outcomes: list[ScanOutcome]) -> dict:
    failed = [o for o in outcomes if o.status == "failed"]
    unavailable = [o for o in outcomes if o.status == "unavailable"]
    return {
        # Unavailable counts against passing. An unevaluated gate is not a
        # satisfied one.
        "passed": not failed and not unavailable,
        "failed": [o.name for o in failed],
        "unavailable": [o.name for o in unavailable],
        "results": [
            {
                "name": o.name,
                "status": o.status,
                "detail": o.detail,
                "findings": list(o.findings),
            }
            for o in outcomes
        ],
    }
