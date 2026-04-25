---
id: api-integration
---

# API Integration

Digitorn exposes a REST API for managing deployed apps, running conversations, discovering tools, and handling background tasks. The API serves 80+ endpoints across app management, chat, tool discovery, health, metrics, MCP management, and module APIs.

## Daemon Lifecycle

The API requires the Digitorn daemon to be running.

```bash
# Start the daemon
digitorn start

# Start with multiple workers
digitorn start --workers 4

# Stop the daemon
digitorn stop
```

All API responses are wrapped in a standard envelope:

```json
{
  "success": true,
  "data": { ... },
  "error": null
}
```

On failure, `success` is `false` and `error` contains a message.

---

## App Lifecycle

### Deploy from YAML Path

```
POST /api/apps/deploy
```

```bash
curl -X POST http://localhost:8000/api/apps/deploy \
  -H "Content-Type: application/json" \
  -d '{"yaml_path": "/path/to/my-app.yaml"}'
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `yaml_path` | string or null | *required* | Absolute path to the YAML app file |
| `force` | bool | `false` | Redeploy even if already deployed |

**Response** (200):
```json
{
  "success": true,
  "data": {
    "app_id": "my-app",
    "name": "My App",
    "version": "1.0",
    "mode": "conversation",
    "agents": ["assistant"],
    "modules": ["filesystem", "database"],
    "total_tools": 12,
    "total_categories": 2,
    "deployed_at": 1710300000.0
  }
}
```

### Deploy from File Upload

```
POST /api/apps/deploy/upload
```

```bash
curl -X POST http://localhost:8000/api/apps/deploy/upload \
  -F "file=@my-app.yaml"
```

Accepts the YAML file as multipart form data.

### List Deployed Apps

```
GET /api/apps/
```

```bash
curl http://localhost:8000/api/apps/
```

Returns a list of `AppSummary` objects.

### Get App Details

```
GET /api/apps/{app_id}
```

```bash
curl http://localhost:8000/api/apps/my-app
```

### One-Shot Execution

```
POST /api/apps/{app_id}/run
```

```bash
curl -X POST http://localhost:8000/api/apps/my-app/run \
  -H "Content-Type: application/json" \
  -d '{"input": "List all Python files in the workspace"}'
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `input` | string | *required* | The user input to process |
| `input_type` | string | `"text"` | Input format hint |

### Undeploy

```
DELETE /api/apps/{app_id}
```

```bash
curl -X DELETE http://localhost:8000/api/apps/my-app
```

Stops all agents, cancels pending approvals, and removes the app from the daemon.

---

## Chat

### Synchronous Conversation Turn

```
POST /api/apps/{app_id}/chat
```

```bash
curl -X POST http://localhost:8000/api/apps/my-app/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "sess-001", "message": "What files are in the workspace?"}'
```

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | Session identifier (created on first use) |
| `message` | string | User message |

Returns the full agent response when the turn completes.

### Streaming Conversation Turn (Socket.IO)

```
POST /api/apps/{app_id}/chat/stream
```

```bash
curl -N -X POST http://localhost:8000/api/apps/my-app/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"session_id": "sess-001", "message": "Analyze the project structure"}'
```

Returns a Socket.IO stream. Event types:

| Event | Description |
|-------|-------------|
| `connected` | Stream established |
| `tool_call` | Agent is calling a tool (name, params) |
| `result` | Tool execution result |
| `error` | Tool or agent error |
| `hook` | Runtime hook fired (e.g., context compaction) |
| `approval_request` | A tool call requires user approval |
| `notification` | Background task notification |
| `notification_result` | Background task completed |

Example Socket.IO event output:

```
event: connected
data: {"session_id": "sess-001"}

event: tool_call
data: {"name": "filesystem.ls", "params": {"path": "."}}

event: result
data: {"success": true, "data": ["src/", "tests/", "README.md"]}

event: tool_call
data: {"name": "filesystem.read", "params": {"path": "README.md"}}

event: result
data: {"success": true, "data": "# My Project\n..."}
```

The final response is the last `data` payload before the stream closes.

---

## Sessions

Sessions persist conversation history across multiple turns. They are scoped per app and expire after 1 hour of inactivity by default.

