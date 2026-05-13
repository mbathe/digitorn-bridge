#!/usr/bin/env bash
# Pull latest main + restart the daemon. Called by GH Actions on push,
# also runnable manually:
#
#     sudo /opt/digitorn-bridge/scripts/deploy.sh
#
# Idempotent. Re-installs Python deps only when pyproject.toml changed.
# Reloads systemd if the unit file in the repo changed.

set -euo pipefail

REPO_DIR="${DIGITORN_INSTALL_DIR:-/opt/digitorn-bridge}"
SERVICE_USER="${DIGITORN_USER:-digitorn}"
SERVICE="digitorn-daemon.service"
GATEWAY_SERVICE="digitorn-gateway.service"
HEALTH_URL="http://127.0.0.1:8000/health"
GATEWAY_HEALTH_URL="http://127.0.0.1:8002/healthz"

log() { printf "[deploy %s] %s\n" "$(date -Iseconds)" "$*"; }

if [ "$EUID" -ne 0 ]; then
  echo "Run as root (use sudo)" >&2
  exit 1
fi

cd "$REPO_DIR"

# 1. Pull the new code as the service user.
# Capture the CURRENT head BEFORE the reset so we can roll back on any
# downstream failure (pre-flight import, migration, health check). The
# previous deploy's deps + migrations are still consistent with this
# SHA, so a ``git reset --hard $OLD_SHA`` + restart gets us back to a
# known-good state instantly.
OLD_SHA="$(sudo -u "$SERVICE_USER" git rev-parse --short HEAD 2>/dev/null || echo unknown)"
log "current=$OLD_SHA (will roll back here on failure)"

log "fetching origin/main"
sudo -u "$SERVICE_USER" git fetch --all --prune --quiet
sudo -u "$SERVICE_USER" git reset --hard origin/main --quiet

NEW_SHA="$(sudo -u "$SERVICE_USER" git rev-parse --short HEAD)"
log "head=$NEW_SHA"

# Single source of truth for "something failed, restore the previous
# code on disk and restart so the services keep serving the OLD
# (known-good) build. We do NOT auto-downgrade alembic migrations -
# that's destructive (can drop columns / lose data). If the failure
# was a migration, the operator must verify the DB state by hand.
rollback() {
  local reason="$1"
  if [ "$OLD_SHA" = "unknown" ] || [ "$OLD_SHA" = "$NEW_SHA" ]; then
    log "rollback skipped ($reason): OLD_SHA=$OLD_SHA NEW_SHA=$NEW_SHA"
    return
  fi
  log "ROLLBACK: $reason - restoring code to $OLD_SHA"
  sudo -u "$SERVICE_USER" git reset --hard "$OLD_SHA" --quiet || \
    log "rollback git reset FAILED - manual intervention required"
  systemctl restart "$SERVICE" "$GATEWAY_SERVICE" 2>/dev/null || true
  log "rollback complete (services restarted on $OLD_SHA)"
  log "NOTE: alembic migrations were NOT auto-downgraded -- check the gateway DB schema if the failure was migration-related"
}

# 2. Reinstall Python deps if pyproject.toml / poetry.lock / requirements*.txt
#    changed. ``nullglob`` makes the requirements glob disappear (instead of
#    being treated as a literal filename) when no such file exists — without
#    it, ``set -euo pipefail`` would kill the script on ``sha256sum: requirements*.txt: No such file``.
DEPS_HASH_FILE="$REPO_DIR/.last_deploy_deps_hash"
shopt -s nullglob
DEPS_FILES=(
  pyproject.toml
  poetry.lock
  requirements*.txt
  packages/auth/pyproject.toml
  packages/gateway/pyproject.toml
)
shopt -u nullglob
NEW_DEPS_HASH="$(sha256sum "${DEPS_FILES[@]}" | sha256sum | cut -c1-16)"
OLD_DEPS_HASH="$(cat "$DEPS_HASH_FILE" 2>/dev/null || echo none)"

if [ "$NEW_DEPS_HASH" != "$OLD_DEPS_HASH" ]; then
  log "deps changed ($OLD_DEPS_HASH → $NEW_DEPS_HASH) — pip install"
  sudo -u "$SERVICE_USER" "$REPO_DIR/.venv/bin/pip" install --upgrade -e \
    "$REPO_DIR[postgres,redis,rss,pdf,presentation]"
  # digitorn-auth is a sibling package consumed by the daemon for
  # remote auth (RemoteAuthMiddleware + RemoteAuthClient). Without
  # it, server.py crashes at startup on `import digitorn_auth`.
  sudo -u "$SERVICE_USER" "$REPO_DIR/.venv/bin/pip" install --upgrade -e \
    "$REPO_DIR/packages/auth"
  # digitorn-gateway is the OpenAI-compatible LLM router. The
  # daemon's outbound LLM calls hit ``gateway.digitorn.ai`` which is
  # served by ``digitorn-gateway.service`` from this same repo.
  sudo -u "$SERVICE_USER" "$REPO_DIR/.venv/bin/pip" install --upgrade -e \
    "$REPO_DIR/packages/gateway"
  echo "$NEW_DEPS_HASH" > "$DEPS_HASH_FILE"
  chown "$SERVICE_USER:$SERVICE_USER" "$DEPS_HASH_FILE"
else
  log "deps unchanged — skipping pip install"
fi

