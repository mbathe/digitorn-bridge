# API Audit — real user scenarios

Test user: `audit<ts>` (created live, tokens in `/tmp/audit_*.txt`).
Started: 2026-04-24

## Legend
- ✅ OK — works as expected
- ⚠️ Quirk — works but with caveat (bad UX, inconsistent shape, undocumented)
- ❌ Bug — returns unexpected error, wrong status, or doesn't do what the docs say
- 🔐 Perm — requires admin/elevated perms (expected for non-admin test user)

## Log

| # | Method | Path | Status | Notes |
|---|---|---|---|---|
| 1 | POST | /auth/register | ✅ 200 | Requires `{username, email, password, display_name}`. Returns raw token response (not wrapped in `{success, data}` — inconsistent with rest of API). |
| 2 | POST | /auth/login | ✅ 200 | Same shape. Accepts `{username, password}`. |
| 3 | POST | /auth/refresh | ⚠️ 200 | Returns new access_token but `refresh_token: null` (no rotation) and `email`/`display_name` null. Client must not discard the old refresh_token — use it again next time. |
| 4 | GET | /auth/me | ✅ 200 | Full user profile. |
| 5 | POST | /auth/logout | ✅ 200 | Token is correctly invalidated (subsequent /auth/me → 401). |

## user/* endpoints

| # | Method | Path | Status | Notes |
|---|---|---|---|---|
| 6 | GET | /api/users/me/profile | ✅ 200 | Returns user profile with attributes. |
| 7 | GET | /api/users/me/devices | ✅ 200 | Empty list initially. |
| 8 | GET | /api/users/me/inbox | ✅ 200 | Paginated. |
| 9 | GET | /api/users/me/inbox/unread_count | ✅ 200 | |
| 10 | GET | /api/users/me/notification-prefs | ✅ 200 | Note: `notification-prefs` with hyphen. |
| 11 | GET | /api/users/me/quotas | ✅ 200 | Empty for non-admin. |
| 12 | GET | /api/users/me/sessions | ✅ 200 | Cross-app list with pagination. |
| 13 | GET | /api/users/me/usage | ✅ 200 | Token cost + 24h timeseries. |
| 14 | GET | /api/users/me/approvals | ✅ 200 | |
| 15 | PUT | /api/users/me/profile | ✅ 200 | Persists display_name + attributes. |
| 16 | PUT | /api/users/me/notification-prefs | ✅ 200 | Schema: `events: dict[str, list[str]]`, `channels: dict[str, str]`. |
| 17 | POST | /api/users/me/devices | ✅ 200 | Schema: `{platform, fcm_token, device_name, app_version}` — fields are `fcm_token` and `device_name` (not `push_token`/`name`). |
| 18 | POST | /api/users/me/password | ❌ 500 → **FIXED** | Handler called non-existent `AuthService.change_password`. Added the method on `LocalProvider` + `AuthService`. Requires daemon restart. Payload: `{current, new}`. |
| 19 | POST | /api/users/me/avatar | ✅ 200 | Multipart file upload. Returns `avatar_url`. |
| 20 | GET | /api/users/me/avatar/{filename} | ✅ 200 | Serves PNG with correct content-type + etag + cache headers. |
| 21 | DELETE | /api/users/me/devices/{id} | ✅ 200 (real) / 404 (bad id) | |
| 22 | POST | /api/users/me/inbox/read_all | ✅ 200 | |
| 23 | POST | /api/users/me/inbox/{id}/read | ✅ 404 | Correct on bad id. (Real-id path not reached — would need an inbox item first.) |
| 24 | DELETE | /api/users/me/inbox/{id} | ✅ 404 | Correct on bad id. |

### Bug fix summary (user/*)

**Bug: `POST /api/users/me/password` → 500 Internal Server Error**

Root cause was twofold:
1. `AuthService.change_password` wasn't implemented at all (the route called a method that didn't exist).
2. The route tried `from digitorn.core.auth.service import get_auth_service` — that function doesn't exist either; the ImportError fired *above* the route's `try/except` so the daemon returned the generic 500 from the unhandled-exception middleware.

Patched:
- Added `LocalProvider.change_password(user_id, current, new)` — verifies current, hashes new with bcrypt, persists via the ORM.
- Added `AuthService.change_password(...)` — delegates to the local provider, raises `RuntimeError` with an actionable message on failure.
- Route now reads `auth_service` from `request.app.state.auth_service` (same pattern as `api/auth.py`).

Verified end-to-end: change with correct current → 200, login with new password → OK, login with old → rejected, change with wrong current → 400 with "Current password is incorrect".

## config + discovery + requires + ui + transcribe (16 endpoints)

