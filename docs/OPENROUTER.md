# OpenRouter setup

The integration is built and tested; it needs a key. Creating the account is the
one step that cannot be automated, because it requires accepting terms and
entering payment details as you.

**Five minutes, and about $5 covers a lot of pilot builds.**

## Why it is worth doing

OpenRouter is currently the single highest-leverage addition to ctswarm, for
three separate reasons:

1. **Real failover.** With only Ollama installed, the router's fallback chain
   cannot cross backends, so the whole class of "the inference server died" is
   untestable. Verification probe 2 says so explicitly rather than passing
   vacuously. This machine has already produced that exact failure three times
   (a wedged model runner), so it is not hypothetical.
2. **A third committee family.** Committees require independent model families,
   and locally there are exactly two eligible (`qwen`, `granite`). A third family
   makes majority votes meaningful instead of a coin flip between two.
3. **The empty high tier.** No installed local model qualifies for planning
   roles, so the router currently degrades planning to the med tier and labels it
   as such. OpenRouter fills that gap without the Claude/Codex subscription cost.

## Setup

1. Go to <https://openrouter.ai> and sign in (GitHub or Google works).
2. Add credit at <https://openrouter.ai/credits>. **$5 to $10 is plenty** to
   start; ctswarm treats paid models as overflow, not as the primary path, and
   the budget cap below is enforced.
3. Create a key at <https://openrouter.ai/keys>. Name it `ctswarm`.
4. Set a **spend limit on the key itself** while you are there. Belt and braces:
   ctswarm has its own cap, but a provider-side limit cannot be bypassed by a bug
   in ctswarm.
5. Paste it into `.env`:

```bash
OPENROUTER_API_KEY=sk-or-v1-...
```

6. Restart so the router picks it up:

```bash
./stack.sh up
./.venv/bin/ctswarm doctor      # openrouter should read "yes"
```

## What happens automatically once the key exists

Nothing else needs configuring. Specifically:

- **Prices are fetched, never hardcoded.** The router pulls the live model and
  price table from OpenRouter's own endpoint and caches it for an hour. Hardcoded
  prices go stale and silently make the budget cap meaningless.
- **Free models are treated as opportunistic capacity**, not as the foundation of
  a 24/7 factory. Their daily limits are tight enough that relying on them would
  stall long builds, so the router reserves remaining quota for review and
  unblock work rather than spending it on parallel chatter.
- **Local still wins by default.** `CTSWARM_PREFER_LOCAL=1` means OpenRouter is
  used for overflow, escalation, and committee independence, not routine coding.
- **The circuit breaker covers it too.** Repeated 429s or malformed tool calls
  pull a model out with exponential backoff, same as any local model.

## Budget

```bash
CTSWARM_BUDGET_USD_PER_BUILD=2.00     # spend above this needs approval
```

Exceeding it raises an approval card rather than silently continuing, and the
`spend_above_budget` rule classifies that as HIGH risk.

Check spend at any time:

```bash
./.venv/bin/ctswarm usage
./.venv/bin/ctswarm capacity
```

## Model selection

You do not pick OpenRouter models by hand. Add the key and run:

```bash
./.venv/bin/ctswarm bench --backend openrouter
```

The bench measures OpenRouter candidates on the same axes as local ones (tool
fidelity, schema adherence, long-context retrieval, cancellation) and writes them
into the same routing table. A cloud model that fails the tool-call gate is
excluded exactly like a local one; being paid buys no exemption.

## A cost reality check

A SWE-AF build makes **400 to 500+ agent invocations**. Measured on this machine,
a single trivial Claude call cost **$0.345**, almost entirely from fixed
system-prompt overhead rather than prompt size. Per-call overhead, multiplied by
hundreds of calls, is what determines the bill.

That is the whole argument for local-first routing, and the reason OpenRouter is
configured as overflow rather than as the default path.
