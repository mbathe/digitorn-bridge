---
id: background-sessions
---

# Background Sessions

Background apps don't wait for a user to type - they react to
**triggers** (cron, file watch, HTTP webhook). A background app
serves many users simultaneously through **background sessions**:
DB rows that hold each user's pre-filled input (prompt, typed
metadata, uploaded files), the routing keys their triggers should
match, and the activation history.

This page documents what the daemon actually stores, how it routes
activations, and how a the chat client / web client wires the user-facing
form. Every behaviour and field maps to real code; entries are
cited with file + line.

## Session modes

`runtime.session_mode`.

| Mode | Behaviour |
|------|-----------|
| `mono` (default) | Each user has **exactly one** background session per app. Auto-created on first interaction (→ `store.get_or_create_mono`). |
| `multi` | Each user can create up to `max_sessions_per_user` (, default 10, 0 = unlimited) sessions, each with distinct params, routing keys, workspace. Created via `POST /background-sessions`. |

```yaml
runtime:
  mode: background
  session_mode: multi
  max_sessions_per_user: 5
  max_concurrent_activations: 20   # cap on parallel LLM calls per broadcast
```

`max_concurrent_activations` gates the asyncio
semaphore - the daemon won't fire more than
N activations at once even when a broadcast trigger resolves to
thousands of sessions, preventing rate-limit storms.

## Trigger routing

Each trigger declares **how** it dispatches to sessions
(). Three modes:

| `routing` | Target | `routing_key` template purpose |
|-----------|--------|--------------------------------|
| `broadcast` (default) | All `active` sessions for the app. | Ignored. Used for cron jobs that should fire for every user. |
| `user` | All sessions of the identified user. | Identifies which user, e.g. `"{{event.header.X-User-Id}}"` or `"{{event.chat_id}}"`. |
| `session` | Exactly one session matching the key. | Identifies the session id directly, e.g. `"{{event.header.X-Session-Id}}"`. |

```yaml
runtime:
  mode: background
  triggers:
    - id: hourly-check
      type: cron
      schedule: "0 * * * *"
      routing: broadcast              # fires every active session

    - id: telegram-message
      type: http
      path: /hooks/telegram
      port: 9100
      routing: user
      routing_key: "{{event.header.X-User-Id}}"
      message: "User said: {{event.body}}"

    - id: direct-webhook
      type: http
      path: /hooks/session
      routing: session
      routing_key: "{{event.header.X-Session-Id}}"
      message: "Direct hit: {{event.body}}"
```

Routing resolution lives in `BackgroundSessionStore.resolve_routing`
():

- `broadcast` → SELECT every `status='active'` session for the app.
- `user` → first try `user_id == routing_key_value`; if no rows,
  scan every active session and match against the JSON
  `routing_keys` dict (so a user can register a Telegram chat id
  under `routing_keys.telegram` and still receive activations).
- `session` → match against either the session row's id or any
  value inside its `routing_keys` dict.

### Available `{{event.*}}` placeholders

The HTTP loop resolves these
placeholders inside both `message` and `routing_key`:

| Placeholder |
|-------------|
| `{{event.body}}` |
| `{{event.path}}` |
| `{{event.method}}` |
| `{{event.header.X-User-Id}}` |
| `{{event.query.<name>}}` |
| `{{event.path}}` (watch) |

For watch triggers, only `{{event.path}}` is meaningful
().

### Trigger types

`TriggerConfig.type` accepts `cron`, `watch`,
`http`. The legacy background runtime starts one of three
asyncio loops per trigger:

| Type | Loop | Notes |
|------|------|-------|
| `cron` | `_cron_loop:1252` | Schedule parsed by `croniter`; sleeps until next tick. |
| `watch` | `_watch_loop:1300` | Polling glob over `paths` (default 5 s, `runtime_config.watch_poll_interval`). Up to 10 000 paths remembered before LRU-style eviction. |
| `http` | `_http_loop:1356` | Prefers `aiohttp`; falls back to a raw asyncio TCP server (`_http_basic_loop:1461`) when aiohttp isn't installed. The basic mode can't extract headers, so `routing_key` resolves to `""` there. |

