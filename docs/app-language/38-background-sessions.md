# Background Sessions - Multi-User Multi-Session

## Overview

Background mode apps run autonomously - reacting to triggers (cron, file watch,
HTTP webhooks) instead of waiting for user input. Each user can have one or
multiple sessions, each with its own context, memory, and trigger routing.

## Session Modes

| Mode | Description |
|------|-------------|
| `mono` | 1 session per user (auto-created on first interaction) |
| `multi` | N sessions per user (created via API with custom params) |

```yaml
execution:
  mode: background
  session_mode: mono              # mono | multi
  max_sessions_per_user: 5        # limit for multi (0 = unlimited)
```
## Trigger Routing

Each trigger can route to different targets:

| Routing | Target | Use case |
|---------|--------|----------|
| `broadcast` | ALL active sessions | Cron jobs, global file watchers |
| `user` | All sessions of ONE user | Telegram message, email |
| `session` | ONE specific session | Webhook with session ID |

```yaml
triggers:
  - id: hourly-check
    type: cron
    schedule: "0 * * * *"
    routing: broadcast            # fires all sessions

  - id: user-message
    type: http
    path: /hooks/telegram
    routing: user                 # fires sessions of identified user
    routing_key: "{{event.header.X-User-Id}}"

  - id: direct-webhook
    type: http
    path: /hooks/session
    routing: session              # fires ONE specific session
    routing_key: "{{event.header.X-Session-Id}}"
```
## API Routes

### Background Sessions CRUD

| Method | Route | Description |
|--------|-------|-------------|
| POST | `/api/apps/{app_id}/background-sessions` | Create a session (mono: auto, multi: with params) |
| GET | `/api/apps/{app_id}/background-sessions` | List sessions (filtered by user via JWT) |
| GET | `/api/apps/{app_id}/background-sessions/{id}` | Get session detail (params, routing_keys, status) |
| POST | `/api/apps/{app_id}/background-sessions/{id}/pause` | Pause (triggers skip this session) |
| POST | `/api/apps/{app_id}/background-sessions/{id}/resume` | Resume |
| DELETE | `/api/apps/{app_id}/background-sessions/{id}` | Delete session |

### Create Session Request

```json
POST /api/apps/job-matcher/background-sessions
{
  "name": "Alice - Data Science",
  "params": {
    "cv": "Data scientist, 5 years experience, Python, ML, TensorFlow",
    "preferences": "remote, 60-80k EUR"
  },
  "routing_keys": {
    "telegram": "alice_chat_12345"
  },
  "workspace": "/home/alice/projects"
}
```

Response:
```json
{
  "success": true,
  "data": {
    "id": "ace2939dc435...",
    "app_id": "job-matcher",
    "user_id": "user_abc123",
    "name": "Alice - Data Science",
    "status": "active",
    "params": {"cv": "...", "preferences": "..."},
    "routing_keys": {"telegram": "alice_chat_12345"},
    "workspace": "/home/alice/projects",
    "created_at": "2026-04-06T12:00:00",
    "activation_count": 0
  }
}
```

### Session Payload (Pre-filled User Input)

A **session payload** is the user's pre-filled input - a prompt, structured
metadata, and uploaded files - that the daemon **replays into every scheduled
activation** as if the user had just typed it live.

