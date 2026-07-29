"""OpenRouter backend: overflow capacity with real quota accounting.

Two things distinguish this from a plain OpenAI-compatible endpoint, and both
matter to routing decisions rather than to request mechanics:

1. **Pricing is fetched, never hardcoded.** OpenRouter publishes per-model
   per-token prices on its models endpoint. Baking numbers into source guarantees
   they go stale and the budget cap silently stops meaning anything.
2. **Quota is observable.** Free-tier limits are tight enough that free models
   are opportunistic capacity, not the foundation of a 24/7 factory. The router
   needs to reserve remaining quota for review and unblock work instead of
   spending it on low-value parallel chatter.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from .openai_compat import OpenAICompatBackend, connect_probe_timeout

# How long a fetched price table stays fresh. Prices change rarely; a stale-price
# window of an hour is a fair trade against hammering the endpoint.
PRICE_TTL_S = 3600.0


class OpenRouterBackend(OpenAICompatBackend):
    """OpenRouter, used as overflow and escalation capacity."""

    def __init__(
        self,
        *,
        api_key: str,
        referer: str = "https://github.com/correlltechnologies/ctswarm",
        title: str = "ctswarm",
        **kwargs,
    ) -> None:
        super().__init__(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            metered=True,
            extra_headers={"HTTP-Referer": referer, "X-Title": title},
            **kwargs,
        )
        # model_ref -> (prompt_usd_per_token, completion_usd_per_token)
        self._prices: dict[str, tuple[float, float]] = {}
        self._prices_fetched_at = 0.0
        self._free_models: set[str] = set()

    async def refresh_prices(self) -> None:
        """Fetch the live model and price table. Silent no-op on failure.

        Failing closed here would be wrong: an unreachable pricing endpoint
        should not stop the factory. It only means cost estimates read zero for
        this window, which the budget guard treats as unknown rather than free.
        """
        if time.time() - self._prices_fetched_at < PRICE_TTL_S:
            return
        try:
            response = await self._client.get("/models", timeout=connect_probe_timeout())
            response.raise_for_status()
            entries = response.json().get("data") or []
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError, OSError):
            return

        prices: dict[str, tuple[float, float]] = {}
        free: set[str] = set()
        for entry in entries:
            model_id = entry.get("id")
            pricing = entry.get("pricing") or {}
            if not model_id:
                continue
            try:
                prompt_price = float(pricing.get("prompt") or 0.0)
                completion_price = float(pricing.get("completion") or 0.0)
            except (TypeError, ValueError):
                continue
            prices[model_id] = (prompt_price, completion_price)
            if prompt_price == 0.0 and completion_price == 0.0:
                free.add(model_id)

        self._prices = prices
        self._free_models = free
        self._prices_fetched_at = time.time()

    def cost_for(self, model_ref: str, prompt_tokens: int, output_tokens: int) -> float:
        prompt_price, completion_price = self._prices.get(model_ref, (0.0, 0.0))
        return prompt_tokens * prompt_price + output_tokens * completion_price

    def is_free(self, model_ref: str) -> bool:
        """Whether the model carries no per-token charge.

        Free models are treated as a distinct capacity class: usable, but subject
        to the tightest daily limits, so they are never the sole source of
        capacity for a long-running build.
        """
        return model_ref in self._free_models or model_ref.endswith(":free")

    async def quota(self) -> dict | None:
        """Remaining credit and rate-limit state for the configured key.

        Returned shape is normalized for the ledger: ``remaining`` is credits
        left when the key is capped, or None for an uncapped key.
        """
        try:
            response = await self._client.get("/key", timeout=connect_probe_timeout())
            response.raise_for_status()
            data = response.json().get("data") or {}
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError, OSError):
            return None

        limit = data.get("limit")
        usage = data.get("usage")
        remaining = None
        if limit is not None and usage is not None:
            try:
                remaining = float(limit) - float(usage)
            except (TypeError, ValueError):
                remaining = None

        return {
            "remaining": remaining,
            "limit_value": float(limit) if limit is not None else None,
            "is_free_tier": bool(data.get("is_free_tier")),
            "raw": data,
        }

    async def health(self) -> bool:
        """Auth-aware health check.

        A plain model listing succeeds even with a dead key, so it cannot detect
        the failure mode that actually matters. The key endpoint does.
        """
        try:
            response = await self._client.get("/key", timeout=connect_probe_timeout())
            return response.status_code == 200
        except (httpx.HTTPError, asyncio.TimeoutError, OSError):
            return False