# 3. Reload systemd if either unit file changed.
UNIT_SRC="$REPO_DIR/scripts/digitorn-daemon.service"
UNIT_DST="/etc/systemd/system/$SERVICE"
GATEWAY_UNIT_SRC="$REPO_DIR/scripts/digitorn-gateway.service"
GATEWAY_UNIT_DST="/etc/systemd/system/$GATEWAY_SERVICE"
NEEDS_DAEMON_RELOAD=0
if ! cmp -s "$UNIT_SRC" "$UNIT_DST"; then
  log "daemon unit file changed — reinstalling"
  install -m 644 "$UNIT_SRC" "$UNIT_DST"
  NEEDS_DAEMON_RELOAD=1
fi
if ! cmp -s "$GATEWAY_UNIT_SRC" "$GATEWAY_UNIT_DST"; then
  log "gateway unit file changed — reinstalling + enabling"
  install -m 644 "$GATEWAY_UNIT_SRC" "$GATEWAY_UNIT_DST"
  NEEDS_DAEMON_RELOAD=1
  # First-time activation: enable so it starts at boot. Idempotent.
  systemctl enable "$GATEWAY_SERVICE" --quiet
fi
if [ "$NEEDS_DAEMON_RELOAD" -eq 1 ]; then
  systemctl daemon-reload
fi

# 4. Reload Caddy if Caddyfile changed.
CADDY_SRC="$REPO_DIR/scripts/Caddyfile"
CADDY_DST="/etc/caddy/Caddyfile"
# Replace the placeholder domains on the fly so the source stays generic.
DOMAIN="${DIGITORN_DOMAIN:-api.digitorn.ai}"
GATEWAY_DOMAIN="${DIGITORN_GATEWAY_DOMAIN:-gateway.digitorn.ai}"
sed -e "s|api\.digitorn\.ai|$DOMAIN|g" \
    -e "s|gateway\.digitorn\.ai|$GATEWAY_DOMAIN|g" \
    "$CADDY_SRC" > /tmp/Caddyfile.new
if ! cmp -s /tmp/Caddyfile.new "$CADDY_DST"; then
  log "caddyfile changed — reloading caddy"
  install -m 644 /tmp/Caddyfile.new "$CADDY_DST"
  systemctl reload caddy
fi
rm -f /tmp/Caddyfile.new

# 4.5. Pre-flight: import the daemon + gateway packages BEFORE we
#       touch the running services. Catches syntax errors / broken
#       imports introduced by the push without ever restarting (the
#       OLD code keeps serving). A failed import here is the cheapest
#       possible "deploy abort".
log "pre-flight: import daemon + gateway"
if ! sudo -u "$SERVICE_USER" "$REPO_DIR/.venv/bin/python" -c "
import digitorn.core.server
import digitorn_gateway.main
" 2>&1 | sed 's/^/[deploy preflight] /'; then
  rollback "pre-flight import check failed"
  exit 1
fi
log "pre-flight OK"

# 4.6. Run gateway alembic migrations. The repo's HEAD may carry new
#      schema changes (added columns, new tables) that the running
#      gateway code expects. Without this, the gateway boots OK but
#      first real request crashes with "column does not exist".
#      Idempotent: alembic skips revisions that are already at head.
#
#      ``cd`` into the gateway dir BEFORE invoking alembic: the
#      ``script_location = alembic`` in alembic.ini is resolved
#      relative to the current working directory, NOT the config
#      file. Running from $REPO_DIR with -c packages/gateway/alembic.ini
#      would make alembic look for $REPO_DIR/alembic/ (wrong path).
log "running gateway alembic migrations"
if ! sudo -u "$SERVICE_USER" bash -c "cd '$REPO_DIR/packages/gateway' && '$REPO_DIR/.venv/bin/alembic' upgrade head" 2>&1 | sed 's/^/[deploy alembic] /'; then
  rollback "alembic upgrade failed"
  exit 1
fi
log "alembic upgrade OK"

# 5. Restart the daemon AND the gateway. We do them in parallel-ish:
#    the daemon takes ~3-5s to come back, the gateway is faster.
log "restarting $SERVICE + $GATEWAY_SERVICE"
systemctl restart "$SERVICE"
systemctl restart "$GATEWAY_SERVICE"

# 6. Wait for daemon healthy first (it's the bigger one).
log "waiting for $HEALTH_URL"
DAEMON_OK=0
for i in $(seq 1 60); do
  if curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null \
      | grep -q '"status":"ok"'; then
    log "daemon healthy after ${i}s (head=$NEW_SHA)"
    DAEMON_OK=1
    break
  fi
  sleep 1
done
if [ "$DAEMON_OK" -ne 1 ]; then
  log "FAIL — daemon not healthy after 60s"
  log "tail of $SERVICE journal:"
  journalctl -u "$SERVICE" -n 50 --no-pager
  rollback "daemon health check failed"
  exit 1
fi

# 7. Wait for gateway healthy.
log "waiting for $GATEWAY_HEALTH_URL"
for i in $(seq 1 30); do
  # The gateway returns ``{"status": "alive"}`` on /healthz, not
  # ``{"status": "ok"}`` like the daemon. Match both shapes loosely.
  if curl -fsS --max-time 3 "$GATEWAY_HEALTH_URL" 2>/dev/null \
      | grep -q '"status"'; then
    log "gateway healthy after ${i}s (head=$NEW_SHA)"
    exit 0
  fi
  sleep 1
done

log "FAIL — gateway not healthy after 30s"
log "tail of $GATEWAY_SERVICE journal:"
journalctl -u "$GATEWAY_SERVICE" -n 50 --no-pager
rollback "gateway health check failed"
exit 1
