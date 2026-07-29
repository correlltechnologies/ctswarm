---
name: architecture-constraints
description: Why ctswarm switches models at two levels, and what cannot be proxied
project: ctswarm
type: project
tags: [ctswarm, swe-af, routing, architecture]
---

The constraint that shapes ctswarm's whole design: **SWE-AF's three runtimes are
not interchangeable at the HTTP layer.**

- `claude_code` and `codex` are **CLI harnesses** driven by subscription logins
  (`claude setup-token`, `codex login`). They are not OpenAI-compatible HTTP
  endpoints, so **no proxy can route across them.**
- Only `open_code` talks to an arbitrary OpenAI-compatible base URL.

So switching happens at two levels, and conflating them produces a design that
cannot work:

| Level | Switches | Mechanism |
|---|---|---|
| Runtime | which harness runs a build | capacity manager sets the `runtime` field in the SWE-AF build request |
| Model | which model serves a request inside `open_code` | ctswarm router, an OpenAI-compatible gateway |

**The elegant part:** opencode's custom-provider config uses `provider/model`
format, and ctswarm's virtual models are named `ctswarm/high|med|low`. One string
is simultaneously a valid opencode model ID and a ctswarm tier, so
`SWE_MODEL_MED=ctswarm/med` satisfies both systems with no translation layer.

**Other load-bearing decisions:**

- SWE-AF is **vendored at a pinned commit**, never forked and never tracking
  `main`. It is public beta with zero tagged releases, so following main would
  change the factory underneath a running pilot.
- SWE-AF already has a HITL path (`swe_af/hitl/ask_user.py`) that posts approval
  requests and waits on a webhook. ctswarm **bridges** it rather than
  reimplementing it.
- A build makes **400 to 500+ agent invocations**, nearly all tool-call driven.
  This is why tool-call fidelity is a hard eligibility gate in the router, not a
  scoring term: a few percent malformed calls stalls the DAG rather than merely
  degrading output.

See [[hardware-and-models]].
