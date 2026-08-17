#!/usr/bin/env bash
#
# Stack control. Wraps docker compose so the flags cannot be forgotten.
#
# The --project-directory is not optional cosmetics. Without it, compose resolves
# every relative path in the overlay against vendor/SWE-AF/ instead of the repo
# root, which silently points the build context and the opencode provider config
# at directories that do not exist. It fails late and confusingly, so this script
# exists to make that unrepresentable.
#
#   ./stack.sh up        build and start everything
#   ./stack.sh start     start what is already built (no rebuild) -- use at boot
#   ./stack.sh down      stop, keep volumes
#   ./stack.sh logs      follow logs
#   ./stack.sh ps        service status
#   ./stack.sh restart   restart agents only (scheduler keeps monitoring)
#   ./stack.sh build svc build one service (all services when omitted)
#   ./stack.sh recreate svc  replace one service without touching dependencies
#   ./stack.sh nuke      stop and delete volumes (destroys build state)
#
# Environment:
#   CTSWARM_PROFILE=pi           add the small-host overlay (arm64, 4GB, no local models)
#   CTSWARM_LOCAL_MODELS=1       run the router; required for hybrid execution mode
#   CTSWARM_HEALTH_TIMEOUT_S=600 how long to wait for services (default 120)

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [[ ! -d vendor/SWE-AF ]]; then
  echo "vendor/SWE-AF is missing. Run ./bootstrap.sh first." >&2
  exit 1
fi

# The pinned upstream is patched with ctswarm's fail-closed execution
# invariants. Apply idempotently before Compose can build an agent image.
bash ./infra/apply-swe-af-patches.sh

# SWE-AF's compose reads its own .env. Keep it in sync rather than maintaining
# two credential files that will drift.
if [[ -f .env ]]; then
  cp .env vendor/SWE-AF/.env
fi

COMPOSE=(docker compose
  --project-name ctswarm
  --project-directory .
  -f vendor/SWE-AF/docker-compose.yml
  -f infra/docker-compose.ctswarm.yml)

# The small-host overlay drops the router, blanks API keys, and caps memory.
if [[ "${CTSWARM_PROFILE:-}" == "pi" ]]; then
  COMPOSE+=(-f infra/docker-compose.pi.yml)
fi

# The router only exists to serve local models. Asking for it on a host that
# has none produces a service that answers health checks and routes nothing.
LOCAL_MODELS=0
if [[ "${CTSWARM_LOCAL_MODELS:-}" == "1" ]]; then
  LOCAL_MODELS=1
  COMPOSE+=(--profile local-models)
elif [[ "${CTSWARM_PROFILE:-}" != "pi" ]]; then
  LOCAL_MODELS=1
fi

# Cold boot on a small host takes longer than a warm start on a workstation.
HEALTH_TIMEOUT_S="${CTSWARM_HEALTH_TIMEOUT_S:-120}"
HEALTH_ATTEMPTS=$(( HEALTH_TIMEOUT_S / 2 ))
(( HEALTH_ATTEMPTS < 1 )) && HEALTH_ATTEMPTS=1

# Waits for a URL, or returns non-zero. Named so the failure message can say
# which thing did not come up rather than "something timed out".
wait_for_http() {
  local label="$1" url="$2"
  for _ in $(seq 1 "$HEALTH_ATTEMPTS"); do
    if curl -sf --max-time 2 "$url" >/dev/null 2>&1; then
      echo "  ${label} healthy"
      return 0
    fi
    sleep 2
  done
  echo "${label} did not become healthy within ${HEALTH_TIMEOUT_S} seconds" >&2
  return 1
}

wait_for_infrastructure() {
  if [[ $LOCAL_MODELS -eq 1 ]]; then
    echo "waiting for control plane and router..."
  else
    echo "waiting for control plane..."
  fi
  wait_for_http "control plane" \
    "http://localhost:${CTSWARM_CONTROL_PLANE_PORT:-18080}/api/v1/health" || return 1
  if [[ $LOCAL_MODELS -eq 1 ]]; then
    wait_for_http "router" "http://localhost:8090/health" || return 1
  fi
}

refresh_mcp_inventory() {
  # The dashboard receives metadata only. Operational commands, arguments,
  # endpoints, environment, and credentials remain in the worker-only configs.
  python3 -m ctswarm.project_workspace \
    --write-inventory bench/results/mcp-inventory.json
}

# The set of services started before the agents. The router belongs here only
# when it is actually part of this configuration.
infrastructure_services() {
  local services=(control-plane build-db ctswarm-approvals)
  [[ $LOCAL_MODELS -eq 1 ]] && services+=(ctswarm-router)
  printf '%s\n' "${services[@]}"
}

bring_up() {
  local -a up_flags=("$@")
  refresh_mcp_inventory
  # Infrastructure first so a slow agent image build does not hide a broken
  # control plane behind it.
  mapfile -t _infra < <(infrastructure_services)
  "${COMPOSE[@]}" up -d "${up_flags[@]}" "${_infra[@]}"
  wait_for_infrastructure || exit 1
  "${COMPOSE[@]}" up -d "${up_flags[@]}"
  echo "waiting for scheduler..."
  wait_for_http "scheduler" "http://localhost:8092/health" || exit 1
  "${COMPOSE[@]}" ps
}

case "${1:-up}" in
  up)
    bring_up
    ;;
  start)
    # Boot path. Never builds: a rebuild at startup turns a power cycle into a
    # 45-minute outage on a small host, and a transient network failure during
    # it takes the whole factory down rather than restoring the last good one.
    bring_up --no-build
    ;;
  down)    "${COMPOSE[@]}" down ;;
  nuke)    "${COMPOSE[@]}" down -v ;;
  logs)    shift; "${COMPOSE[@]}" logs -f "$@" ;;
  ps)      "${COMPOSE[@]}" ps ;;
  restart)
    refresh_mcp_inventory
    "${COMPOSE[@]}" restart swe-agent swe-fast
    ;;
  build)   shift; "${COMPOSE[@]}" build "$@" ;;
  recreate)
    shift
    if [[ $# -eq 0 ]]; then
      echo "recreate requires at least one service name" >&2
      exit 2
    fi
    refresh_mcp_inventory
    "${COMPOSE[@]}" up -d --no-deps --force-recreate "$@"
    ;;
  config)  "${COMPOSE[@]}" config ;;
  *)       "${COMPOSE[@]}" "$@" ;;
esac