### List Sessions

```
GET /api/apps/{app_id}/sessions
```

```bash
curl http://localhost:8000/api/apps/my-app/sessions
```

### Get Session Metadata

```
GET /api/apps/{app_id}/sessions/{session_id}
```

```bash
curl http://localhost:8000/api/apps/my-app/sessions/sess-001
```

### Get Full Message History

```
GET /api/apps/{app_id}/sessions/{session_id}/history
```

```bash
curl http://localhost:8000/api/apps/my-app/sessions/sess-001/history
```

### Delete Session

```
DELETE /api/apps/{app_id}/sessions/{session_id}
```

```bash
curl -X DELETE http://localhost:8000/api/apps/my-app/sessions/sess-001
```

### Persistent Socket.IO Event Stream

```
GET /api/apps/{app_id}/sessions/{session_id}/events
```

```bash
curl -N http://localhost:8000/api/apps/my-app/sessions/sess-001/events
```

This is the **primary SDK endpoint**. The connection stays open for the session lifetime and auto-pushes all events: tool calls, results, errors, background task notifications, and approval requests.

A client should open this stream once and keep it open, then send messages via the async message endpoint below.

### Send Message (Async)

```
POST /api/apps/{app_id}/sessions/{session_id}/messages
```

```bash
curl -X POST http://localhost:8000/api/apps/my-app/sessions/sess-001/messages \
  -H "Content-Type: application/json" \
  -d '{"message": "Read the config file"}'
```

| Field | Type | Description |
|-------|------|-------------|
| `message` | string | User message |

Returns **202 Accepted** immediately. The agent processes the message asynchronously and pushes the response through the persistent Socket.IO stream (`/sessions/{sid}/events`).

---

## Background Tasks

Background tasks let you launch long-running tool executions without blocking the conversation.

### Launch Task

```
POST /api/apps/{app_id}/background-tasks
```

```bash
curl -X POST http://localhost:8000/api/apps/my-app/background-tasks \
  -H "Content-Type: application/json" \
  -d '{"tool": "filesystem.find", "params": {"pattern": "*.py", "path": "."}}'
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tool` | string | *required* | Tool name in `module.action` format |
| `params` | dict | `{}` | Tool parameters |

### List Tasks

```
GET /api/apps/{app_id}/background-tasks
```

```bash
curl http://localhost:8000/api/apps/my-app/background-tasks
```

### Get Task Status

```
GET /api/apps/{app_id}/background-tasks/{task_id}
```

```bash
curl http://localhost:8000/api/apps/my-app/background-tasks/task-123
```

### Cancel Task

```
DELETE /api/apps/{app_id}/background-tasks/{task_id}
```

```bash
curl -X DELETE http://localhost:8000/api/apps/my-app/background-tasks/task-123
```

### Wait for Task

```
POST /api/apps/{app_id}/background-tasks/{task_id}/wait
```

```bash
curl -X POST http://localhost:8000/api/apps/my-app/background-tasks/task-123/wait \
  -H "Content-Type: application/json" \
  -d '{"timeout": 30.0}'
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `timeout` | float | `60.0` | Max seconds to wait before returning |

Blocks until the task completes or the timeout is reached.

---

## Tool Discovery

These endpoints expose the same tool discovery capabilities that agents use internally via meta-tools.

### Search Tools

```
GET /api/apps/{app_id}/tools/search?query=read+file
```

```bash
curl "http://localhost:8000/api/apps/my-app/tools/search?query=read+file"
```

Uses hybrid semantic + keyword search. Returns ranked tool matches.

### List Categories

```
GET /api/apps/{app_id}/tools/categories
```

```bash
curl http://localhost:8000/api/apps/my-app/tools/categories
```

### Browse Category (Paginated)

```
GET /api/apps/{app_id}/tools/categories/{category}
```

```bash
curl http://localhost:8000/api/apps/my-app/tools/categories/filesystem
```

### Get Tool Schema

```
GET /api/apps/{app_id}/tools/{tool_name}
```

```bash
curl http://localhost:8000/api/apps/my-app/tools/filesystem.read
```

Returns the full JSON schema, description, parameter details, side effects, and aliases.

### Execute Tool Directly

```
POST /api/apps/{app_id}/tools/{tool_name}/execute
```

```bash
curl -X POST http://localhost:8000/api/apps/my-app/tools/filesystem.read/execute \
  -H "Content-Type: application/json" \
  -d '{"params": {"path": "/home/user/project/README.md"}}'
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `params` | dict | `{}` | Tool parameters |

