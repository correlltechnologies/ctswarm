"""Inference backends and the registry that assembles them for a host."""

from __future__ import annotations

import os

from ..platform_detect import HostProfile, detect_host
from .base import (
    Backend,
    ChatRequest,
    ChatResponse,
    FailureKind,
    classify_http_failure,
    inspect_completion,
)
from .mlx import LMStudioBackend, MLXBackend
from .ollama import OllamaBackend
from .openai_compat import OpenAICompatBackend
from .openrouter import OpenRouterBackend

__all__ = [
    "Backend",
    "ChatRequest",
    "ChatResponse",
    "FailureKind",
    "LMStudioBackend",
    "MLXBackend",
    "OllamaBackend",
    "OpenAICompatBackend",
    "OpenRouterBackend",
    "build_backends",
    "classify_http_failure",
    "inspect_completion",
]


def build_backends(
    host: HostProfile | None = None, env: dict | None = None
) -> dict[str, Backend]:
    """Assemble the backends available on this host.

    Construction is cheap and does not perform I/O, so an unreachable backend is
    still registered. The router discovers liveness through health checks and the
    circuit breaker, which means a backend that comes back up mid-build is picked
    up automatically instead of requiring a restart. Probe 2 depends on exactly
    this behavior.
    """
    host = host or detect_host()
    env = env if env is not None else dict(os.environ)
    backends: dict[str, Backend] = {}

    if host.has_ollama or env.get("CTSWARM_OLLAMA_HOST"):
        try:
            context_ceiling = max(
                0, int(env.get("CTSWARM_OLLAMA_CONTEXT_CEILING", "0"))
            )
        except (TypeError, ValueError):
            context_ceiling = 0
        backends["ollama"] = OllamaBackend(
            host=env.get("CTSWARM_OLLAMA_HOST", "http://localhost:11434"),
            context_ceiling=context_ceiling,
        )

    if host.has_mlx or env.get("CTSWARM_MLX_HOST"):
        backends["mlx"] = MLXBackend(
            host=env.get("CTSWARM_MLX_HOST", "http://localhost:8081")
        )

    if host.has_lmstudio or env.get("CTSWARM_LMSTUDIO_HOST"):
        backends["lmstudio"] = LMStudioBackend(
            host=env.get("CTSWARM_LMSTUDIO_HOST", "http://localhost:1234")
        )

    openrouter_key = env.get("OPENROUTER_API_KEY")
    if openrouter_key:
        backends["openrouter"] = OpenRouterBackend(api_key=openrouter_key)

    # Any additional OpenAI-compatible endpoint, for a second machine on the
    # tailnet or a self-hosted server. Declared as CTSWARM_EXTRA_BACKEND=name|url
    # so adding remote capacity needs no code change.
    extra = env.get("CTSWARM_EXTRA_BACKEND")
    if extra and "|" in extra:
        name, _, url = extra.partition("|")
        name = name.strip()
        if name and url.strip():
            backends[name] = OpenAICompatBackend(
                name=name,
                base_url=url.strip(),
                api_key=env.get("CTSWARM_EXTRA_BACKEND_KEY"),
                metered=False,
            )

    return backends