This is what turns a generic cron job ("check job sites every hour") into a
personalised app ("check job sites every hour, looking for *Python remote roles
in Berlin paying 80k+*, using *this CV*"). Without payload, every user gets the
same activation; with payload, each user gets a fully personalised one.

#### Where the payload lives

| Data | Storage | Why |
|------|---------|-----|
| `prompt` (text) | DB column `BackgroundSession.params._payload.prompt` | Loaded once per tick |
| `metadata` (dict) | DB column `BackgroundSession.params._payload.metadata` | Loaded once per tick |
| File **bytes** | Disk: `~/.digitorn/apps/<app_id>/sessions/<sid>/payload/` | Read by the daemon at trigger time |
| File **metadata** (name/mime/size) | DB | Indexed without I/O |

The agent **never** runs `filesystem.read` on these files - the daemon reads
them at every tick, classifies them, and injects them directly into the user
message:

- **text-like** files (txt, md, json, yaml, csv, code) → inlined verbatim
  between `--- name ---` fences
- **images** (png, jpg, webp, gif) → base64 image content blocks (Anthropic
  native format)
- **PDFs** → base64 document blocks (Claude's native PDF support)
- **other binaries** → a short note "[skipped: name (mime, size)]"

This makes the system fully **self-contained even when the daemon is remote** -
the client uploads files via standard multipart HTTP, they live only on the
daemon's disk, and the agent only ever sees what the daemon injects.

#### Payload routes

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/apps/{app_id}/background-sessions/{sid}/payload` | Get the full payload + a `validation` block |
| PUT | `/api/apps/{app_id}/background-sessions/{sid}/payload` | Set `prompt` and/or `metadata` (shallow-merged) |
| POST | `/api/apps/{app_id}/background-sessions/{sid}/payload/files` | Multipart upload (max 25 MiB per file) |
| DELETE | `/api/apps/{app_id}/background-sessions/{sid}/payload/files/{name}` | Remove one file (disk + index) |
| DELETE | `/api/apps/{app_id}/background-sessions/{sid}/payload` | Wipe the entire payload |

#### Declarative payload schema

An app can **declare** the shape of its payload directly in the YAML. When
declared, the Flutter dashboard renders a typed form (instead of a generic
key/value editor) and the daemon **enforces validation** before letting any
trigger fire on the session.

```yaml
execution:
  mode: background
  payload_schema:
    required: true                    # block ticks if payload is invalid

    prompt:
      required: true
      label: "What should I look for?"
      placeholder: "Find me Python jobs paying 80k+"
      description: "Be specific - the agent reuses this every tick."
      min_length: 20
      max_length: 1000

    metadata:
      - name: location                # internal key (must be a valid identifier)
        label: "City"                  # shown in the form
        type: string                   # string | text | integer | number | boolean | select
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
      - name: cv                       # logical slot name
        label: "Your CV"
        required: true
        mime: [application/pdf]        # wildcards supported: image/*
        max_size_mb: 5                 # server hard cap is 25 MB
        max_count: 1
      - name: portfolio
        required: false
        mime: [application/pdf, image/*]
        max_count: 5
        max_size_mb: 10
```
**Field types for `metadata`:**

| `type` | Form widget | Notes |
|--------|-------------|-------|
| `string` | single-line input | |
| `text` | multi-line textarea | |
| `integer` | numeric input | `min` / `max` enforced |
| `number` | numeric input | float, `min` / `max` enforced |
| `boolean` | switch / checkbox | |
| `select` | dropdown | requires non-empty `options` |

**Validation behaviour:**

- `payload_schema.required: false` (or no schema) → free-form payload, the cron
  fires regardless. Backwards compatible with legacy apps.
- `payload_schema.required: true` → the daemon **skips** any tick whose session
  payload doesn't satisfy the schema (missing required prompt, missing required
  metadata field, missing required file slot). The skip is logged as a warning
  but does not raise - other sessions in the same broadcast keep running.

The validation status is also surfaced in `GET /payload`:

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

The frontend uses this to grey out the *Activate session* button until the
user has filled in everything.

#### Where the schema is exposed

The compiled schema is included in **every app summary** returned by the
listing/detail routes:

```http
GET /api/apps              → each app dict has "payload_schema": {...} | null
GET /api/apps/{id}         → same
GET /api/apps/{id}/payload-schema → just the schema (or null)
```

So a frontend can render the entire marketplace + every typed form with a
**single round-trip** to `GET /api/apps`.

### Triggers & Activations

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/apps/{app_id}/triggers` | List triggers with routing info |
| POST | `/api/apps/{app_id}/triggers/{trigger_id}/fire` | Manual fire (async) |
| POST | `/api/apps/{app_id}/triggers/{trigger_id}/test` | Sync test with custom payload |
| GET | `/api/apps/{app_id}/activations` | Activation history (paginated, filterable) |
| GET | `/api/apps/{app_id}/activations/stats` | Aggregated statistics |
| GET | `/api/apps/{app_id}/activations/{id}` | Full activation detail |
| GET | `/api/apps/{app_id}/errors` | Recent failed activations |

### Activation Response

```json
{
  "id": "2420cff2...",
  "app_id": "job-matcher",
  "trigger_id": "hourly-check",
  "trigger_type": "cron",
  "status": "completed",
  "session_id": "ace2939dc435",
  "user_id": "user_abc123",
  "message": "Hourly check triggered.",
  "started_at": "2026-04-06T12:00:00",
  "completed_at": "2026-04-06T12:00:02",
  "duration_ms": 1582.5,
  "response": "Found 3 new job postings matching your profile...",
  "tool_calls_count": 2,
  "turns_used": 1,
  "prompt_tokens": 1200,
  "completion_tokens": 150,
  "error": null
}
```

### Stats Response

```json
{
  "total": 42,
  "completed": 40,
  "failed": 2,
  "total_duration_ms": 65000.0,
  "avg_duration_ms": 1547.6,
  "total_prompt_tokens": 50400,
  "total_completion_tokens": 6300,
  "total_tool_calls": 84,
  "last_activation_at": "2026-04-06T12:00:00",
  "success_rate": 95.2
}
```

## Complete YAML Example

```yaml
app:
  app_id: job-matcher
  name: "Job Matcher"
  version: "1.0"

modules:
  web: {}
  memory: {}

agents:
  - id: main
    role: worker
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
      max_tokens: 2048
    system_prompt: |
      You are a job matching agent. You have access to the user's CV
      and preferences in your session params. Search for matching jobs
      and report findings.

execution:
  mode: background
  session_mode: multi
  max_sessions_per_user: 10
  max_turns: 20
  timeout: 120

  triggers:
    - id: hourly-search
      type: cron
      schedule: "0 * * * *"
      routing: broadcast
      message: "Search for new job postings matching the user's profile."

    - id: user-command
      type: http
      path: /hooks/command
      port: 9100
      routing: user
      routing_key: "{{event.header.X-User-Id}}"
      message: "User command: {{event.body}}"

  # Declarative payload - the dashboard renders a typed form and the
  # daemon refuses to fire ticks until each user has filled it in.
  payload_schema:
    required: true
    prompt:
      required: true
      label: "What kind of job are you looking for?"
      placeholder: "Senior Python engineer, remote, ML-focused"
      min_length: 20
    metadata:
      - name: location
        label: "City"
        type: string
        required: true
      - name: min_salary
        type: integer
        default: 60000
        min: 0
      - name: remote_only
        type: boolean
        default: true
      - name: contract_type
        type: select
        options: [full_time, part_time, contract]
        default: full_time
    files:
      - name: cv
        label: "Your CV"
        required: true
        mime: [application/pdf]
        max_size_mb: 5

capabilities:
  default_policy: auto
  grant:
    - module: web
    - module: memory
```
With this YAML in place, the activation flow becomes:

1. User creates a session via `POST /background-sessions`
2. User uploads their CV via `POST /payload/files` and fills the form via
   `PUT /payload`
3. The cron schedule kicks off every hour. For each active session the daemon:
   - resolves the routing → list of target sessions
   - validates `_payload` against `payload_schema` (skip if invalid)
   - reads `cv.pdf` from disk, encodes it as a base64 document block
   - builds the user message: trigger context + prompt + metadata table + the
     PDF as a content block
   - calls `agent_turn` with the multimodal message - the agent sees the CV
     content directly, never runs `filesystem.read` on it
4. Each activation row + its events show up in the dashboard timeline
