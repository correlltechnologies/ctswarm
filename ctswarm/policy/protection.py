"""Branch protection as code.

The plan's central security claim is that model committees are quality control,
not a security boundary, and that what actually protects the repository must stay
deterministic and enforceable "even when every model makes the same mistake".
This module is where that claim is cashed in.

Everything here is applied through the GitHub API and version-controlled, rather
than clicked into a settings page, so protection is reviewable, diffable, and
restorable. An agent cannot argue with a branch protection rule.

Two deliberate constraints:

- **The factory's token must never carry the scopes needed to change these
  rules.** `apply` is an operator action run by a human, not something the build
  loop can invoke. A system that can rewrite its own constraints has none.
- **`verify` is read-only and safe to run constantly.** Drift detection matters
  more than initial setup: protection that was correct at install and quietly
  disabled last month is worse than no protection, because it is still trusted.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProtectionPolicy:
    """Required protection for a branch.

    Defaults implement the pilot posture from the plan: the factory opens pull
    requests and a human merges them.
    """

    branch: str = "main"
    required_approvals: int = 1
    require_ci: bool = True
    required_checks: tuple[str, ...] = ()
    dismiss_stale_reviews: bool = True
    require_conversation_resolution: bool = True
    # Force-push and deletion stay off for everyone, including administrators.
    # An agent that can force-push can erase the evidence of what it did.
    allow_force_push: bool = False
    allow_deletions: bool = False
    # Administrators are included on purpose. During the pilot the owner is also
    # the person most likely to be holding the token the factory runs under, so
    # exempting admins would exempt the factory.
    enforce_admins: bool = True
    require_linear_history: bool = False

    def to_api_payload(self) -> dict:
        return {
            "required_status_checks": (
                {"strict": True, "contexts": list(self.required_checks)}
                if self.require_ci
                else None
            ),
            "enforce_admins": self.enforce_admins,
            "required_pull_request_reviews": (
                {
                    "dismiss_stale_reviews": self.dismiss_stale_reviews,
                    "require_code_owner_reviews": False,
                    "required_approving_review_count": self.required_approvals,
                }
                if self.required_approvals > 0
                else None
            ),
            "restrictions": None,
            "allow_force_pushes": self.allow_force_push,
            "allow_deletions": self.allow_deletions,
            "required_conversation_resolution": self.require_conversation_resolution,
            "required_linear_history": self.require_linear_history,
        }


@dataclass
class ProtectionReport:
    """What protection is actually in force, versus what is required."""

    repo: str
    branch: str
    exists: bool = False
    compliant: bool = False
    violations: list[str] = field(default_factory=list)
    current: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "branch": self.branch,
            "protection_exists": self.exists,
            "compliant": self.compliant,
            "violations": self.violations,
            "error": self.error,
        }


def _gh_api(args: list[str], *, method: str = "GET", body: dict | None = None):
    """Call the GitHub API through the `gh` CLI.

    Using `gh` rather than raw HTTP means credentials come from the operator's
    existing login and are never handled by ctswarm, so there is no path by which
    the factory could read or reuse the token that governs it.
    """
    cmd = ["gh", "api", "-X", method, *args]
    if body is not None:
        cmd += ["--input", "-"]
    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(body) if body is not None else None,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout).strip()[:400]
    try:
        return json.loads(proc.stdout or "{}"), ""
    except ValueError:
        return {}, ""


def verify(repo: str, policy: ProtectionPolicy) -> ProtectionReport:
    """Check the live protection against the policy. Read-only."""
    report = ProtectionReport(repo=repo, branch=policy.branch)
    data, error = _gh_api([f"repos/{repo}/branches/{policy.branch}/protection"])

    if error:
        if "Branch not protected" in error or "404" in error:
            report.violations = [
                f"branch '{policy.branch}' has NO protection at all; the factory "
                "could push to it directly"
            ]
            return report
        report.error = error
        report.violations = [f"could not read protection: {error}"]
        return report

    report.exists = True
    report.current = data or {}
    violations: list[str] = []

    reviews = (data or {}).get("required_pull_request_reviews") or {}
    approvals = reviews.get("required_approving_review_count", 0)
    if approvals < policy.required_approvals:
        violations.append(
            f"required approvals is {approvals}, policy requires "
            f"{policy.required_approvals}: autonomous merges would be possible"
        )

    checks = (data or {}).get("required_status_checks") or {}
    if policy.require_ci and not checks:
        violations.append("CI is not required to pass before merge")

    missing_checks = set(policy.required_checks) - set(checks.get("contexts") or [])
    if missing_checks:
        violations.append(f"required checks missing: {sorted(missing_checks)}")

    if (data or {}).get("allow_force_pushes", {}).get("enabled") and not policy.allow_force_push:
        violations.append(
            "force pushes are allowed: an agent could rewrite history and erase "
            "the audit trail"
        )
    if (data or {}).get("allow_deletions", {}).get("enabled") and not policy.allow_deletions:
        violations.append("branch deletion is allowed")
    if policy.enforce_admins and not (data or {}).get("enforce_admins", {}).get("enabled"):
        violations.append(
            "protection is not enforced for admins, so a token with admin rights "
            "bypasses every rule above"
        )
    if policy.require_conversation_resolution and not (data or {}).get(
        "required_conversation_resolution", {}
    ).get("enabled"):
        violations.append("unresolved review conversations do not block merge")

    report.violations = violations
    report.compliant = not violations
    return report


def apply(repo: str, policy: ProtectionPolicy) -> tuple[bool, str]:
    """Apply the policy to the branch.

    An operator action. This deliberately requires admin rights on the
    repository, which is exactly why the factory's own credential must not have
    them: the ability to set protection is the ability to remove it.
    """
    _, error = _gh_api(
        [
            f"repos/{repo}/branches/{policy.branch}/protection",
            "-H",
            "Accept: application/vnd.github+json",
        ],
        method="PUT",
        body=policy.to_api_payload(),
    )
    if error:
        return False, error
    return True, f"protection applied to {repo}@{policy.branch}"


def token_scope_report() -> dict:
    """What the current GitHub credential can do.

    Surfaced because an over-scoped token silently defeats the entire protection
    model. A factory token holding `admin:org` or `workflow` can edit the CI that
    is supposed to gate it, which turns every check downstream into theatre.
    """
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "error": str(exc)}

    output = proc.stdout + proc.stderr
    scopes: list[str] = []
    for line in output.splitlines():
        if "Token scopes:" in line:
            scopes = [
                s.strip().strip("'\"")
                for s in line.split("Token scopes:", 1)[1].split(",")
            ]

    # Scopes that let the holder alter the rules that constrain it.
    dangerous = {
        "admin:org": "can change organization-level protections",
        "workflow": "can rewrite the CI workflows that gate merges",
        "delete_repo": "can delete the repository",
        "admin:repo_hook": "can alter or remove audit webhooks",
    }
    held = {s: why for s, why in dangerous.items() if s in scopes}

    return {
        "available": proc.returncode == 0,
        "scopes": scopes,
        "over_scoped": held,
        "advice": (
            "Prefer a GitHub App installation token scoped to selected "
            "repositories with contents + pull-requests only. The factory must "
            "not hold a scope that lets it modify its own constraints."
        )
        if held
        else "scopes look appropriately narrow for a factory credential",
    }
