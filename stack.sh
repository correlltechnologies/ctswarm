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
#   ./stack.sh down      stop, keep volumes
#   ./stack.sh logs      follow logs
#   ./stack.sh ps        service status
#   ./stack.sh restart   restart agents only (scheduler keeps monitoring)
#   ./stack.sh build svc build one service (all services when omitted)
#   ./stack.sh recreate svc  replace one service without touching dependencies
#   ./stack.sh nuke      stop and delete volumes (destroys build state)

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

case "${1:-up}" in
  up)
    # Infrastructure first so a slow agent image build does not hide a broken
    # control plane or router behind it.
    "${COMPOSE[@]}" up -d control-plane build-db ctswarm-router ctswarm-approvals
    echo "waiting for router and control plane..."
    infrastructure_ready=0
    for _ in $(seq 1 60); do
      if curl -sf --max-time 2 http://localhost:8090/health >/dev/null 2>&1 \
         && curl -sf --max-time 2 http://localhost:${CTSWARM_CONTROL_PLANE_PORT:-18080}/api/v1/health >/dev/null 2>&1; then
        echo "  infrastructure healthy"
        infrastructure_ready=1
        break
      fi
      sleep 2
    done
    if [[ $infrastructure_ready -ne 1 ]]; then
      echo "infrastructure did not become healthy within 120 seconds" >&2
      exit 1
    fi
    "${COMPOSE[@]}" up -d
    echo "waiting for scheduler..."
    scheduler_ready=0
    for _ in $(seq 1 60); do
      if curl -sf --max-time 2 http://localhost:8092/health >/dev/null 2>&1; then
        echo "  scheduler healthy"
        scheduler_ready=1
        break
      fi
      sleep 2
    done
    if [[ $scheduler_ready -ne 1 ]]; then
      echo "scheduler did not become healthy within 120 seconds" >&2
      exit 1
    fi
    "${COMPOSE[@]}" ps
    ;;
  down)    "${COMPOSE[@]}" down ;;
  nuke)    "${COMPOSE[@]}" down -v ;;
  logs)    shift; "${COMPOSE[@]}" logs -f "$@" ;;
  ps)      "${COMPOSE[@]}" ps ;;
  restart) "${COMPOSE[@]}" restart swe-agent swe-fast ;;
  build)   shift; "${COMPOSE[@]}" build "$@" ;;
  recreate)
    shift
    if [[ $# -eq 0 ]]; then
      echo "recreate requires at least one service name" >&2
      exit 2
    fi
    "${COMPOSE[@]}" up -d --no-deps --force-recreate "$@"
    ;;
  config)  "${COMPOSE[@]}" config ;;
  *)       "${COMPOSE[@]}" "$@" ;;
esac