Security policies (grant/approve/deny from the app's `capabilities:` block) still apply to direct tool execution.

---

## Index

### Get Full Tool Index

```
GET /api/apps/{app_id}/index
```

```bash
curl http://localhost:8000/api/apps/my-app/index
```

Returns the complete tool index structure: all categories, tools, schemas, and metadata.

---

## Notifications

Notifications are generated by background tasks that complete while the agent is in a conversation turn.

### Check for Active Notifications

```
GET /api/apps/{app_id}/notifications/active
```

```bash
curl http://localhost:8000/api/apps/my-app/notifications/active
```

Returns `{ "active": true }` or `{ "active": false }`. This is a lightweight poll endpoint for clients that need a quick check.

### Drain Notifications

```
POST /api/apps/{app_id}/notifications
```

```bash
curl -N -X POST http://localhost:8000/api/apps/my-app/notifications
```

Drains all pending notifications and triggers an agent turn to process them. Returns results as an Socket.IO stream.

---

## Approvals

When an app defines a `capabilities:` block with `approve:` rules, certain tool calls require explicit user approval before execution.

### List Pending Approvals

```
GET /api/apps/{app_id}/approvals
```

```bash
curl http://localhost:8000/api/apps/my-app/approvals
```

### Resolve an Approval

```
POST /api/apps/{app_id}/approve
```

```bash
curl -X POST http://localhost:8000/api/apps/my-app/approve \
  -H "Content-Type: application/json" \
  -d '{"request_id": "req-abc", "approved": true, "message": ""}'
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `request_id` | string | *required* | The approval request ID |
| `approved` | bool | *required* | `true` to approve, `false` to deny |
| `message` | string | `""` | Optional message for the agent |

Pending approvals time out after 5 minutes and are auto-denied.

When using the streaming chat endpoint (`/chat/stream`), approval requests are pushed as `approval_request` Socket.IO events. The client should display the request to the user and POST the decision back to this endpoint.

---

## Rate Limiting

Per-app rate limiting uses a sliding window counter. The default limit is 60 requests per minute, applied to `/chat`, `/run`, and `/chat/stream` endpoints.

### Get Current Usage

```
GET /api/apps/{app_id}/quota
```

```bash
curl http://localhost:8000/api/apps/my-app/quota
```

### Set Custom Limit

```
PUT /api/apps/{app_id}/quota
```

```bash
curl -X PUT http://localhost:8000/api/apps/my-app/quota \
  -H "Content-Type: application/json" \
  -d '{"rpm": 120}'
```

| Field | Type | Description |
|-------|------|-------------|
| `rpm` | int | Requests per minute |

### Reset to Default

```
DELETE /api/apps/{app_id}/quota
```

```bash
curl -X DELETE http://localhost:8000/api/apps/my-app/quota
```

---

## Secrets

Per-app encrypted secret storage. Secrets are stored with Fernet encryption and never returned in plaintext by the API.

### List secret keys

```
GET /api/apps/{app_id}/secrets
```

```bash
curl http://localhost:8000/api/apps/my-app/secrets
```

**Response** (200):
```json
{
  "success": true,
  "data": { "app_id": "my-app", "keys": ["API_KEY", "CLIENT_SECRET"] }
}
```

### Check if a secret exists

```
GET /api/apps/{app_id}/secrets/{key}
```

```bash
curl http://localhost:8000/api/apps/my-app/secrets/API_KEY
```

**Response** (200):
```json
{
  "success": true,
  "data": { "app_id": "my-app", "key": "API_KEY", "exists": true }
}
```

### Set a secret

```
PUT /api/apps/{app_id}/secrets/{key}
```

```bash
curl -X PUT http://localhost:8000/api/apps/my-app/secrets/API_KEY \
  -H "Content-Type: application/json" \
  -d '{"value": "sk-live-abc123"}'
```

### Delete a secret

```
DELETE /api/apps/{app_id}/secrets/{key}
```

```bash
curl -X DELETE http://localhost:8000/api/apps/my-app/secrets/API_KEY
```

### CLI commands

```bash
digitorn secret set my-app API_KEY "sk-live-abc123"
digitorn secret set my-app API_KEY         # prompts for value (hidden)
digitorn secret list my-app
digitorn secret delete my-app API_KEY
```

---

## SDK Integration Pattern

The recommended flow for building a client application on top of the Digitorn API:

### 1. Deploy the App

```bash
curl -X POST http://localhost:8000/api/apps/deploy \
  -d '{"yaml_path": "/path/to/app.yaml"}'
```

### 2. Open a Persistent Socket.IO Connection

```bash
curl -N http://localhost:8000/api/apps/my-app/sessions/sess-001/events
```

Keep this connection open for the session lifetime. All events (tool calls, results, errors, approvals, background notifications) arrive here.

### 3. Send Messages Asynchronously

```bash
curl -X POST http://localhost:8000/api/apps/my-app/sessions/sess-001/messages \
  -d '{"message": "Analyze the project"}'
# Returns 202 immediately
```

The response arrives via the Socket.IO stream opened in step 2.

### 4. Handle Approvals

When an `approval_request` event arrives on the Socket.IO stream, display it to the user and resolve it:

```bash
curl -X POST http://localhost:8000/api/apps/my-app/approve \
  -d '{"request_id": "req-abc", "approved": true}'
```

### 5. Launch Background Tasks

```bash
curl -X POST http://localhost:8000/api/apps/my-app/background-tasks \
  -d '{"tool": "database.query", "params": {"sql": "SELECT count(*) FROM users"}}'
```

Background task completions are pushed as `notification` events on the persistent Socket.IO stream.

### 6. Poll or Wait for Tasks

For clients that do not use the persistent Socket.IO stream:

```bash
# Quick check
curl http://localhost:8000/api/apps/my-app/notifications/active

# Block until task completes
curl -X POST http://localhost:8000/api/apps/my-app/background-tasks/task-123/wait \
  -d '{"timeout": 30.0}'
```

### 7. Clean Up

```bash
# Delete session
curl -X DELETE http://localhost:8000/api/apps/my-app/sessions/sess-001

# Undeploy app
curl -X DELETE http://localhost:8000/api/apps/my-app
```

### Summary: Endpoint Reference

| # | Method | Endpoint | Description |
|---|--------|----------|-------------|
| 1 | POST | `/api/apps/deploy` | Deploy from YAML path |
| 2 | POST | `/api/apps/deploy/upload` | Deploy from file upload |
| 3 | GET | `/api/apps/` | List deployed apps |
| 4 | GET | `/api/apps/{app_id}` | Get app details |
| 5 | POST | `/api/apps/{app_id}/run` | One-shot execution |
| 6 | DELETE | `/api/apps/{app_id}` | Undeploy |
| 7 | POST | `/api/apps/{app_id}/chat` | Sync conversation turn |
| 8 | POST | `/api/apps/{app_id}/chat/stream` | Socket.IO streaming turn |
| 9 | GET | `/api/apps/{app_id}/sessions` | List sessions |
| 10 | GET | `/api/apps/{app_id}/sessions/{sid}` | Get session metadata |
| 11 | GET | `/api/apps/{app_id}/sessions/{sid}/history` | Full message history |
| 12 | DELETE | `/api/apps/{app_id}/sessions/{sid}` | Delete session |
| 13 | GET | `/api/apps/{app_id}/sessions/{sid}/events` | Persistent Socket.IO stream |
| 14 | POST | `/api/apps/{app_id}/sessions/{sid}/messages` | Send message (async) |
| 15 | POST | `/api/apps/{app_id}/background-tasks` | Launch task |
| 16 | GET | `/api/apps/{app_id}/background-tasks` | List tasks |
| 17 | GET | `/api/apps/{app_id}/background-tasks/{id}` | Get task status |
| 18 | DELETE | `/api/apps/{app_id}/background-tasks/{id}` | Cancel task |
| 19 | POST | `/api/apps/{app_id}/background-tasks/{id}/wait` | Wait for task |
| 20 | GET | `/api/apps/{app_id}/tools/search` | Search tools |
| 21 | GET | `/api/apps/{app_id}/tools/categories` | List categories |
| 22 | GET | `/api/apps/{app_id}/tools/categories/{c}` | Browse category |
| 23 | GET | `/api/apps/{app_id}/tools/{name}` | Get tool schema |
| 24 | POST | `/api/apps/{app_id}/tools/{name}/execute` | Execute tool |
| 25 | GET | `/api/apps/{app_id}/index` | Full tool index |
| 26 | POST | `/api/apps/{app_id}/notifications` | Drain notifications |
| 27 | GET | `/api/apps/{app_id}/notifications/active` | Check for active notifications |
| 28 | GET | `/api/apps/{app_id}/approvals` | List pending approvals |
| 29 | POST | `/api/apps/{app_id}/approve` | Resolve approval |
| 30 | GET | `/api/apps/{app_id}/quota` | Get rate limit usage |
| 31 | PUT | `/api/apps/{app_id}/quota` | Set custom rate limit |
| 32 | DELETE | `/api/apps/{app_id}/quota` | Reset rate limit |
| 33 | GET | `/api/apps/{app_id}/secrets` | List secret keys |
| 34 | GET | `/api/apps/{app_id}/secrets/{key}` | Check secret exists |
| 35 | PUT | `/api/apps/{app_id}/secrets/{key}` | Set secret value |
| 36 | DELETE | `/api/apps/{app_id}/secrets/{key}` | Delete secret |

---

## Health & Metrics Endpoints

Infrastructure endpoints for monitoring and orchestration. These are daemon-level (not scoped to an app).

```
GET /health              → {"status": "ok"}
GET /healthz             → 200 (Kubernetes liveness)
GET /readyz              → 200 (Kubernetes readiness)
GET /api/metrics         → JSON metrics snapshot
GET /api/metrics/prometheus → Prometheus text exposition
```

---

## MCP Management API

Daemon-level endpoints for managing MCP server installations and the connection pool.

### Server Management

```
GET    /api/mcp/search?q=...           → Search MCP catalog
POST   /api/mcp/servers                → Install MCP server
DELETE /api/mcp/servers/{server_id}     → Remove MCP server
GET    /api/mcp/servers                → List installed servers
GET    /api/mcp/servers/{server_id}    → Server details
POST   /api/mcp/servers/{server_id}/test → Test server connection
PUT    /api/mcp/servers/{server_id}/config → Update server config
```

### Connection Pool

```
GET    /api/mcp/pool                         → Pool status
POST   /api/mcp/pool/{server_id}/connect     → Connect server in pool
POST   /api/mcp/pool/{server_id}/disconnect  → Disconnect server
GET    /api/mcp/pool/health                  → Pool health check
```

---

## Per-User Rate Limiting

Extends the per-app rate limiting with user-level granularity. Requires authentication middleware to identify users.

```
GET    /api/apps/{app_id}/quota/user/{user_id}     → Get user quota
PUT    /api/apps/{app_id}/quota/user/{user_id}     → Set user quota
DELETE /api/apps/{app_id}/quota/user/{user_id}     → Reset user quota
```

---

## MCP OAuth (per-app)

OAuth flow management for MCP servers that require user-level authentication.

```
GET    /api/apps/{app_id}/oauth/authorize              → Start OAuth flow
GET    /api/apps/{app_id}/oauth/callback               → OAuth callback
GET    /api/apps/{app_id}/mcp/pending-oauth             → Pending OAuth requests
POST   /api/apps/{app_id}/mcp/{server_id}/oauth-token  → Inject token
DELETE /api/apps/{app_id}/mcp/{server_id}/oauth-token  → Revoke token
```

---

## Module API

Direct access to loaded modules, their manifests, and action execution.

```
GET    /api/modules                          → List loaded modules
GET    /api/modules/{module_id}              → Module details + manifest
POST   /api/modules/{module_id}/execute      → Execute action directly
GET    /api/modules/{module_id}/health       → Module health check
```
