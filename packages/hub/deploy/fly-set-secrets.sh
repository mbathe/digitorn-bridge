#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────
# One-shot helper: set every required secret on Fly before first deploy.
#
# Usage:
#   1. source ~/.digitorn/oracle-cloud.env
#   2. export HUB_DATABASE_URL=postgresql+asyncpg://...   # the prod DB
#   3. ./deploy/fly-set-secrets.sh
# ──────────────────────────────────────────────────────────────────
set -euo pipefail

if ! command -v fly >/dev/null && ! command -v flyctl >/dev/null; then
  echo "✗ flyctl not installed. https://fly.io/docs/hands-on/install-flyctl/" >&2
  exit 1
fi
FLY="$(command -v fly || command -v flyctl)"

: "${HUB_DATABASE_URL:?HUB_DATABASE_URL is required (Neon Postgres connection string)}"
: "${OCI_S3_ENDPOINT:?source ~/.digitorn/oracle-cloud.env first}"
: "${OCI_ACCESS_KEY:?source ~/.digitorn/oracle-cloud.env first}"
: "${OCI_SECRET_KEY:?source ~/.digitorn/oracle-cloud.env first}"
: "${OCI_BUCKET:?source ~/.digitorn/oracle-cloud.env first}"

JWT_SECRET="${HUB_JWT_SECRET:-$(openssl rand -hex 32)}"
CORS_ORIGINS="${HUB_CORS_ORIGINS:-*}"

echo "→ Setting Fly secrets for app digitorn-hub …"

"$FLY" secrets set --app digitorn-hub --stage \
  HUB_DATABASE_URL="$HUB_DATABASE_URL" \
  HUB_JWT_SECRET="$JWT_SECRET" \
  HUB_S3_ENDPOINT_URL="$OCI_S3_ENDPOINT" \
  HUB_S3_ACCESS_KEY="$OCI_ACCESS_KEY" \
  HUB_S3_SECRET_KEY="$OCI_SECRET_KEY" \
  HUB_S3_BUCKET="$OCI_BUCKET" \
  HUB_CORS_ORIGINS="$CORS_ORIGINS"

echo "✓ Secrets staged. They will activate on the next 'fly deploy'."
echo
echo "Generated values you may want to keep:"
echo "  HUB_JWT_SECRET=$JWT_SECRET"
