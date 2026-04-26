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
HEALTH_URL="http://127.0.0.1:8000/health"

log() { printf "[deploy %s] %s\n" "$(date -Iseconds)" "$*"; }

if [ "$EUID" -ne 0 ]; then
  echo "Run as root (use sudo)" >&2
  exit 1
fi

cd "$REPO_DIR"

# 1. Pull the new code as the service user.
log "fetching origin/main"
sudo -u "$SERVICE_USER" git fetch --all --prune --quiet
sudo -u "$SERVICE_USER" git reset --hard origin/main --quiet

NEW_SHA="$(sudo -u "$SERVICE_USER" git rev-parse --short HEAD)"
log "head=$NEW_SHA"

# 2. Reinstall Python deps if pyproject.toml or any requirements*.txt changed.
DEPS_HASH_FILE="$REPO_DIR/.last_deploy_deps_hash"
NEW_DEPS_HASH="$(sha256sum pyproject.toml requirements*.txt 2>/dev/null \
                  | sha256sum | cut -c1-16)"
OLD_DEPS_HASH="$(cat "$DEPS_HASH_FILE" 2>/dev/null || echo none)"

if [ "$NEW_DEPS_HASH" != "$OLD_DEPS_HASH" ]; then
  log "deps changed ($OLD_DEPS_HASH → $NEW_DEPS_HASH) — pip install"
  sudo -u "$SERVICE_USER" "$REPO_DIR/.venv/bin/pip" install --upgrade -e \
    "$REPO_DIR[postgres,redis,rss,pdf,presentation]"
  echo "$NEW_DEPS_HASH" > "$DEPS_HASH_FILE"
  chown "$SERVICE_USER:$SERVICE_USER" "$DEPS_HASH_FILE"
else
  log "deps unchanged — skipping pip install"
fi

# 3. Reload systemd if the unit file changed.
UNIT_SRC="$REPO_DIR/scripts/digitorn-daemon.service"
UNIT_DST="/etc/systemd/system/$SERVICE"
if ! cmp -s "$UNIT_SRC" "$UNIT_DST"; then
  log "unit file changed — reinstalling and reloading systemd"
  install -m 644 "$UNIT_SRC" "$UNIT_DST"
  systemctl daemon-reload
fi

# 4. Reload Caddy if Caddyfile changed.
CADDY_SRC="$REPO_DIR/scripts/Caddyfile"
CADDY_DST="/etc/caddy/Caddyfile"
# Replace the placeholder domain on the fly so the source stays generic.
DOMAIN="${DIGITORN_DOMAIN:-api.digitorn.ai}"
sed "s|api\.digitorn\.ai|$DOMAIN|g" "$CADDY_SRC" > /tmp/Caddyfile.new
if ! cmp -s /tmp/Caddyfile.new "$CADDY_DST"; then
  log "caddyfile changed — reloading caddy"
  install -m 644 /tmp/Caddyfile.new "$CADDY_DST"
  systemctl reload caddy
fi
rm -f /tmp/Caddyfile.new

# 5. Restart the daemon.
log "restarting $SERVICE"
systemctl restart "$SERVICE"

# 6. Wait for healthy.
log "waiting for $HEALTH_URL"
for i in $(seq 1 60); do
  if curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null \
      | grep -q '"status":"ok"'; then
    log "daemon healthy after ${i}s (head=$NEW_SHA)"
    exit 0
  fi
  sleep 1
done

log "FAIL — daemon not healthy after 60s"
log "tail of journal:"
journalctl -u "$SERVICE" -n 50 --no-pager
exit 1
