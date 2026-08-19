#!/usr/bin/env bash
#
# ctswarm bootstrap: clone to running, on Linux+CUDA or macOS+Apple Silicon.
#
# Design rules this script follows:
#   - Idempotent. Safe to re-run. Never overwrites an existing .env.
#   - Honest. Reports what it could not do and the exact command to fix it,
#     rather than continuing and failing later with a confusing error.
#   - Non-destructive. Does not upgrade or restart services you already run.
#     A factory that restarts your inference server during setup is a factory
#     that will restart it during a build.
#
# Usage:
#   ./bootstrap.sh              full setup
#   ./bootstrap.sh --revendor   re-fetch SWE-AF at the pinned commit
#   ./bootstrap.sh --no-models  skip model downloads
#   ./bootstrap.sh --check      report only, change nothing

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# shellcheck disable=SC1091
source infra/versions.env

REVENDOR=0
SKIP_MODELS=0
CHECK_ONLY=0
SUBSCRIPTION_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --revendor)   REVENDOR=1 ;;
    --no-models)  SKIP_MODELS=1 ;;
    --check)      CHECK_ONLY=1 ;;
    --subscription-only) SUBSCRIPTION_ONLY=1 ;;
    -h|--help)    sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

# A host with no accelerator has no usable local path, so probing for one and
# then downloading a model it cannot serve wastes an hour and several GB. Infer
# the mode rather than making the operator remember a flag, and let an explicit
# environment setting win over the guess either way.
case "${CTSWARM_EXECUTION_MODE:-}" in
  subscription_only) SUBSCRIPTION_ONLY=1 ;;
  hybrid)            SUBSCRIPTION_ONLY=0 ;;
esac

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; RESET=$'\033[0m'

