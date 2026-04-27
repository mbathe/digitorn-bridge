#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu 22/24 box for the digitorn-bridge daemon.
#
# Idempotent — safe to re-run. Run as root (or with ``sudo``):
#
#     curl -fsSL https://raw.githubusercontent.com/<OWNER>/digitorn-bridge/main/scripts/bootstrap.sh | sudo bash
#
# What it sets up
# ---------------
#   • Python 3.12 + venv in /opt/digitorn-bridge/.venv
#   • Service user "digitorn" with /home/digitorn
#   • Redis 7 (apt) — listens on 127.0.0.1:6379
#   • Caddy 2  (apt) — auto-TLS for $DOMAIN, reverse-proxies :8000
#   • systemd unit digitorn-daemon.service (loaded, NOT started — fill .env first)
#   • UFW with 22 / 80 / 443 open
#   • Sudoers rule so the SSH deploy user can restart the service / run deploy.sh
#
# After this script finishes, you still must:
#   1. Fill /etc/digitorn/digitorn.env with real secrets (DB URL, JWT secret, API keys)
#   2. Point the DNS A record for $DOMAIN to this box's public IP
#   3. systemctl start digitorn-daemon
#   4. Add the GitHub Actions secrets (HETZNER_HOST, HETZNER_USER, HETZNER_SSH_KEY)

set -euo pipefail

# ── Config (override via env) ─────────────────────────────────────────
DOMAIN="${DIGITORN_DOMAIN:-api.digitorn.ai}"
REPO_URL="${DIGITORN_REPO_URL:-https://github.com/mbathe/digitorn-bridge.git}"
REPO_BRANCH="${DIGITORN_REPO_BRANCH:-main}"
SERVICE_USER="${DIGITORN_USER:-digitorn}"
INSTALL_DIR="${DIGITORN_INSTALL_DIR:-/opt/digitorn-bridge}"
ENV_DIR="/etc/digitorn"
DEPLOY_SUDO_USER="${DEPLOY_SUDO_USER:-ubuntu}"  # the SSH user GH Actions logs in as

if [ "$EUID" -ne 0 ]; then
  echo "Run as root: sudo bash $0" >&2
  exit 1
fi

echo "═════════════════════════════════════════════════════════════════"
echo "  digitorn-bridge bootstrap"
echo "  domain      : $DOMAIN"
echo "  repo        : $REPO_URL ($REPO_BRANCH)"
echo "  service user: $SERVICE_USER"
echo "  install dir : $INSTALL_DIR"
echo "  deploy ssh  : $DEPLOY_SUDO_USER"
echo "═════════════════════════════════════════════════════════════════"

# ── 1. APT packages ──────────────────────────────────────────────────
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  ca-certificates curl gnupg lsb-release \
  git build-essential pkg-config \
  redis-server \
  ufw \
  debian-keyring debian-archive-keyring apt-transport-https

# ── 2. Python 3.12 (deadsnakes for 22.04, native for 24.04) ───────────
# IMPORTANT: install ``python3.12-venv`` and ``python3.12-dev`` even when
# the python3.12 binary is already present (Ubuntu 24.04 ships
# python3.12 by default but NOT the venv module — that's a separate
# apt package). Without this, ``python3.12 -m venv`` fails with
# ``ensurepip is not available``.
if ! command -v python3.12 >/dev/null 2>&1; then
  if grep -q "^VERSION_ID=\"22.04\"" /etc/os-release; then
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update
  fi
  apt-get install -y python3.12
fi
apt-get install -y python3.12-venv python3.12-dev

# ── 3. Caddy (official cloudsmith repo) ──────────────────────────────
if ! command -v caddy >/dev/null 2>&1; then
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi

# ── 4. Service user ──────────────────────────────────────────────────
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/$SERVICE_USER" \
    --shell /bin/bash "$SERVICE_USER"
fi

# ── 5. Clone repo ────────────────────────────────────────────────────
# Pre-create the install dir so ``digitorn`` (a system user that doesn't
# have write access to /opt) can clone into it. Cloning as root and then
# chowning the tree is simpler than juggling permissions on /opt.
if [ ! -d "$INSTALL_DIR/.git" ]; then
  rm -rf "$INSTALL_DIR"
  git clone --branch "$REPO_BRANCH" --depth 1 "$REPO_URL" "$INSTALL_DIR"
else
  sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" fetch --all --prune
  sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" reset --hard "origin/$REPO_BRANCH"
fi

# Always re-own — covers both the fresh-clone-as-root path AND any new
# files git pulled in (e.g. on update).
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# ── 6. Python venv + project install ─────────────────────────────────
# Check on ``bin/pip`` instead of ``bin/python``: a previous failed
# ``python -m venv`` (e.g. when python3.12-venv was missing) leaves a
# half-built tree where ``bin/python`` is a symlink but ``bin/pip``
# was never installed. Re-running the bootstrap then sees python and
# skips creation, but pip is missing and everything below fails.
if [ ! -x "$INSTALL_DIR/.venv/bin/pip" ]; then
  rm -rf "$INSTALL_DIR/.venv"
  sudo -u "$SERVICE_USER" python3.12 -m venv "$INSTALL_DIR/.venv"
fi
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip wheel
# Extras: postgres for Neon, redis for Socket.IO + queue, rss/pdf/presentation
# for the corresponding builtin apps (smart-rss-digest, PDF tools, slide makers).
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -e \
  "$INSTALL_DIR[postgres,redis,rss,pdf,presentation]"

