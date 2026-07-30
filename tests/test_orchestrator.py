"""Regression tests for build submission configuration."""

from __future__ import annotations

from ctswarm.capacity import Runtime
from ctswarm.orchestrator import runtime_model_overrides


def test_open_code_uses_router_virtual_models() -> None:
    models = runtime_model_overrides(Runtime.OPEN_CODE)

    assert models["default"] == "ctswarm/med"
    assert models["pm"] == "ctswarm/high"
    assert models["qa_synthesizer"] == "ctswarm/low"


def test_claude_code_overrides_open_code_container_environment() -> None:
    models = runtime_model_overrides(Runtime.CLAUDE_CODE)

    assert models == {"default": "sonnet", "qa_synthesizer": "haiku"}
    assert all(not model.startswith("ctswarm/") for model in models.values())


def test_codex_chatgpt_auth_overrides_open_code_environment(monkeypatch) -> None:
    monkeypatch.setenv("SWE_CODEX_AUTH_MODE", "chatgpt")
    models = runtime_model_overrides(Runtime.CODEX)

    assert models == {"default": "gpt-5.5"}
    assert all(not model.startswith("ctswarm/") for model in models.values())


def test_codex_api_key_auth_uses_codex_model(monkeypatch) -> None:
    monkeypatch.setenv("SWE_CODEX_AUTH_MODE", "api_key")

    assert runtime_model_overrides(Runtime.CODEX) == {
        "default": "gpt-5.3-codex"
    }


def test_codex_auto_auth_tracks_api_key_presence(monkeypatch) -> None:
    monkeypatch.setenv("SWE_CODEX_AUTH_MODE", "auto")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert runtime_model_overrides(Runtime.CODEX)["default"] == "gpt-5.5"

    monkeypatch.setenv("OPENAI_API_KEY", "present")
    assert (
        runtime_model_overrides(Runtime.CODEX)["default"] == "gpt-5.3-codex"
    )
