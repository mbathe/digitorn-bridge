# digitorn-gateway — operations runbook

Live-cheat-sheet for operating the gateway in dev and prod.

---

## Config precedence

Pydantic Settings reads from these sources, HIGH to LOW priority:

1. **Process env vars** (everything matching `DIGITORN_GATEWAY_*`)
2. **`~/.digitorn/gateway.env`** (operator-managed secrets + overrides)
3. **Defaults baked into `digitorn_gateway/config.py`**

> **Footgun**: a stale `$env:DIGITORN_GATEWAY_AUTH_JWKS_URL` (or any other override) in your shell will SILENTLY override `gateway.env` and the gateway will appear to ignore your config file.

**Always launch via `scripts/start-gateway.ps1` (Windows) or `scripts/start-gateway.sh` (Linux/Mac, mirrors the same contract).** The script clears all `DIGITORN_GATEWAY_*` from the launching shell before exec.

---

## Required config in `~/.digitorn/gateway.env`

```ini
# Database (Postgres asyncpg DSN)
DIGITORN_GATEWAY_DATABASE_URL=postgresql+asyncpg://user:pass@host/db?ssl=require

# Master key for credential encryption (32 bytes base64url)
DIGITORN_GATEWAY_MASTER_KEY=...

# JWT issuer the auth service hardcodes in tokens
DIGITORN_GATEWAY_AUTH_ISSUER=digitorn

# Extra accepted issuers as JSON array (e.g. legacy URL)
DIGITORN_GATEWAY_AUTH_ACCEPT_ISSUERS=["https://auth.digitorn.ai"]

# JWKS endpoint the gateway fetches public keys from at boot.
# Production: prod auth service. Dev with local auth: http://127.0.0.1:8001/.well-known/jwks.json
DIGITORN_GATEWAY_AUTH_JWKS_URL=https://auth.digitorn.ai/.well-known/jwks.json
```

---

## Boot

```powershell
# Windows
.\packages\gateway\scripts\start-gateway.ps1

# Linux/Mac (when added)
./packages/gateway/scripts/start-gateway.sh
```

The script:
1. Clears `DIGITORN_GATEWAY_*` from the current shell.
2. Kills any prior listener on `:8002`.
3. Launches `py -3.12 -m digitorn_gateway` detached, redirects logs to `~/.digitorn/logs/gateway-<timestamp>.log`.
4. Polls `/healthz` for up to 30s; non-zero exit on failure with stderr tail.

---

## Verify

After boot:

```powershell
# Health
curl http://127.0.0.1:8002/healthz

# Confirm JWKS keys are loaded (admin token required)
curl -H "Authorization: Bearer $TOK" http://127.0.0.1:8002/admin/diag/system | jq

# Confirm cache pricing columns are present
curl -H "Authorization: Bearer $TOK" http://127.0.0.1:8002/admin/models | jq '.rows[0] | keys'
```

Expected keys on a model row include: `cost_per_1k_cache_read_tokens`, `cost_per_1k_cache_write_tokens`.

---

## Seed cache pricing from LiteLLM (one-shot after migration 0017)

```powershell
curl -X POST -H "Authorization: Bearer $TOK" http://127.0.0.1:8002/admin/diag/seed-cache-pricing
```

Pass `?overwrite=true` to force re-sync (default behaviour preserves operator-set prices).

---

## Multi-process safety

In production on fly.io the platform supervisor guarantees a single process per machine (`processes = ["app"]` in `fly.toml`). Locally the boot script enforces "kill old before launching new".

**Never run `python -m digitorn_gateway` directly in dev without going through the script** — there is no built-in self-supervision and you risk having two listeners on the same port (we hit this in 2026-05-11 incident).

---

## Common failure modes and fixes

| Symptom | Cause | Fix |
|---|---|---|
| `401 invalid_token: bad_signature` on every request | Gateway loaded the wrong JWKS at boot | Verify `DIGITORN_GATEWAY_AUTH_JWKS_URL` in `gateway.env`, then restart via boot script |
| `401 issuer_mismatch (got 'digitorn', expected ...)` | `auth_issuer` doesn't match what the auth service emits | Set `DIGITORN_GATEWAY_AUTH_ISSUER=digitorn` (matches `jwt.py:_ISSUER`) |
| `502 gateway_unreachable` from daemon proxy | Daemon's `runtime.gateway_base_url` points at wrong port | Default daemon target is `127.0.0.1:8002/v1`. Either align gateway port or override `DIGITORN_RUNTIME__GATEWAY_BASE_URL` on the daemon |
| `429 quota_exceeded` under load | Per-user requests/min cap (default 60) | Increase the user's plan via `/admin/users/{id}` or wait for the window to reset |
| Two gateway processes after restart | Manual launch without `start-gateway.ps1` | Always use the script; it kills any prior listener first |

---

## Observability

| What | Where |
|---|---|
| Per-call audit log | Postgres `gateway_usage_events` (partitioned monthly) |
| Aggregations by provider/model | `GET /admin/usage/top-providers`, `top-models` (carries cache_read_tokens, cache_write_tokens, cache_hit_rate) |
| Month-to-date totals | `GET /admin/usage/summary` (includes cache_read / cache_write / cache_hit_rate) |
| Per-day timeline | `GET /admin/usage/timeline?metric=cache_read_tokens` (also accepts cache_write_tokens) |
| Live route health | `GET /admin/routes` -> `is_blocked`, `consecutive_failures`, `last_error` |
| Live credential health | `GET /admin/credentials/health` -> in-flight, 429 cooldown remaining, total dispatched |
| Quota supervisor state | `GET /admin/diag/system` -> `supervisor.{running,dirty_users,active_blocks}` |