| # | Method | Path | Status | Notes |
|---|---|---|---|---|
| 25 | GET | /api/config | ✅ 200 | Full config tree. |
| 26 | GET | /api/config/browse[?path=] | ✅ 200 | Home dir by default, or any path. |
| 27 | PATCH | /api/config | 🔐 403 | Admin-only (expected). |
| 28 | GET | /api/discovery/modules | ✅ 200 | |
| 29 | GET | /api/discovery/modules/{id} | ✅ 200 | |
| 30 | GET | /api/discovery/templates | ✅ 200 | Real IDs: `01-scheduled-monitor`, `02-conversational-assistant`, etc. |
| 31 | GET | /api/discovery/templates/{id} | ✅ 200 | Returns full YAML + metadata. |
| 32 | GET | /api/discovery/triggers | ✅ 200 | |
| 33 | GET | /api/discovery/triggers/configured | ✅ 200 | |
| 34 | POST | /api/discovery/compile | ✅ 200 | Path is `/compile`, not `/compile_yaml` or `/compile-yaml`. Returns validation + graph. |
| 35 | POST | /api/discovery/prompt-preview | ✅ 200 | Path uses hyphen. Payload: `{content: "..."}`. |
| 36 | POST | /api/discovery/generate-package-manifest | ✅ 200 | Payload: `{yaml: "..."}`. |
| 37 | GET | /api/requires (or /api/requires/) | ✅ 200 | 307→200, or direct 200 without trailing slash. |
| 38 | GET | /api/requires/jobs | ✅ 200 | |
| 39 | GET | /api/ui/tool_display_defaults | ✅ 200 | ⚠️ Inconsistent: uses underscore, while `/notification-prefs`, `/prompt-preview`, `/generate-package-manifest` use hyphens. |
| 40 | GET | /api/transcribe/health | ✅ 200 | `ready: true` when local whisper model is loaded. |

## credentials/* (core CRUD)

| # | Method | Path | Status | Notes |
|---|---|---|---|---|
| 41 | GET | /api/credentials | ✅ 200 | User-scoped list. |
| 42 | GET | /api/credentials/providers | ✅ 200 | Returns built-in + custom provider catalog. |
| 43 | POST | /api/credentials | ✅ 200 | ⚠️ Body: `{name, provider_name, fields: {...}}` — NOT `{provider, data}`. Response masks the secret (e.g. `sk-...cdef`). |
| 44 | GET | /api/credentials/{id} | ✅ 200 | |
| 45 | PUT | /api/credentials/{id} | ✅ 200 | |
| 46 | DELETE | /api/credentials/{id} | ✅ 200 | |
| 47 | GET | /api/credentials-grants | ✅ 200 | Path uses hyphen. |
| 48 | GET | /api/credentials/{id}/grants | ✅ 200 | |
| 49 | GET | /api/users/me/credentials/{app}/{provider}/oauth/status | per-app endpoint | Requires valid app context. |
| 50 | GET | /api/users/me/credentials/{app}/{provider}/mcp/status | per-app endpoint | Same. |
| 51 | GET/POST | /api/admin/credentials[*] | 🔐 403 | Admin-only (expected). |

## apps/* (core app & session lifecycle)

| # | Method | Path | Status | Notes |
|---|---|---|---|---|
| 52 | GET | /api/apps | ✅ 200 | `data` is directly a list (not wrapped). |
| 53 | GET | /api/apps/{id} | ✅ 200 | |
| 54 | GET | /api/apps/{id}/required-secrets | ✅ 200 | |
| 55 | GET | /api/apps/{id}/sessions | ✅ 200 | |
| 56 | POST | /api/apps/{id}/sessions | ✅ 200 | `data.session_id`, greeting inline. |
| 57 | GET | /api/apps/{id}/sessions/{sid} | ✅ 200 | |
| 58 | GET | /api/apps/{id}/sessions/{sid}/history | ✅ 200 | |
| 59 | GET | /api/apps/{id}/sessions/{sid}/events | ✅ 200 | |
| 60 | GET | /api/apps/{id}/sessions/{sid}/workspace | ✅ 200 | |
| 61 | GET | /api/apps/{id}/sessions/{sid}/memory | ✅ 200 | |
| 62 | GET | /api/apps/{id}/sessions/{sid}/queue | ✅ 200 | |
| 63 | GET | /api/apps/{id}/sessions/{sid}/state | ✅ 200 | |
| 64 | GET | /api/apps/{id}/sessions/{sid}/preview | ✅ 200 | |
| 65 | GET | /api/apps/{id}/sessions/{sid}/context-breakdown | ✅ 200 | Path uses hyphen. |
| 66 | GET | /api/apps/{id}/approvals | ✅ 200 | App-level, NOT per-session. |
| 67 | POST | /api/apps/{id}/sessions/{sid}/messages | ✅ 200 | **End-to-end chat works**: turn runs, completes, history persists user + assistant. |
| 68 | POST | /api/apps/{id}/sessions/{sid}/compact | ✅ 200 | Returns `{before, after, freed, note}` — correctly no-ops on short sessions. |
| 69 | POST | /api/apps/{id}/sessions/{sid}/fork | ✅ 200 | ⚠️ Response field is `new_session_id`, NOT `session_id` like create endpoint — inconsistent. |
| 70 | POST | /api/apps/{id}/sessions/{sid}/abort | ✅ 200 | Idempotent even when no turn active. |
| 71 | DELETE | /api/apps/{id}/sessions/{sid} | ✅ 200 | |

