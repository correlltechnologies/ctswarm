"""Safe local-project and inherited-MCP discovery for Mission Control.

The dashboard never accepts an arbitrary filesystem path. Projects are addressed
by opaque ids derived from paths beneath one configured root, then resolved and
validated again on every request. Git commands are fixed argument lists; no
user-provided value is interpreted by a shell.

MCP discovery here is an *import source*, not the configuration itself. It reads
the host's Claude and Codex files and returns metadata only: commands,
arguments, URLs, environment variables, and credentials never leave them. What a
build actually gets is decided by ``ctswarm.mcp_registry``, which the operator
controls; discovery exists so that what is already on the box can be adopted
into it rather than retyped.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import tomllib

IGNORED_DIRECTORIES = frozenset(
    {
        ".cache",
        ".git",
        ".idea",
        ".next",
        ".venv",
        ".worktrees",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "target",
        "vendor",
    }
)
SCM_PROVIDERS = frozenset({"github", "bitbucket", "gitlab", "azure_devops", "other", "local"})


class ProjectWorkspaceError(ValueError):
    """Raised when a project or repository input is outside the safe workspace."""


def projects_root() -> Path:
    return (
        Path(os.environ.get("CTSWARM_PROJECTS_ROOT", Path.home() / "Desktop" / "Projects"))
        .expanduser()
        .resolve()
    )


def _git(path: Path, *args: str, timeout: float = 8.0) -> str:
    try:
        result = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _project_id(relative_path: str) -> str:
    encoded = base64.urlsafe_b64encode(relative_path.encode()).decode()
    return encoded.rstrip("=")


def _decode_project_id(project_id: str) -> str:
    if not project_id or len(project_id) > 1024:
        raise ProjectWorkspaceError("invalid project id")
    try:
        padding = "=" * (-len(project_id) % 4)
        return base64.urlsafe_b64decode(project_id + padding).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProjectWorkspaceError("invalid project id") from exc


def resolve_project(project_id: str) -> Path:
    root = projects_root()
    candidate = (root / _decode_project_id(project_id)).resolve()
    if not candidate.is_relative_to(root):
        raise ProjectWorkspaceError("project is outside the configured projects root")
    if not candidate.is_dir() or not (candidate / ".git").exists():
        raise ProjectWorkspaceError("project does not exist or is not a Git repository")
    return candidate


def sanitize_remote(remote: str) -> str:
    """Remove URL credentials/query data before a remote reaches telemetry."""
    value = remote.strip()
    if not value or "://" not in value:
        return value
    parts = urlsplit(value)
    hostname = parts.hostname or ""
    if parts.port:
        hostname = f"{hostname}:{parts.port}"
    return urlunsplit((parts.scheme, hostname, parts.path, "", ""))


def validate_remote(remote: str) -> str:
    value = remote.strip()
    if not value:
        raise ProjectWorkspaceError("repository URL is required")
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise ProjectWorkspaceError("repository URL contains invalid characters")
    if "://" in value:
        parts = urlsplit(value)
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ProjectWorkspaceError(
                "repository URLs must not contain credentials, query strings, or fragments"
            )
        if parts.scheme not in {"https", "ssh", "git", "file"}:
            raise ProjectWorkspaceError("unsupported repository URL scheme")
    elif not (
        value.startswith("git@") or value.startswith("ssh@") or Path(value).is_absolute()
    ):
        raise ProjectWorkspaceError("use an HTTPS, SSH, Git, file, or absolute path")
    return value


def detect_scm(remote: str) -> str:
    value = remote.lower()
    if "github.com" in value:
        return "github"
    if "bitbucket.org" in value:
        return "bitbucket"
    if "gitlab.com" in value or "gitlab." in value:
        return "gitlab"
    if "dev.azure.com" in value or "visualstudio.com" in value:
        return "azure_devops"
    if not remote or Path(remote).is_absolute() or remote.startswith("file://"):
        return "local"
    return "other"


def _status(path: Path) -> dict[str, Any]:
    lines = _git(path, "status", "--porcelain=v1", "--untracked-files=normal").splitlines()
    staged = sum(1 for line in lines if line and line[:1] not in {" ", "?"})
    modified = sum(1 for line in lines if len(line) > 1 and line[1] not in {" ", "?"})
    untracked = sum(1 for line in lines if line.startswith("??"))
    return {
        "dirty": bool(lines),
        "staged": staged,
        "modified": modified,
        "untracked": untracked,
    }


def _ahead_behind(path: Path) -> tuple[int, int]:
    raw = _git(path, "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
    try:
        ahead, behind = (int(value) for value in raw.split())
    except (TypeError, ValueError):
        return 0, 0
    return ahead, behind


def _worktrees(path: Path) -> list[dict[str, Any]]:
    raw = _git(path, "worktree", "list", "--porcelain")
    if not raw:
        return []
    worktrees: list[dict[str, Any]] = []
    for block in raw.split("\n\n"):
        item: dict[str, Any] = {
            "path": "",
            "head": "",
            "branch": "",
            "bare": False,
            "detached": False,
            "locked": False,
            "prunable": False,
        }
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                item["path"] = value
            elif key == "HEAD":
                item["head"] = value[:12]
            elif key == "branch":
                item["branch"] = value.removeprefix("refs/heads/")
            elif key in {"bare", "detached", "locked", "prunable"}:
                item[key] = True
        if item["path"]:
            item["current"] = Path(item["path"]).resolve() == path.resolve()
            worktrees.append(item)
    return worktrees


def _summary(path: Path, root: Path) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    remote = sanitize_remote(_git(path, "remote", "get-url", "origin"))
    branch = _git(path, "branch", "--show-current") or "Detached HEAD"
    status = _status(path)
    ahead, behind = _ahead_behind(path)
    worktrees = _worktrees(path)
    return {
        "id": _project_id(relative),
        "name": path.name,
        "relative_path": relative,
        "path": str(path),
        "remote_url": remote,
        "scm_provider": detect_scm(remote),
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "worktree_count": len(worktrees),
        **status,
    }


def discover_projects(*, max_depth: int | None = None) -> list[dict[str, Any]]:
    root = projects_root()
    if not root.is_dir():
        return []
    depth_limit = (
        max_depth
        if max_depth is not None
        else int(os.environ.get("CTSWARM_PROJECT_SCAN_DEPTH", "3"))
    )
    found: list[dict[str, Any]] = []
    for current, directories, _files in os.walk(root):
        path = Path(current)
        depth = len(path.relative_to(root).parts)
        directories[:] = [
            name
            for name in directories
            if name not in IGNORED_DIRECTORIES and not name.startswith(".")
        ]
        # Linked worktrees have a .git *file* and are shown beneath their main
        # repository instead of being duplicated in the project picker.
        if (path / ".git").is_dir():
            found.append(_summary(path, root))
            directories[:] = []
            continue
        if depth >= depth_limit:
            directories[:] = []
    return sorted(found, key=lambda item: (item["name"].lower(), item["relative_path"]))


def project_details(project_id: str, *, history_limit: int = 30) -> dict[str, Any]:
    root = projects_root()
    path = resolve_project(project_id)
    summary = _summary(path, root)
    raw_history = _git(
        path,
        "log",
        f"-{max(1, min(history_limit, 100))}",
        "--date=iso-strict",
        "--pretty=format:%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1f%D",
    )
    history = []
    for line in raw_history.splitlines():
        parts = line.split("\x1f", 5)
        if len(parts) != 6:
            continue
        commit, short, author, committed_at, subject, refs = parts
        history.append(
            {
                "commit": commit,
                "short": short,
                "author": author,
                "committed_at": committed_at,
                "subject": subject,
                "refs": refs,
            }
        )
    summary["history"] = history
    summary["worktrees"] = _worktrees(path)
    summary["default_branch"] = _git(
        path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"
    ).removeprefix("origin/")
    return summary


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _config_path(env_name: str, mounted: str, fallback: Path) -> Path:
    configured = os.environ.get(env_name)
    if configured:
        return Path(configured).expanduser()
    mounted_path = Path(mounted)
    return mounted_path if mounted_path.is_file() else fallback


def claude_config_path() -> Path:
    """Where this process should look for the host's Claude configuration.

    Public because the MCP registry imports from the same files. Two modules
    each guessing the location is how you get a registry that finds nothing in
    a container while discovery on the same box finds everything.
    """
    return _config_path(
        "CTSWARM_CLAUDE_CONFIG", "/host-config/claude.json", Path.home() / ".claude.json"
    )


def codex_config_path() -> Path:
    return _config_path(
        "CTSWARM_CODEX_CONFIG",
        "/host-config/codex.toml",
        Path.home() / ".codex" / "config.toml",
    )


def _mcp_entry(
    name: str, config: dict[str, Any], *, source: str, runtime: str
) -> dict[str, Any]:
    command = str(config.get("command") or "")
    url = str(config.get("url") or "")
    transport = "remote" if url else "local process" if command else "unknown"
    available = bool(url or command)
    note = f"Inherited by {runtime} when that runtime handles the work."
    if command and Path(command).is_absolute() and not Path(command).exists():
        available = False
        note = "Configured command points to a host path that is not currently available."
    elif command and not Path(command).is_absolute() and shutil.which(command) is None:
        # The scheduler image and agent image do not share a PATH, so this is a
        # warning rather than a rejection. The worker performs the real check.
        note = f"Configured for {runtime}; command availability is checked in the worker."
    return {
        "id": f"{source}:{name}",
        "name": name,
        "source": source,
        "runtime": runtime,
        "transport": transport,
        "available": available,
        "note": note,
    }


def discover_mcp_servers(project_path: Path | None = None) -> list[dict[str, Any]]:
    inventory_path = os.environ.get("CTSWARM_MCP_INVENTORY")
    if inventory_path:
        inventory = _read_json(Path(inventory_path))
        entries = list(inventory.get("global") or [])
        if project_path is not None:
            entries.extend((inventory.get("projects") or {}).get(str(project_path), []))
        safe_entries = [entry for entry in entries if isinstance(entry, dict)]
        unique = {str(entry.get("id")): entry for entry in safe_entries if entry.get("id")}
        return sorted(
            unique.values(),
            key=lambda item: (str(item.get("name", "")).lower(), str(item.get("source", ""))),
        )

    claude_path = claude_config_path()
    codex_path = codex_config_path()
    claude = _read_json(claude_path)
    entries: list[dict[str, Any]] = []
    for name, config in (claude.get("mcpServers") or {}).items():
        if isinstance(config, dict):
            entries.append(
                _mcp_entry(str(name), config, source="claude", runtime="Claude Code")
            )

    if project_path is not None:
        project_config = (claude.get("projects") or {}).get(str(project_path), {})
        if isinstance(project_config, dict):
            for name, config in (project_config.get("mcpServers") or {}).items():
                if isinstance(config, dict):
                    item = _mcp_entry(
                        str(name), config, source="claude", runtime="Claude Code"
                    )
                    item["project_specific"] = True
                    entries.append(item)

    codex = _read_toml(codex_path)
    for name, config in (codex.get("mcp_servers") or {}).items():
        if isinstance(config, dict):
            entries.append(_mcp_entry(str(name), config, source="codex", runtime="Codex"))

    unique = {entry["id"]: entry for entry in entries}
    return sorted(unique.values(), key=lambda item: (item["name"].lower(), item["source"]))


def write_mcp_inventory(
    destination: Path,
    *,
    claude_path: Path | None = None,
    codex_path: Path | None = None,
) -> None:
    """Write a browser-safe MCP inventory without copying operational config."""
    claude = _read_json(claude_path or Path.home() / ".claude.json")
    codex = _read_toml(codex_path or Path.home() / ".codex" / "config.toml")
    global_entries: list[dict[str, Any]] = []
    project_entries: dict[str, list[dict[str, Any]]] = {}

    for name, config in (claude.get("mcpServers") or {}).items():
        if isinstance(config, dict):
            global_entries.append(
                _mcp_entry(str(name), config, source="claude", runtime="Claude Code")
            )
    for project, project_config in (claude.get("projects") or {}).items():
        if not isinstance(project_config, dict):
            continue
        items = []
        for name, config in (project_config.get("mcpServers") or {}).items():
            if isinstance(config, dict):
                item = _mcp_entry(str(name), config, source="claude", runtime="Claude Code")
                item["project_specific"] = True
                items.append(item)
        if items:
            project_entries[str(project)] = items
    for name, config in (codex.get("mcp_servers") or {}).items():
        if isinstance(config, dict):
            global_entries.append(
                _mcp_entry(str(name), config, source="codex", runtime="Codex")
            )

    payload = {"global": global_entries, "projects": project_entries}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.chmod(0o644)
    temporary.replace(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a secret-free MCP inventory.")
    parser.add_argument("--write-inventory", type=Path, required=True)
    arguments = parser.parse_args()
    write_mcp_inventory(arguments.write_inventory)
