---
name: hardware-and-models
description: ctswarm inference hardware, model findings, and the ollama wedge failure mode
project: ctswarm
type: project
tags: [ctswarm, ollama, models, hardware]
---

Quinn runs ctswarm on two machines: a Linux box (RTX 5070, **11.9GB usable VRAM**,
46.7GB RAM, Ollama) and a Mac that must be supported via **MLX**. The repo's model
catalog is platform-aware for exactly this reason, so the same clone picks GGUF
candidates on Linux and MLX quants on Apple Silicon.

**Known-bad models (verified 2026-07-29):**

- `ornith:9b` does not reliably complete. Two independent hangs, one on a
  tool-calling request and one on a trivial "Say OK", each >90s with no output.
  Quinn had already reported this independently. Do not assign it an agent role.
- `laguna-xs-2.1` cannot be pulled on ollama 0.31.1; the pull fails with a
  download prompt rather than a version error. Needs an ollama upgrade.

**The failure mode worth remembering:** a wedged Ollama runner blocks the *entire*
inference queue while `GET /v1/models` keeps returning 200 with a full model list.
Every other model's requests queue behind it forever, which looks like "all the
other models are timing out". Clearing it needs `sudo systemctl restart ollama`;
`ollama stop` does not work and the runner process is owned by the `ollama` user.

**Why this matters:** a per-model circuit breaker cannot see this. The other
models are fine, they just never get scheduled. ctswarm therefore has
`probe_generation()` (a real tiny generation under a short timeout) separate from
`health()`, plus `OllamaBackend.wedged_models()`.

See [[architecture-constraints]].
