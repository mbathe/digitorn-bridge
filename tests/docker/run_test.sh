#!/usr/bin/env bash
# One-shot test of the deployment recipe inside a clean Ubuntu 24.04
# container. Builds the image, brings up redis + daemon, polls /health,
# then dumps logs + exits.
#
# Run from any directory:
#   bash tests/docker/run_test.sh
#
# Pass --keep to leave the rig running after a successful health check
# (useful for poking at the daemon manually):
#   bash tests/docker/run_test.sh --keep

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/tests/docker/docker-compose.test.yml"
KEEP=0

for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=1 ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

cleanup() {
  if [ "$KEEP" -eq 0 ]; then
    echo ""
    echo "[run_test] tearing down…"
    docker compose -f "$COMPOSE_FILE" down -v --remove-orphans 2>/dev/null || true
  else
    echo ""
    echo "[run_test] --keep set; rig is still running."
    echo "  Stop it with:"
    echo "    docker compose -f tests/docker/docker-compose.test.yml down -v"
  fi
}
trap cleanup EXIT

echo "═══════════════════════════════════════════════════════════════════"
echo "  digitorn — deployment dry-run inside Ubuntu 24.04 container"
echo "═══════════════════════════════════════════════════════════════════"

# Phase 1 — build the image. This is where missing apt packages,
# missing Python deps, or pyproject typos surface.
echo ""
echo "[phase 1/3] docker compose build (this is where deps fail loud)"
echo ""
docker compose -f "$COMPOSE_FILE" build daemon 2>&1 | tee /tmp/digitorn-build.log

if ! grep -q "DONE" /tmp/digitorn-build.log && \
   ! docker images --format '{{.Repository}}' | grep -q docker-daemon; then
  # Build report didn't end with success — fail loud.
  if grep -qE "ERROR|error|failed" /tmp/digitorn-build.log; then
    echo ""
    echo "[run_test] BUILD FAILED — last 20 log lines above. Fix the deps."
    exit 1
  fi
fi

# Phase 2 — start the rig. up -d so we control the wait loop.
echo ""
echo "[phase 2/3] starting redis + daemon"
docker compose -f "$COMPOSE_FILE" up -d --no-build

# Phase 3 — health-check loop. Compose has a healthcheck but we want
# to gate the script on it explicitly with a friendly progress dot.
echo ""
echo "[phase 3/3] waiting for /health (max 90s)…"
deadline=$(( $(date +%s) + 90 ))
last_status=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  status="$(curl -fsS --max-time 3 http://127.0.0.1:8000/health 2>/dev/null \
            | grep -oE '"status":"[^"]+"' || true)"
  if [ -n "$status" ] && [ "$status" != "$last_status" ]; then
    echo "  $(date -Iseconds) — $status"
    last_status="$status"
  fi
  if echo "$status" | grep -q '"ok"'; then
    if curl -fsS --max-time 3 http://127.0.0.1:8000/health \
         | grep -q '"warming_up":false'; then
      echo ""
      echo "  ✓ daemon ready (warming_up=false)"
      echo ""
      curl -fsS http://127.0.0.1:8000/health \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print("  version:", d.get("version")); print("  socketio:", d.get("socketio")); print("  workers:", d.get("workers"))' 2>/dev/null \
        || true
      echo ""
      echo "═══════════════════════════════════════════════════════════════════"
      echo "  ✅ PASS — bootstrap recipe works on a clean Ubuntu 24.04 box"
      echo "═══════════════════════════════════════════════════════════════════"
      exit 0
    fi
  fi
  sleep 2
done

# Timeout — dump logs and fail.
echo ""
echo "  ✗ /health never returned warming_up=false within 90s"
echo ""
echo "[run_test] daemon container logs (last 60 lines):"
echo "───────────────────────────────────────────────────────────────────"
docker compose -f "$COMPOSE_FILE" logs --tail=60 daemon
echo "───────────────────────────────────────────────────────────────────"
echo ""
echo "[run_test] FAIL — see logs above. Fix and re-run."
exit 1
