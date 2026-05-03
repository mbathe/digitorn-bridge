#!/usr/bin/env bash
#
# Smoke test for a cloud-deployed daemon's web_preview infrastructure.
# Run after every deploy or on a cron to alert on regressions.
#
# Usage:
#   ./check-preview-cloud.sh [daemon_url] [wildcard_base]
#
# Examples:
#   ./check-preview-cloud.sh https://daemon.digitorn.cloud preview.digitorn.cloud
#   ./check-preview-cloud.sh http://localhost:8000 localhost  # local rehearsal
#
# Exits non-zero on any check failure so it plays nicely with CI / cron.

set -euo pipefail

DAEMON_URL="${1:-http://localhost:8000}"
WILDCARD_BASE="${2:-localhost}"
PROBE_PORT="${PROBE_PORT:-9999}"
PROBE_HOST="preview-${PROBE_PORT}.${WILDCARD_BASE}"

CHECK_FAILED=0
fail() { echo "FAIL: $1" >&2; CHECK_FAILED=1; }
pass() { echo "  ok: $1"; }

# ── 1. Daemon is reachable ──────────────────────────────────────────
echo "==> Daemon health (${DAEMON_URL}/healthz)"
if curl -fsS "${DAEMON_URL}/healthz" >/dev/null; then
    pass "daemon /healthz returned 200"
else
    fail "daemon /healthz unreachable or non-2xx"
fi

# ── 2. web_preview module is loaded + reports stats ─────────────────
echo
echo "==> web_preview health (${DAEMON_URL}/health/web_preview)"
WP_HEALTH=$(curl -fsS "${DAEMON_URL}/health/web_preview" || true)
if [[ -z "$WP_HEALTH" ]]; then
    fail "/health/web_preview unreachable"
else
    echo "$WP_HEALTH" | jq -r '"  count=\(.count) by_type=\(.by_type) sessions=\(.session_count)"'
    if echo "$WP_HEALTH" | jq -e '.status == "ok" or .status == "unloaded"' >/dev/null; then
        pass "module reports status"
    else
        fail "module status not ok/unloaded"
    fi
fi

# ── 3. Wildcard DNS resolves ────────────────────────────────────────
echo
echo "==> Wildcard DNS (${PROBE_HOST})"
if getent hosts "${PROBE_HOST}" >/dev/null 2>&1 \
   || nslookup "${PROBE_HOST}" >/dev/null 2>&1; then
    pass "${PROBE_HOST} resolves"
else
    fail "${PROBE_HOST} does not resolve — check wildcard DNS / /etc/hosts"
fi

# ── 4. TLS cert valid (skip for plain http test) ────────────────────
if [[ "${DAEMON_URL}" =~ ^https:// ]]; then
    echo
    echo "==> Wildcard TLS cert"
    CERT_INFO=$(echo | openssl s_client \
        -servername "${PROBE_HOST}" \
        -connect "${PROBE_HOST}:443" 2>/dev/null \
        | openssl x509 -noout -dates -subject 2>/dev/null || true)
    if [[ -n "$CERT_INFO" ]]; then
        echo "$CERT_INFO" | sed 's/^/  /'
        pass "TLS handshake succeeded"
    else
        fail "TLS handshake failed for ${PROBE_HOST}:443"
    fi
fi

# ── 5. Edge proxy is alive (502/504 means it tries to forward) ──────
echo
echo "==> Edge proxy reachable (${PROBE_HOST})"
SCHEME="http"
if [[ "${DAEMON_URL}" =~ ^https:// ]]; then SCHEME="https"; fi
HTTP_CODE=$(curl -sIo /dev/null -w "%{http_code}" \
    --max-time 10 \
    "${SCHEME}://${PROBE_HOST}/" || echo "000")
case "$HTTP_CODE" in
    502|504)
        pass "edge proxy returned $HTTP_CODE — port ${PROBE_PORT} has no listener (expected for probe port)"
        ;;
    200|301|302|404)
        echo "  warn: got $HTTP_CODE on probe port ${PROBE_PORT} — something IS listening there"
        ;;
    000)
        fail "edge proxy unreachable / TLS error / timeout"
        ;;
    *)
        echo "  warn: unexpected HTTP $HTTP_CODE on ${PROBE_HOST}"
        ;;
esac

echo
if [[ $CHECK_FAILED -eq 0 ]]; then
    echo "✓ All preview-cloud checks passed"
    exit 0
else
    echo "✗ One or more checks failed — see above"
    exit 1
fi
