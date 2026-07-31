"""Project browser and inherited MCP inventory regression tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi import Response

import ctswarm.scheduler as scheduler_module
from ctswarm.project_workspace import (
    ProjectWorkspaceError,
    detect_scm,
    discover_mcp_servers,
    discover_projects,
    project_details,
    resolve_project,
    sanitize_remote,
    selected_mcp_context,
    validate_remote,
    write_mcp_inventory,
)
from ctswarm.scheduler import SwarmLaunchRequest


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(root: Path, name: str = "sample") -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test Operator")
    _git(repo, "config", "user.email", "operator@example.invalid")
    (repo / "README.md").write_text("# Sample\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "Initial project")
    _git(repo, "remote", "add", "origin", "git@github.com:example/sample.git")
    return repo


def test_project_discovery_history_and_worktrees(monkeypatch, tmp_path) -> None:
    root = tmp_path / "projects"
    repo = _repository(root)
    worktree = root / "sample-feature"
    _git(repo, "worktree", "add", "-b", "feature/readable-ui", str(worktree))
    (repo / "local.txt").write_text("untracked\n")
    monkeypatch.setenv("CTSWARM_PROJECTS_ROOT", str(root))

    projects = discover_projects()
    assert [project["name"] for project in projects] == ["sample"]
    assert projects[0]["scm_provider"] == "github"
    assert projects[0]["dirty"] is True
    assert projects[0]["worktree_count"] == 2

    detail = project_details(projects[0]["id"])
    assert detail["history"][0]["subject"] == "Initial project"
    assert {item["branch"] for item in detail["worktrees"]} == {
        "main",
        "feature/readable-ui",
    }
    assert resolve_project(projects[0]["id"]) == repo


def test_project_id_cannot_escape_root(monkeypatch, tmp_path) -> None:
    root = tmp_path / "projects"
    _repository(root)
    monkeypatch.setenv("CTSWARM_PROJECTS_ROOT", str(root))
    escaped = "Li4="  # base64url for ".."

    with pytest.raises(ProjectWorkspaceError, match="outside"):
        resolve_project(escaped)


def test_remote_validation_removes_sensitive_url_parts() -> None:
    assert sanitize_remote("https://user:secret@github.com/org/repo.git?token=x") == (
        "https://github.com/org/repo.git"
    )
    assert detect_scm("git@bitbucket.org:team/repo.git") == "bitbucket"
    assert validate_remote("git@gitlab.com:team/repo.git") == (
        "git@gitlab.com:team/repo.git"
    )
    with pytest.raises(ProjectWorkspaceError, match="must not contain credentials"):
        validate_remote("https://user:secret@github.com/org/repo.git")


def test_mcp_inventory_exposes_metadata_without_configuration_secrets(
    monkeypatch, tmp_path
) -> None:
    claude = tmp_path / "claude.json"
    claude.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "vercel": {
                        "url": "https://mcp.example.invalid?token=secret-value"
                    },
                    "local-docs": {
                        "command": "npx",
                        "args": ["private-package", "--token", "secret-value"],
                    },
                }
            }
        )
    )
    codex = tmp_path / "codex.toml"
    codex.write_text(
        '[mcp_servers.review]\ncommand = "uvx"\nargs = ["secret-package"]\n'
    )
    monkeypatch.setenv("CTSWARM_CLAUDE_CONFIG", str(claude))
    monkeypatch.setenv("CTSWARM_CODEX_CONFIG", str(codex))

    inventory = discover_mcp_servers()
    serialized = json.dumps(inventory)
    assert {item["id"] for item in inventory} == {
        "claude:vercel",
        "claude:local-docs",
        "codex:review",
    }
    assert "secret-value" not in serialized
    assert "private-package" not in serialized
    assert "mcp.example.invalid" not in serialized
    context = selected_mcp_context(["claude:vercel", "codex:review"], inventory)
    assert "vercel via Claude Code" in context
    assert "review via Codex" in context


def test_generated_mcp_inventory_is_safe_and_project_aware(
    monkeypatch, tmp_path
) -> None:
    project = tmp_path / "projects" / "sample"
    claude = tmp_path / "claude.json"
    claude.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "global-tool": {
                        "url": "https://mcp.example.invalid?token=secret-value"
                    }
                },
                "projects": {
                    str(project): {
                        "mcpServers": {
                            "repo-tool": {
                                "command": "npx",
                                "args": ["private-package", "secret-value"],
                            }
                        }
                    }
                },
            }
        )
    )
    codex = tmp_path / "codex.toml"
    codex.write_text('[mcp_servers.review]\ncommand = "uvx"\n')
    destination = tmp_path / "mcp-inventory.json"

    write_mcp_inventory(destination, claude_path=claude, codex_path=codex)

    serialized = destination.read_text()
    assert "secret-value" not in serialized
    assert "private-package" not in serialized
    assert "mcp.example.invalid" not in serialized
    monkeypatch.setenv("CTSWARM_MCP_INVENTORY", str(destination))
    inventory = discover_mcp_servers(project)
    assert {item["id"] for item in inventory} == {
        "claude:global-tool",
        "claude:repo-tool",
        "codex:review",
    }


async def test_launch_resolves_project_and_inherits_configured_mcps(
    monkeypatch, tmp_path
) -> None:
    root = tmp_path / "projects"
    _repository(root)
    claude = tmp_path / "claude.json"
    claude.write_text(
        json.dumps({"mcpServers": {"vercel": {"url": "https://example.invalid"}}})
    )
    codex = tmp_path / "codex.toml"
    codex.write_text("")
    monkeypatch.setenv("CTSWARM_PROJECTS_ROOT", str(root))
    monkeypatch.setenv("CTSWARM_CLAUDE_CONFIG", str(claude))
    monkeypatch.setenv("CTSWARM_CODEX_CONFIG", str(codex))
    project = discover_projects()[0]
    captured = {}

    def enqueue(request):
        captured["request"] = request
        return "build-launch-test"

    monkeypatch.setattr(scheduler_module.scheduler, "enqueue", enqueue)
    monkeypatch.setattr(scheduler_module.scheduler, "snapshot", lambda _build_id: None)

    result = await scheduler_module.launch_swarm(
        SwarmLaunchRequest(goal="Build the complete flow", project_id=project["id"]),
        Response(),
    )

    request = captured["request"]
    assert result == {"build_id": "build-launch-test", "state": "queued"}
    assert request.repo_url == "git@github.com:example/sample.git"
    assert request.project_path.endswith("/projects/sample")
    assert request.scm_provider == "github"
    assert request.source_branch == "main"
    assert request.create_pull_request is True
    assert request.mcp_servers == ["claude:vercel"]
