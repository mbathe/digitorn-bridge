# Digitorn API - full collection

260 requests auto-generated from `app.openapi()`. Regenerate with:
```
py -3.12 tools/export_openapi.py && py -3.12 tools/export_bruno.py
```

## Run order (lower seq = earlier)

Folders are prioritised so tokens + app_id + session_id are captured before anything that depends on them:

| seq | folder | notes |
|-----|--------|-------|
| 10  | auth   | login first → captures access_token, refresh_token, user_id |
| 20  | user   |       |
| 30  | discovery | read-only |
| 40  | modules   | read-only |
| 50  | credentials | configure API keys |
| 60  | mcp   |       |
| 70  | packages |    |
| 80  | apps  | deploy → create_session → send_message (captures app_id, session_id) |
| 90  | builder |     |
| 100+ | oauth, config, ui, transcribe, requires, untagged | |
| 900 | security | grants/revokes - near the end |
| 910 | admin  | destructive - run last |

## State-driven 404s on first Run all

`auth/session_history`, `auth/fork_session`, `auth/delete_session` all read from the `UserSession` table which is **populated lazily** (persistence.py:57): the row only gets written when a conversation session persists its first message. On a clean daemon:

  1. Auth runs at seq 10 - UserSession table empty → **404**
  2. Apps runs at seq 80 - session_send_message populates a row
  3. Second Run all - the 404s disappear

Same logic applies to routes that need a `{{request_id}}` (approval resolve), `{{credential_id}}`, `{{draft_id}}`, `{{task_id}}`, `{{mcp_server_id}}` - those IDs don't exist in a fresh database. Create the resource manually with its POST route first, then the GET/DELETE/PUT variants succeed.

## Variables filled automatically

| Variable | Captured by |
|----------|-------------|
| `access_token`, `refresh_token`, `user_id` | `auth/login` and `auth/register` |
| `access_token`, `refresh_token` | `auth/refresh` |
| `app_id` | `apps/deploy_app` |
| `session_id` | `apps/create_session` |
| `last_correlation_id` | `apps/session_send_message` |

## Variables you fill in `environments/Local.bru`

- `email`, `username`, `password` - your real credentials
- `yaml_path` - absolute path to the YAML you want to deploy
- `message` - text sent to the agent