say()   { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
ok()    { printf '  %sok%s   %s\n' "$GREEN" "$RESET" "$*"; }
warn()  { printf '  %swarn%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail()  { printf '  %sfail%s %s\n' "$RED" "$RESET" "$*"; }
note()  { printf '       %s%s%s\n' "$DIM" "$*" "$RESET"; }

TODO=()
todo() { TODO+=("$1"); }

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
say "Detecting platform"
# ---------------------------------------------------------------------------
OS="$(uname -s)"
ARCH="$(uname -m)"
ACCEL="cpu"

if [[ "$OS" == "Darwin" && ( "$ARCH" == "arm64" || "$ARCH" == "aarch64" ) ]]; then
  ACCEL="metal"
  MEM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
  ok "macOS Apple Silicon, ${MEM_GB}GB unified memory"
elif have nvidia-smi; then
  ACCEL="cuda"
  VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
  MEM_GB=$(( VRAM_MB / 1024 ))
  ok "${GPU_NAME}, ${MEM_GB}GB VRAM"
else
  MEM_GB=4
  # A GPU-less arm64 box is a single-board computer or a small VPS, not a
  # workstation someone forgot to plug a card into. Default it to the mode that
  # actually works there instead of warning about slow inference it will never
  # be asked to do.
  if [[ "$ARCH" == "aarch64" || "$ARCH" == "armv7l" ]]; then
    [[ "${CTSWARM_EXECUTION_MODE:-}" == "hybrid" ]] || SUBSCRIPTION_ONLY=1
    ok "arm64 host with no accelerator; using subscriptions-only execution"
  else
    warn "no GPU detected; local inference will run on CPU and be slow"
    note "run with --subscription-only to skip local models entirely"
  fi
fi

if [[ $SUBSCRIPTION_ONLY -eq 1 ]]; then
  say "Subscriptions-only mode"
  note "every agent role runs on the Claude Code and Codex CLIs"
  note "no local model server, no API keys, no router service"
  SKIP_MODELS=1
fi

# ---------------------------------------------------------------------------
say "Checking prerequisites"
# ---------------------------------------------------------------------------
for tool in git python3 docker; do
  if have "$tool"; then ok "$tool"; else
    fail "$tool not found"
    todo "install $tool"
  fi
done

if have python3; then
  PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  PY_OK=$(python3 -c 'import sys; print(1 if (3,11) <= sys.version_info < (3,14) else 0)')
  if [[ "$PY_OK" == "1" ]]; then ok "python $PY_VER"; else
    fail "python $PY_VER is outside the supported range (3.11 to 3.13)"
    todo "install python 3.12"
  fi
fi

if have docker && docker info >/dev/null 2>&1; then
  ok "docker daemon reachable"
else
  warn "docker daemon not reachable; the SWE-AF stack cannot start"
  todo "start Docker, or add your user to the docker group"
fi

have gh && ok "gh cli" || { warn "gh cli missing (needed for PR creation)"; todo "install gh"; }

# ---------------------------------------------------------------------------
say "Checking local inference backend"
# ---------------------------------------------------------------------------
LOCAL_BACKEND="none"

if [[ $SUBSCRIPTION_ONLY -eq 1 ]]; then
  note "not required in subscriptions-only mode"
elif [[ "$ACCEL" == "metal" ]] && python3 -c 'import mlx_lm' 2>/dev/null; then
  LOCAL_BACKEND="mlx"
  ok "mlx-lm available"
elif have ollama; then
  if curl -sf --max-time 3 http://localhost:11434/ >/dev/null 2>&1; then
    LOCAL_BACKEND="ollama"
    OLLAMA_VER=$(ollama --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo "unknown")
    ok "ollama $OLLAMA_VER running"
  else
    warn "ollama installed but not responding on :11434"
    todo "start ollama (systemctl start ollama, or run 'ollama serve')"
  fi
elif [[ "$ACCEL" == "metal" ]]; then
  warn "no local backend; on Apple Silicon prefer mlx-lm"
  note "pip install mlx-lm   (or install Ollama for the GGUF path)"
  todo "install mlx-lm or ollama"
else
  warn "no local backend detected"
  note "install from https://ollama.com/download"
  todo "install ollama"
fi

# A wedged runner blocks the whole queue while the control endpoints keep
# answering 200. Detect it here so setup does not hand over a broken backend.
if [[ "$LOCAL_BACKEND" == "ollama" ]]; then
  WEDGED=$(curl -sf --max-time 3 http://localhost:11434/api/ps 2>/dev/null \
    | python3 -c '
import json,sys,datetime
try: data=json.load(sys.stdin)
except Exception: sys.exit(0)
now=datetime.datetime.now(datetime.timezone.utc)
for m in data.get("models",[]):
    exp=m.get("expires_at")
    if not exp: continue
    try: dl=datetime.datetime.fromisoformat(exp.replace("Z","+00:00"))
    except Exception: continue
    if dl < now - datetime.timedelta(seconds=30): print(m.get("name",""))
' 2>/dev/null || true)
  if [[ -n "$WEDGED" ]]; then
    fail "wedged model runner(s): $WEDGED"
    note "these block every request for every other model while /v1/models still returns 200"
    note "fix: sudo systemctl restart ollama"
    todo "restart ollama to clear wedged runner(s)"
  fi
fi

# ---------------------------------------------------------------------------
say "Python environment"
# ---------------------------------------------------------------------------
if [[ $CHECK_ONLY -eq 0 ]]; then
  if [[ ! -d .venv ]]; then
    python3 -m venv .venv
    ok "created .venv"
  else
    ok ".venv exists"
  fi
  ./.venv/bin/pip install -q --upgrade pip >/dev/null 2>&1 || true
  ./.venv/bin/pip install -q -e ".[dev]"
  ok "installed ctswarm"
else
  note "check mode: skipping venv"
fi

# ---------------------------------------------------------------------------
say "Model candidates"
# ---------------------------------------------------------------------------
# Chosen by what fits the detected accelerator. Models that would spill past it
# are not pulled automatically; a 20GB download that then runs at 2 tok/s is not
# a favor. `ctswarm bench` decides what is actually usable.
if [[ $SKIP_MODELS -eq 1 || $CHECK_ONLY -eq 1 ]]; then
  note "skipping model downloads"
elif [[ "$LOCAL_BACKEND" == "ollama" ]]; then
  if   (( MEM_GB >= 24 )); then MODELS=("qwen3.5:9b" "granite4.1:8b" "qwen3.5:4b" "granite4.1:3b")
  elif (( MEM_GB >= 10 )); then MODELS=("qwen3.5:9b" "granite4.1:8b" "qwen3.5:4b" "granite4.1:3b")
  elif (( MEM_GB >= 6 ));  then MODELS=("qwen3.5:4b" "granite4.1:3b")
  else                          MODELS=("granite4.1:3b")
  fi
  INSTALLED=$(ollama list 2>/dev/null | tail -n +2 | awk '{print $1}')
  for model in "${MODELS[@]}"; do
    if grep -qx "$model" <<<"$INSTALLED"; then
      ok "$model already present"
    else
      say "  pulling $model"
      if ollama pull "$model" >/dev/null 2>&1; then ok "$model"; else
        warn "$model pull failed (may need a newer ollama)"
      fi
    fi
  done
elif [[ "$LOCAL_BACKEND" == "mlx" ]]; then
  note "MLX models download on first use via mlx_lm.server"
  note "start one with: python -m mlx_lm server --model mlx-community/Qwen3.5-9B-Instruct-4bit --port 8081"
  todo "start an mlx_lm server before running 'ctswarm bench'"
fi

# ---------------------------------------------------------------------------
say "Vendoring SWE-AF at pinned commit"
# ---------------------------------------------------------------------------
# Pinned rather than tracking main: SWE-AF is public beta with no tagged release,
# so following main would change the factory underneath a running pilot.
if [[ $CHECK_ONLY -eq 1 ]]; then
  note "check mode: skipping vendor"
elif [[ -d vendor/SWE-AF/.git ]] && [[ $REVENDOR -eq 0 ]]; then
  CURRENT=$(git -C vendor/SWE-AF rev-parse HEAD 2>/dev/null || echo none)
  if [[ "$CURRENT" == "$SWE_AF_COMMIT" ]]; then
    ok "SWE-AF at pinned ${SWE_AF_COMMIT:0:8}"
  else
    warn "SWE-AF at ${CURRENT:0:8}, pin is ${SWE_AF_COMMIT:0:8}"
    note "re-run with --revendor to move it"
  fi
else
  rm -rf vendor/SWE-AF
  mkdir -p vendor
  if git clone -q "$SWE_AF_REPO" vendor/SWE-AF 2>/dev/null; then
    git -C vendor/SWE-AF checkout -q "$SWE_AF_COMMIT"
    ok "SWE-AF pinned at ${SWE_AF_COMMIT:0:8} (${SWE_AF_COMMIT_DATE})"
  else
    fail "could not clone SWE-AF"
    todo "check network access to github.com"
  fi
fi

# Keep local safety fixes reproducible across fresh clones and --revendor.
if [[ $CHECK_ONLY -eq 0 && -d vendor/SWE-AF/.git ]]; then
  bash ./infra/apply-swe-af-patches.sh
  ok "applied ctswarm SWE-AF safety patches"
fi

# ---------------------------------------------------------------------------
say "Configuration"
# ---------------------------------------------------------------------------
# Writes a key only when .env has it present and empty, so an operator's value
# and a value from an earlier run both survive. The value arrives through the
# environment rather than argv, because a process list is world-readable.
fill_if_empty() {
  local key="$1"
  [[ -n "${CTSWARM_FILL_VALUE:-}" ]] || return 1
  CTSWARM_FILL_KEY="$key" python3 - <<'PYEOF'
import os
from pathlib import Path

key = os.environ["CTSWARM_FILL_KEY"]
value = os.environ["CTSWARM_FILL_VALUE"]
path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
for index, line in enumerate(lines):
    if line.rstrip("\n").strip() == f"{key}=":
        lines[index] = f"{key}={value}\n"
        path.write_text("".join(lines), encoding="utf-8")
        raise SystemExit(0)
raise SystemExit(1)
PYEOF
}

if [[ $CHECK_ONLY -eq 1 ]]; then
  note "check mode: skipping .env"
else
  if [[ ! -f .env ]]; then
    cp .env.example .env
    ok "created .env from .env.example"
  fi
  if [[ $SUBSCRIPTION_ONLY -eq 1 ]] && ! grep -q '^CTSWARM_EXECUTION_MODE=' .env; then
    sed -i.bak "s|^# *CTSWARM_EXECUTION_MODE=.*|CTSWARM_EXECUTION_MODE=subscription_only|" .env && rm -f .env.bak
    grep -q '^CTSWARM_EXECUTION_MODE=' .env \
      || printf '\nCTSWARM_EXECUTION_MODE=subscription_only\n' >> .env
    ok "pinned CTSWARM_EXECUTION_MODE=subscription_only"
  fi
  # Discovery runs on every bootstrap, not only the run that creates .env. The
  # first run is the worst possible moment to look for a login: the operator
  # has not made one yet, and this script is what tells them to. Looking only
  # then meant a `gh auth login` done afterwards never reached .env, so the
  # agent containers went off to open a pull request with no token, at the end
  # of a build rather than before it.
  if have gh && gh auth status >/dev/null 2>&1; then
    if CTSWARM_FILL_VALUE=$(gh auth token 2>/dev/null) && fill_if_empty GH_TOKEN; then
      ok "GH_TOKEN from gh cli"
    fi
    unset CTSWARM_FILL_VALUE
  fi
  if [[ -f .env ]]; then
    ok ".env present; existing values left untouched"
  fi
fi

# Docker turns a bind mount of a missing host file into a *directory*, silently.
# The agent containers mount four credential files individually, so on a host
# where a login has not happened yet, starting the stack first leaves the CLI
# facing a directory where its config should be and failing in a way that looks
# like a broken image rather than a missing login.
if [[ $CHECK_ONLY -eq 0 ]]; then
  CODEX_HOME="${CTSWARM_CODEX_HOME:-$HOME/.codex}"
  CLAUDE_CREDENTIALS="${CTSWARM_CLAUDE_CREDENTIALS:-$HOME/.claude/.credentials.json}"
  CLAUDE_CONFIG="${CTSWARM_CLAUDE_CONFIG:-$HOME/.claude.json}"
  created_placeholder=0
  mkdir -p "$CODEX_HOME" "$(dirname "$CLAUDE_CREDENTIALS")"
  for f in "$CLAUDE_CONFIG" "$CODEX_HOME/config.toml"; do
    if [[ ! -e "$f" ]]; then
      : > "$f"; chmod 600 "$f"; created_placeholder=1
    fi
  done
  # The credentials file needs a placeholder too, and specifically an empty JSON
  # object rather than an empty file. `claude setup-token` hands you a token for
  # .env and does not write this path, and on macOS the CLI uses the Keychain
  # and never writes it at all -- so without this the mount source is missing
  # and the container gets a directory where its credentials should be. An
  # unreadable directory is a hard failure; `{}` is simply "no stored
  # credentials", which falls through to CLAUDE_CODE_OAUTH_TOKEN as intended.
  # $CODEX_HOME/auth.json needs the same treatment for the same reason, and its
  # absence from this list is how a root-owned *directory* appeared at
  # ~/.codex/auth.json on the Pi: the agent services mount that exact path, so
  # starting the stack before a `codex login` created it. Then codex login
  # cannot write there either, and the operator is stuck needing root to undo
  # something that setting up the stack did to them.
  for f in "$CLAUDE_CREDENTIALS" "$CODEX_HOME/auth.json"; do
    if [[ ! -e "$f" ]]; then
      printf '{}\n' > "$f"; chmod 600 "$f"
      created_placeholder=1
    fi
  done
  (( created_placeholder )) && note "created config placeholders so bind mounts stay files, not directories"
fi

# Credentials are reported, never auto-acquired: both of these open an
# interactive browser flow that must not run unattended from a setup script.
HEADLESS=0
if [[ "$OS" == "Linux" && -z "${DISPLAY:-}" && -n "${SSH_CONNECTION:-}" ]]; then
  HEADLESS=1
fi

# Existence is not enough. The placeholder written above is a real file at the
# same path, so a bare `-f` test reports a login that this script itself just
# fabricated, and the summary then omits the one step that actually blocks a
# build. Match ctswarm/capacity.py: the file counts only if it parses as a JSON
# object with something in it.
credentials_present() {
  [[ -f "$1" ]] || return 1
  python3 - "$1" <<'PYEOF' 2>/dev/null
import json, sys
try:
    payload = json.loads(open(sys.argv[1], encoding="utf-8").read() or "{}")
except (OSError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if isinstance(payload, dict) and payload else 1)
PYEOF
}

if [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]] \
   || grep -qE '^CLAUDE_CODE_OAUTH_TOKEN=sk-' .env 2>/dev/null \
   || credentials_present "${CTSWARM_CLAUDE_CREDENTIALS:-$HOME/.claude/.credentials.json}"; then
  ok "claude_code runtime configured"
else
  warn "claude_code runtime not configured"
  # setup-token prints a token rather than waiting on a browser redirect, so
  # this one works over plain SSH with nothing else set up.
  note "run: claude setup-token    then put the value in .env"
  todo "configure CLAUDE_CODE_OAUTH_TOKEN"
fi

if credentials_present "${CTSWARM_CODEX_HOME:-$HOME/.codex}/auth.json"; then
  ok "codex runtime configured (ChatGPT login found)"
else
  warn "codex runtime not configured"
  if [[ $HEADLESS -eq 1 ]]; then
    # codex login completes against a loopback callback, which a headless box
    # cannot open. Forward the port from the machine that has a browser.
    note "codex login needs a browser callback this host cannot open."
    note "from your laptop:  ssh -L 1455:localhost:1455 ${USER}@$(hostname)"
    note "then, in that session:  codex login"
  else
    note "run: codex login"
  fi
  todo "run codex login"
fi

if [[ -n "${OPENROUTER_API_KEY:-}" ]] || grep -qE '^OPENROUTER_API_KEY=sk-or' .env 2>/dev/null; then
  ok "openrouter overflow configured"
else
  note "openrouter not configured (optional; used as overflow capacity)"
fi

if grep -qE '^SLACK_BOT_TOKEN=xox' .env 2>/dev/null; then
  ok "slack approvals configured"
else
  note "slack not configured; the local approval UI at :8091 will be used"
  note "to enable slack see docs/SLACK.md"
fi

# ---------------------------------------------------------------------------
say "Sandbox"
# ---------------------------------------------------------------------------
if [[ $CHECK_ONLY -eq 1 ]]; then
  note "check mode: skipping sandbox"
elif have npm; then
  if [[ -d sandbox/node_modules ]]; then
    ok "sandbox dependencies present"
  else
    (cd sandbox && npm install --silent >/dev/null 2>&1) && ok "sandbox dependencies installed" \
      || { warn "sandbox npm install failed"; todo "cd sandbox && npm install"; }
  fi
else
  # Not a build blocker, so it must not join the "before this can run a build"
  # list. The bundled sandbox is the target for `ctswarm verify` probes on this
  # host; a real build compiles and tests the *target* repository inside the
  # agent container, which carries its own Node. Saying otherwise sends a Pi
  # operator off to install a toolchain the factory never touches.
  note "npm not found; ctswarm verify will skip the bundled sandbox probes"
  note "nothing else needs it: target repositories are built inside the agent container"
fi

# ---------------------------------------------------------------------------
say "Summary"
# ---------------------------------------------------------------------------
echo
printf '  platform        %s / %s / %s\n' "$OS" "$ARCH" "$ACCEL"
if [[ $SUBSCRIPTION_ONLY -eq 1 ]]; then
  printf '  execution       subscriptions only (claude_code + codex)\n'
else
  printf '  local backend   %s\n' "$LOCAL_BACKEND"
fi
printf '  swe-af pin      %s\n' "${SWE_AF_COMMIT:0:8}"
echo

if (( ${#TODO[@]} )); then
  printf '  %sBefore this can run a build:%s\n' "$BOLD" "$RESET"
  for item in "${TODO[@]}"; do printf '    - %s\n' "$item"; done
  echo
fi

# `bootstrap.sh` has never started the stack, despite README claiming it does.
# Print the command rather than fixing the claim by surprising anyone.
if [[ $SUBSCRIPTION_ONLY -eq 1 ]]; then
  cat <<'NEXT'
  Next:
    ./.venv/bin/ctswarm doctor              inventory of what is wired up
    CTSWARM_PROFILE=pi ./stack.sh up        build and start the stack
    ./.venv/bin/ctswarm status              queue and build state

  Bench and the router are not used in this mode; every role runs on the
  Claude Code and Codex CLIs. See docs/RASPBERRY_PI.md for host setup.

NEXT
else
  cat <<'NEXT'
  Next:
    ./.venv/bin/ctswarm doctor    full inventory of what is wired up
    ./.venv/bin/ctswarm bench     qualify local models, write the routing table
    ./.venv/bin/ctswarm serve     start the router on :8090
    ./.venv/bin/ctswarm verify    run the self-verification probe suite

NEXT
fi