### Crash during audit
Daemon crashed once silently around `context-breakdown`/`approvals` probing (process died, port 8000 closed, no trace in visible logs). Recovered after manual restart — no investigation yet since it hasn't recurred. If it happens again, capture the stdout of the daemon terminal.

## apps/* (workspace + mutations + deploy)

| # | Method | Path | Status | Notes |
|---|---|---|---|---|
| 72 | PUT | /workspace/files/{path} | ✅ 200 | Body: `{content, auto_approve, source}`. NOT raw text. |
| 73 | GET | /workspace/files/{path} | ✅ 200 | |
| 74 | GET | /workspace/files/{path}/history | ✅ 200 | |
| 75 | POST | /workspace/files/approve | ✅ 200 | |
| 76 | POST | /workspace/files/reject | ✅ 200 | |
| 77 | POST | /workspace/files/approve-hunks | ✅ 400 (no pending) | Correct behaviour. |
| 78 | POST | /workspace/git-status | ✅ 200 | |
| 79 | POST | /workspace/commit | ✅ 400 (not a git repo) | Correct. |
| 80 | GET | /workspace/code-snapshot | ✅ 200 | |
| 81 | DELETE | /workspace/files/{path} | ❌ 405 | **Missing route** — no `@router.delete` registered. UI can only reject non-approved, or agent uses the `workspace.delete` action. |
| 82 | GET | /deploy-status | ✅ 200 | Path is `deploy-status`, not `/deploy/status`. |
| 83 | GET | /diagnostics | ✅ 200 | Path is `/diagnostics`, NOT `app_diagnostics`. |
| 84 | GET | /errors | ✅ 200 | |
| 85 | GET | /status | ✅ 200 | |
| 86 | GET | /icon | ✅ 404 | Correct when app has no icon. |
| 87 | GET | /ui-config | ✅ 200 | |
| 88 | GET | /index | ✅ 200 | |
| 89 | GET | /files | ✅ 200 | |
| 90 | GET | /secrets | ✅ 200 | |
| 91 | GET | /check-update | ✅ 200 | |
| 92 | GET | /triggers | ✅ 200 | |
| 93 | GET | /watchers | ✅ 200 | |
| 94 | GET | /background-tasks | ✅ 200 | |
| 95 | GET | /background-sessions | ✅ 200 | |
| 96 | GET | /activations | ✅ 200 | |
| 97 | GET | /activations/stats | ✅ 200 | |
| 98 | GET | /channels/health | ✅ 200 | |
| 99 | GET | /tools/search?query=... | ✅ 200 | ⚠️ Param is `query`, NOT `q`. |
| 100 | GET | /tools/categories | ✅ 200 | |
| 101 | GET | /tools/categories/{cat} | ✅ 200 | |
| 102 | GET | /sessions/search?q=... | ✅ 200 | ⚠️ Param is `q` here, NOT `query`. Inconsistent with `/tools/search`. |
| 103 | GET | /sessions/{sid}/active-ops | ✅ 200 | |
| 104 | GET | /sessions/{sid}/export | ✅ 200 | Markdown export. |
| 105 | GET | /mcp/pending-oauth | ✅ 200 | |
| 106 | GET | /widgets | ✅ 200 | |
| 107 | POST | /sessions/{sid}/resume | ✅ 200 | |
| 108 | POST | /sessions/{sid}/undo | ⚠️ 200 | Body says `success: false` but HTTP is 200. Inconsistent — should be 4xx on error. |
| 109 | POST | /sessions/{sid}/queue/clear | ✅ 200 | |
| 110 | POST | /disable | ❌ 500 → **FIXED** | Raw SQL `SET disabled = 1` used integer literal against Postgres boolean column → `DatatypeMismatchError`. Switched all three occurrences in `manager.py` to parameterised boolean (`:d` with Python `True`/`False`). SQLite continued to work, only Postgres flagged the type mismatch. |
| 111 | POST | /enable | 🔐 403 | Admin-only (expected). |
| 112 | DELETE | /{app_id} | ✅ 200 | Graceful "nothing to delete" when app doesn't exist under user's scope. |
| 113 | POST | /deploy | ⚠️ 400 | Expects `{yaml_path: "..."}` (file on disk), not inline YAML. To deploy from text, use `/deploy/upload` (multipart) or `/install` routes. |