> **Channels module overrides this.** When `tools.modules.channels`
> is loaded, `run_background` hands
> dispatch over to that module entirely. The legacy loops are only
> used in apps that don't load `channels`. Most production webhook
> setups (HMAC auth, retries, dead-letter) use channels.

## Per-trigger circuit breaker

`_TriggerCircuitBreaker`. Pauses a trigger
after consecutive failures with exponential backoff (5 min → 10 →
20 → 40 → 60 min cap). Trip thresholds depend on error category:

| Category | Detection (substring) | Failures before trip |
|----------|----------------------|----------------------|
| **Fatal** | `402`, `insufficient`, `balance`, `billing`, `quota`, `401`, `unauthorized`, `invalid api key`, `forbidden`, `permission denied`, `not authorized` | 2 |
| **Transient** | `timeout`, `timed out`, `connection`, `network`, `temporarily`, `rate limit`, `429`, `503`, `502`, `504`, `unavailable` | 5 |
| **Unknown** | Anything else (code crash, schema violation, ...) | 3 |

Resets on the first success (`record_success:612`). Useful when a
remote API goes down - the trigger naturally backs off instead
of hammering it.

## Session payload

Background apps almost always need **per-session user input** -
the CV that the job-matcher should re-read every hour, the
filtering preferences for the news-summariser, the project URL
the deploy-watcher should poll. That data lives in the session
**payload**.

### Storage layout

| Data | Storage | Loaded by |
|------|---------|-----------|
| `prompt` (text) | DB column `BackgroundSession.params._payload.prompt` | Daemon at every tick. |
| `metadata` (dict) | DB column `BackgroundSession.params._payload.metadata` | Daemon at every tick. |
| File **bytes** | Disk: `~/.digitorn/apps/<app_id>/sessions/<sid>/payload/<safe_name>`. | Daemon at every tick (file size cap **10 MiB**,). |
| File **metadata** (`{name, path, mime_type, size_bytes}`) | DB inside `_payload.files[]` | Indexed without I/O. |

The agent **never** runs `filesystem.read` on payload files. The
daemon reads them at activation time (`_build_payload_message_content`,
), classifies them by MIME, and injects them
straight into the user message:

| MIME | Injected as |
|------|-------------|
| `text/*`, `application/json`, `yaml`, `toml`, `xml`, `javascript`, `python`, `sh` (or any UTF-8 decodable file) | Inlined verbatim between `--- name ---` ... `--- end name ---` fences. |
| `image/*` | Anthropic-native `{type: image, source: {type: base64, media_type, data}}` content block. |
| `application/pdf` | Anthropic-native `{type: document, source: {type: base64, media_type: application/pdf, data}}` content block (Claude's native PDF support). |
| Other binary | A note `[skipped: name (mime, size) not inlined]` so the agent knows the file exists but couldn't be embedded. |
| Files larger than 10 MiB | A note `[name: too large (NNN bytes, cap 10485760)]`. |

### Payload surface

The daemon exposes a payload surface for each background
session that lets clients:

- Read the current `{prompt, metadata, files, validation}`
  payload.
- Set `prompt` and / or `metadata` (shallow-merged).
- Upload files via multipart (server hard cap 25 MiB).
- Remove one file (disk + index).

Public clients use the SDK; the route shapes are not
documented publicly.
| `DELETE` | `/payload` | | Wipes the entire payload. |

## Declarative payload schema

`runtime.payload_schema` (, type
`PayloadSchemaConfig` at `extra: forbid`). Lets an app
declare the shape of its payload so the dashboard can render a
typed form, and so the daemon can refuse to fire activations on a
session that doesn't satisfy the schema.

```yaml
runtime:
  mode: background
  payload_schema:
    required: true                  # daemon enforces validation
    prompt:
      required: true
      label: What should I look for?
      placeholder: Find me Python jobs paying 80k+
      description: Be specific - the agent reuses this every tick.
      min_length: 20
      max_length: 1000
    metadata:
      - name: location
        label: City
        type: string
        required: true
      - name: min_salary
        type: integer
        default: 60000
        min: 0
        max: 500000
      - name: remote_only
        type: boolean
        default: true
      - name: contract_type
        type: select
        options: [full_time, part_time, contract]
        default: full_time
    files:
      - name: cv
        label: Your CV
        required: true
        mime: [application/pdf]
        max_size_mb: 5
        max_count: 1
      - name: portfolio
        required: false
        mime: [application/pdf, "image/*"]
        max_count: 5
        max_size_mb: 10
```

### Field types for `metadata`

`PayloadFieldConfig.type` accepts six values:

| `type` | Form widget | Notes |
|--------|-------------|-------|
| `string` | Single-line input. | |
| `text` | Multi-line textarea. | |
| `integer` | Numeric input, integer only. | `min` / `max` (floats accepted) bound the value. |
| `number` | Numeric input, float allowed. | Same `min` / `max` bounds. |
| `boolean` | Switch / checkbox. | |
| `select` | Dropdown. | Requires non-empty `options`. |

Other recognised keys: `label`, `required`, `default`,
`description`, `placeholder`.

### File rules

`PayloadFileRuleConfig`:

| Field | Default | Description |
|-------|---------|-------------|
| `name` | required | Logical slot id (`cv`, `portfolio`, ...). |
| `label` | `""` | Human label. |
| `description` | `""` | Help text shown next to the upload zone. |
| `required` | `false` | Whether at least one matching file is mandatory. |
| `mime` | `[]` (any) | Accepted MIME types. Supports wildcards (`image/*`). |
| `max_size_mb` | `25.0` | Per-file size cap. Server hard cap is 25 MiB. |
| `max_count` | `1` | Max number of files for this slot. |

### Validation behaviour

- `payload_schema.required: false` (or no schema) → the daemon
  fires every tick regardless. Backwards compatible with legacy
  apps.
- `payload_schema.required: true` → before each activation the
  daemon calls `_validate_payload_against_schema` and **skips**
  the session if any errors come back.
  The skip is logged as a warning; other sessions in the same
  broadcast are unaffected.

`GET /payload` includes a `validation` block:

```json
{
  "prompt": "...",
  "metadata": { ... },
  "files": [ ... ],
  "validation": {
    "schema_required": true,
    "valid": false,
    "errors": [
      "payload.metadata.location is required",
      "payload.files: missing required 'cv'"
    ]
  }
}
```

The the chat client / web client uses this to grey out the *Activate
session* button until the form is complete.

### Where the schema is exposed

The compiled schema is included in every app-summary the
daemon returns. A client listing the apps gets each app's
`payload_schema: {...} | null` in the same response - so a
frontend can render the entire marketplace + every typed
form in a single round-trip.

## Session lifecycle

The daemon exposes a session surface that lets clients:

- List sessions filtered by JWT identity.
- Create a session (mono: get-or-create, multi: enforces
  `max_sessions_per_user`).
- Get one session.
- Pause / resume - paused sessions skip incoming triggers.
- Delete. Wipes payload files first (best-effort) so no
  orphan bytes.

### Create session payload

```jsonc
{
  "name": "Alice - Data Science",
  "params": { "tier": "premium" },
  "routing_keys": { "telegram": "alice_chat_12345" },
  "workspace": "/home/alice/projects"
}
```

The `user_id` is read from the JWT. `params` is opaque;
reserved key `_payload` is managed by the payload surface
above.

## Activations

Each trigger fire creates an `Activation` row. The daemon
exposes an activations surface for:

- Listing triggers with routing info.
- Manual fire (async).
- Sync test with custom payload.
- Paginated history (filterable by trigger / status).
- Aggregated stats (count, success_rate, avg_duration, total
  tokens).
| `GET` | `/activations/{id}` | | Full activation detail + event timeline. |
| `GET` | `/errors` | | Recent failed activations. |

### Activation event timeline

For every activation, an `_ActivationEventRecorder`
() wraps the agent's callbacks and writes
each tool call, thinking block, channel send, and artifact into
the `activation_events` table with a monotonically increasing
sequence number. The dashboard drawer renders this as a
step-by-step trace.

Tool calls that produce files (`_FILE_WRITE_ACTIONS` at
: `filesystem.write/edit/create`,
`notebook.*`, `spreadsheet.*`, `pdf.*`, `presentation.create`)
are also written as a separate `artifact` event, so the UI's
*Artifacts* tab doesn't have to re-parse tool params.

### Failure surfacing

When an activation fails (agent_turn raised, or the result has a
non-empty `error` other than `"aborted"`), the daemon
():

1. Classifies the error via `_classify_error` (same path as
   foreground turns).
2. If the activation has a `session_id`, emits a session-bus
   `error` event so any open client sees a live banner.
3. Always writes a `BG_ACTIVATION_FAILED` inbox entry for
   sessionless / global activations, so the user gets a row in
   their notification bell either way.

## Complete example

```yaml
app:
  app_id: job-matcher
  name: Job Matcher
  version: "1.0"

runtime:
  mode: background
  session_mode: multi
  max_sessions_per_user: 10
  max_turns: 20
  timeout: 120
  max_concurrent_activations: 20

  triggers:
    - id: hourly-search
      type: cron
      schedule: "0 * * * *"
      routing: broadcast
      message: Search for new job postings matching the user's profile.

    - id: user-command
      type: http
      path: /hooks/command
      port: 9100
      routing: user
      routing_key: "{{event.header.X-User-Id}}"
      message: "User command: {{event.body}}"

  payload_schema:
    required: true
    prompt:
      required: true
      label: What kind of job are you looking for?
      placeholder: Senior Python engineer, remote, ML-focused
      min_length: 20
    metadata:
      - {name: location,      type: string,  required: true,  label: City}
      - {name: min_salary,    type: integer, default: 60000, min: 0}
      - {name: remote_only,   type: boolean, default: true}
      - {name: contract_type, type: select,
         options: [full_time, part_time, contract], default: full_time}
    files:
      - name: cv
        label: Your CV
        required: true
        mime: [application/pdf]
        max_size_mb: 5

agents:
  - id: main
    role: worker
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: https://api.deepseek.com/v1
      max_tokens: 2048
    system_prompt: |
      You are a job matching agent. The user's CV and preferences
      are injected into every activation. Search for matching
      jobs and report findings concisely.

tools:
  modules:
    web: {}
    memory: {}
  capabilities:
    default_policy: auto
    grant:
      - {module: web}
      - {module: memory}
```

End-to-end flow:

1. User creates a session via `POST /background-sessions` (or
   uses the auto-created mono session).
2. User uploads CV via `POST /payload/files` and fills the form
   via `PUT /payload`.
3. The cron schedule kicks off every hour. For each active
   session the daemon:
   - Resolves routing → list of target sessions
     (`bg_store.resolve_routing`).
   - Validates `_payload` against `payload_schema`; skips on
     errors.
   - Reads `cv.pdf` from disk, encodes it as a base64 document
     block.
   - Builds the user message: trigger context + prompt +
     metadata table + the PDF as a content block.
   - Calls `agent_turn` with that multimodal message - the agent
     sees the CV directly, never runs `filesystem.read` on it.
4. Each activation row + its events show up in the dashboard
   timeline.

## Cross-references

- Trigger types and routing modes from the agent author's view:
  [Triggers](09-triggers.md)
- App-config block reference (`runtime` block + every field):
  [App Configuration → runtime](02-app-config.md#runtime---lifecycle-and-execution-policy)
- Per-user / per-app credential resolution at activation time:
  [credentials.md](../reference/runtime/credentials.md)
- Channels module (production webhook handler that supersedes
  the legacy HTTP loop): [Channels](40-channels.md)
- Multi-tenant install scopes (system vs user, where `user_id`
  comes from): [Multi-Tenant App Installs](45-multi-tenant.md)
