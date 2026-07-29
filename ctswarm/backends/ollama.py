"""Ollama backend.

Ollama serves an OpenAI-compatible API at ``/v1`` alongside its native API at
``/api``. Chat goes through the compatible path; the native path is used for the
things OpenAI's shape has no concept of, namely which models are resident in
accelerator memory right now.

That residency information matters more than it looks. Swapping a model into a
12GB card evicts whatever was there, so routing three roles to three different
large models produces constant VRAM thrashing. The router uses ``loaded_models``
to prefer an already-warm model when scores are otherwise close.
"""

from __future__ import annotations

import asyncio

import httpx

from .openai_compat import OpenAICompatBackend, connect_probe_timeout


class OllamaBackend(OpenAICompatBackend):
    """Local Ollama server."""

    def __init__(self, *, host: str = "http://localhost:11434", **kwargs) -> None:
        self.host = host.rstrip("/")
        super().__init__(
            name="ollama",
            base_url=f"{self.host}/v1",
            metered=False,
            **kwargs,
        )
        self._native = httpx.AsyncClient(
            base_url=self.host, timeout=connect_probe_timeout()
        )

    async def health(self) -> bool:
        """Ollama's root path returns a plain-text banner when alive."""
        try:
            response = await self._native.get("/")
            return response.status_code == 200
        except (httpx.HTTPError, asyncio.TimeoutError, OSError):
            return False

    async def list_models(self) -> list[str]:
        """Installed models, via the native tags endpoint.

        Ollama's ``/v1/models`` also works, but the native endpoint additionally
        carries size and family metadata that ``ctswarm doctor`` reports.
        """
        try:
            response = await self._native.get("/api/tags")
            response.raise_for_status()
            return [
                entry["name"]
                for entry in response.json().get("models", [])
                if isinstance(entry, dict) and "name" in entry
            ]
        except (httpx.HTTPError, ValueError, KeyError, asyncio.TimeoutError, OSError):
            return []

    async def loaded_models(self) -> set[str]:
        """Models currently resident in memory.

        Empty set on any failure, which degrades the router to score-only
        ordering rather than breaking routing entirely.
        """
        try:
            response = await self._native.get("/api/ps")
            response.raise_for_status()
            return {
                entry["name"]
                for entry in response.json().get("models", [])
                if isinstance(entry, dict) and "name" in entry
            }
        except (httpx.HTTPError, ValueError, KeyError, asyncio.TimeoutError, OSError):
            return set()

    async def model_details(self, model_ref: str) -> dict:
        """Capability metadata for one model, as ``ollama show`` reports it.

        Used by the bench to record whether a model claims tool support before
        measuring whether it can actually deliver it.
        """
        try:
            response = await self._native.post("/api/show", json={"model": model_ref})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError, OSError):
            return {}
        info = body.get("model_info") or {}
        context = next(
            (v for k, v in info.items() if k.endswith(".context_length")), None
        )
        return {
            "capabilities": body.get("capabilities") or [],
            "context": context,
            "parameters": (body.get("details") or {}).get("parameter_size"),
            "quantization": (body.get("details") or {}).get("quantization_level"),
            "family": (body.get("details") or {}).get("family"),
        }

    async def close(self) -> None:
        await self._native.aclose()
        await super().close()