# ── 7. Config dirs ───────────────────────────────────────────────────
mkdir -p "$ENV_DIR" /var/log/digitorn
chown root:"$SERVICE_USER" "$ENV_DIR"
chmod 750 "$ENV_DIR"
chown "$SERVICE_USER:$SERVICE_USER" /var/log/digitorn

# Per-user digitorn config (~digitorn/.digitorn/config.yaml)
sudo -u "$SERVICE_USER" mkdir -p "/home/$SERVICE_USER/.digitorn"
if [ ! -f "/home/$SERVICE_USER/.digitorn/config.yaml" ]; then
  sudo -u "$SERVICE_USER" tee "/home/$SERVICE_USER/.digitorn/config.yaml" > /dev/null <<EOF
# digitorn config.yaml — production
server:
  host: "127.0.0.1"
  port: 8000
  workers: 1
  reload: false
  auth_enabled: true
  sandbox: true
  rate_limit_rpm: 600
  turn_workers: 8
  io_workers: 16
  kv_backend: "redis://127.0.0.1:6379/0"
  cors_origins:
    - "https://$DOMAIN"
    - "https://digitorn.ai"
    - "https://app.digitorn.ai"

logging:
  level: info
  format: json

# Database URL is loaded from /etc/digitorn/digitorn.env via systemd
# EnvironmentFile (DIGITORN_DATABASE__URL).
EOF
fi

# Sample env file (NEVER commit secrets)
if [ ! -f "$ENV_DIR/digitorn.env" ]; then
  cat > "$ENV_DIR/digitorn.env" <<'EOF'
# /etc/digitorn/digitorn.env — fill these and never commit.
# Format is KEY=value, no quotes, no shell expansion.

# Postgres (Neon EU pooled URL, asyncpg driver, ssl=require)
DIGITORN_DATABASE__URL=postgresql+asyncpg://USER:PASS@HOST/DB?ssl=require

# JWT — generate with: openssl rand -hex 64
DIGITORN_AUTH__JWT_SECRET=change-me-change-me-change-me-change-me-change-me-change-me-change-me

# LLM provider keys (only what you actually use)
DEEPSEEK_API_KEY=sk-...
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
EOF
  chmod 640 "$ENV_DIR/digitorn.env"
  chown root:"$SERVICE_USER" "$ENV_DIR/digitorn.env"
  echo "  ⚠  Edit $ENV_DIR/digitorn.env before starting the daemon."
fi

# ── 8. systemd unit ──────────────────────────────────────────────────
install -m 644 "$INSTALL_DIR/scripts/digitorn-daemon.service" \
  /etc/systemd/system/digitorn-daemon.service

# Patch user/paths into the unit (in case service user/path differ from defaults)
sed -i \
  -e "s|^User=.*|User=$SERVICE_USER|" \
  -e "s|^Group=.*|Group=$SERVICE_USER|" \
  -e "s|^WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|" \
  -e "s|/opt/digitorn-bridge|$INSTALL_DIR|g" \
  /etc/systemd/system/digitorn-daemon.service

# ── 9. Caddyfile ─────────────────────────────────────────────────────
sed "s|api\.digitorn\.ai|$DOMAIN|g" "$INSTALL_DIR/scripts/Caddyfile" \
  > /etc/caddy/Caddyfile

mkdir -p /var/log/caddy
chown caddy:caddy /var/log/caddy 2>/dev/null || true

# ── 10. UFW ──────────────────────────────────────────────────────────
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

# ── 11. Sudoers rule for the deploy user ─────────────────────────────
# Lets GH Actions run scripts/deploy.sh and a couple of systemctl
# commands without a password — restricted to those exact paths.
cat > /etc/sudoers.d/digitorn-deploy <<EOF
# Managed by scripts/bootstrap.sh — DO NOT EDIT manually.
$DEPLOY_SUDO_USER ALL=(ALL) NOPASSWD: $INSTALL_DIR/scripts/deploy.sh
$DEPLOY_SUDO_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart digitorn-daemon
$DEPLOY_SUDO_USER ALL=(ALL) NOPASSWD: /bin/systemctl status digitorn-daemon
$DEPLOY_SUDO_USER ALL=(ALL) NOPASSWD: /bin/systemctl daemon-reload
$DEPLOY_SUDO_USER ALL=(ALL) NOPASSWD: /bin/systemctl reload caddy
EOF
chmod 440 /etc/sudoers.d/digitorn-deploy

# ── 12. Enable services ──────────────────────────────────────────────
systemctl daemon-reload
systemctl enable redis-server caddy digitorn-daemon
systemctl restart redis-server caddy

cat <<EOF

═════════════════════════════════════════════════════════════════
  Bootstrap complete.
═════════════════════════════════════════════════════════════════

Next steps:

  1. Fill the secrets:
       sudo -e $ENV_DIR/digitorn.env

  2. Point DNS:
       $DOMAIN  ─►  $(curl -s https://api.ipify.org)

  3. Start the daemon:
       sudo systemctl start digitorn-daemon
       sudo journalctl -u digitorn-daemon -f

  4. Confirm it's healthy from the outside (after DNS propagates):
       curl https://$DOMAIN/health

  5. Add 3 GitHub repo secrets at
     Settings ▸ Secrets and variables ▸ Actions:
       HETZNER_HOST     = $(curl -s https://api.ipify.org)
       HETZNER_USER     = $DEPLOY_SUDO_USER
       HETZNER_SSH_KEY  = (private key paired with the public key
                           in $(eval echo "~$DEPLOY_SUDO_USER")/.ssh/authorized_keys)

  Then every push to $REPO_BRANCH redeploys automatically.

EOF
