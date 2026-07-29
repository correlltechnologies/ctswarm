"""MLX and LM Studio backends for Apple Silicon.

Two distinct servers, one adapter each, because they differ in exactly one
meaningful way: ``mlx_lm.server`` serves a single model per process, while LM
Studio manages a pool and can load on demand.

That difference drives real routing behavior. On a Mac running bare
``mlx_lm.server``, asking for a model the process was not started with is a hard
failure, not a slow load, so the router must not treat MLX models as
interchangeable the way it can with Ollama.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from typing import Optional

import httpx

from .openai_compat import OpenAICompatBackend, connect_probe_timeout


class MLXBackend(OpenAICompatBackend):
    """``mlx_lm.server`` running locally on Apple Silicon.

    Start it with:
        python -m mlx_lm server --model <hf-repo> --port 8081
    """

    def __init__(self, *, host: str = "http://localhost:8081", **kwargs) -> None:
        self.host = host.rstrip("/")
        super().__init__(
            name="mlx",
            base_url=f"{self.host}/v1",
            metered=False,
            **kwargs,
        )
        self._pinned: Optional[str] = None

    async def list_models(self) -> list[str]:
        models = await super().list_models()
        # A single-model server still advertises its one model through /v1/models.
        # Remember it so the router can refuse requests for anything else rather
        # than issuing a call that is certain to 404.
        if len(models) == 1:
            self._pinned = models[0]
        return models

    def serves(self, model_ref: str) -> bool:
        """Whether this server can serve the reference without a restart."""
        if self._pinned is None:
            return True
        return model_ref == self._pinned

    @staticmethod
    def available_locally() -> bool:
        """True when mlx-lm is installed on this interpreter."""
        try:
            import importlib.util

            return importlib.util.find_spec("mlx_lm") is not None
        except (ImportError, ValueError):
            return False


class LMStudioBackend(OpenAICompatBackend):
    """LM Studio's local server.

    LM Studio defaults to port 1234 and can hold several models loaded at once,
    so unlike bare MLX it behaves like Ollama from the router's perspective.
    """

    def __init__(self, *, host: str = "http://localhost:1234", **kwargs) -> None:
        self.host = host.rstrip("/")
        super().__init__(
            name="lmstudio",
            base_url=f"{self.host}/v1",
            metered=False,
            **kwargs,
        )

    async def loaded_models(self) -> set[str]:
        """Currently loaded models, via the LM Studio CLI when present.

        There is no stable HTTP endpoint for this, so we shell out to `lms` and
        degrade to an empty set when it is unavailable. An empty set costs the
        router only its warm-model preference.
        """
        if not shutil.which("lms"):
            return set()
        try:
            proc = await asyncio.create_subprocess_exec(
                "lms",
                "ps",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except (OSError, asyncio.TimeoutError):
            return set()
        return {
            line.split()[0]
            for line in stdout.decode(errors="replace").splitlines()[1:]
            if line.strip()
        }
