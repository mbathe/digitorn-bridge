---
id: dev-cli
---

# Dev CLI — Test Apps Against the Real Daemon

The Dev CLI is a command-line tool for testing Digitorn apps against the real daemon. It sends HTTP requests to the same API the Flutter client uses — every behavior rule, sub-agent, tool call, and approval flow runs in production mode.

**Primary use cases:**
- **Developers** testing apps during development
- **Digitorn Builder** agent testing apps it creates automatically
- **CI/CD** pipelines validating app deployments
- **Agents** that need to verify their work end-to-end

## Commands

```bash
digitorn dev deploy <yaml_path>                    # Deploy an app
digitorn dev status <app_id>                       # Check app status
digitorn dev chat <app_id> [options]               # Interactive chat
digitorn dev history <app_id> <session_id>         # Show session history
```

## Quick Start

```bash
# 1. Deploy your app
digitorn dev deploy my-app.yaml

# 2. Chat with it (interactive)
digitorn dev chat my-app --workspace /path/to/project

# 3. Or send a single message (non-interactive, for scripts/agents)
digitorn dev chat my-app -w /path/to/project -m "analyze this project"
```

## Commands Reference

### deploy

Deploy an app YAML to the daemon for testing.

```bash
digitorn dev deploy <yaml_path> [--daemon URL] [--force/--no-force]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--daemon`, `-d` | `http://127.0.0.1:8000` | Daemon URL |
| `--force`, `-f` | `true` | Force redeploy if already deployed |

**Example:**
```bash
digitorn dev deploy packages/digitorn/builtins/digitorn-code/app.yaml
# Deployed: digitorn-code (conversation mode)
```

### status

Show deployment status for an app.

```bash
digitorn dev status <app_id> [--daemon URL]
```

**Example:**
```bash
digitorn dev status digitorn-code
# App: digitorn-code
#   Status: deployed
#   Mode: conversation
#   Agents: ['main', 'worker', 'explore', 'plan', 'verification']
```

### chat

Interactive multi-turn chat with a deployed app. This is the main testing tool.

```bash
digitorn dev chat <app_id> [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--workspace`, `-w` | current directory | Workspace directory path |
| `--daemon`, `-d` | `http://127.0.0.1:8000` | Daemon URL |
| `--session`, `-s` | auto-generated | Resume an existing session |
| `--timeout`, `-t` | `600` | Max wait time per turn (seconds) |
| `--message`, `-m` | (empty) | Single message — non-interactive mode |

**Interactive mode:**
```bash
digitorn dev chat digitorn-code -w /path/to/project

> analyze this project
(agent working...)
Assistant: This is a Python project using FastAPI...

> fix the bug in src/auth.py
(agent working...)
  [auto-approved] filesystem.read
  [auto-approved] filesystem.edit
Assistant: Fixed the null check at line 42...

> /quit
```

**Single message mode (for scripts and agents):**
```bash
digitorn dev chat digitorn-code \
  -w /path/to/project \
  -m "read pyproject.toml and tell me what this project does"
```

**In-session commands:**

| Command | Description |
|---------|-------------|
| `/quit` or `/exit` | End the session |
| `/abort` | Cancel the current agent turn |
| `/history` | Show full session history |
| `/status` | Show session status |

### history

Show the full conversation history for a session.

```bash
digitorn dev history <app_id> <session_id> [--daemon URL]
```

**Example:**
```bash
digitorn dev history digitorn-code dev-abc123
# [system] You are agent "main"...
# > analyze this project
# Assistant: (used 3 tools)
#   [Glob] pattern=**/*.py
#   [Read] file_path=pyproject.toml
#   [Grep] pattern=def main
# Assistant: This project is...
```

## Auto-Approval

The dev CLI automatically approves all pending tool approval requests while waiting for the agent to complete. This mimics the Flutter client behavior where the user clicks "Approve" on each tool call.

Approvals are polled every second during the wait loop. Each approved tool is logged:
```
(agent working...)
  [auto-approved] filesystem.read
  [auto-approved] shell.bash
  [auto-approved] filesystem.edit
```

**For testing without approval popups**, create an app with `default_policy: auto`:
```yaml
capabilities:
  default_policy: auto    # auto-approve everything
  max_risk_level: high
  grant:
    - module: filesystem
      actions: [read, write, edit, grep, glob]
    - module: shell
      actions: [bash]
    - module: memory
      actions: [set_goal, remember, task_create, task_update]
```

## Using from Python (for agents)

The dev CLI can be called programmatically from Python — this is how the Digitorn Builder agent tests the apps it creates:

```python
from digitorn.core.cli.dev import dev_cli

# Deploy
dev_cli(["deploy", "path/to/app.yaml"])

# Single message test
dev_cli(["chat", "my-app", "-w", "/path/to/project", "-m", "hello"])

# Check status
dev_cli(["status", "my-app"])

# Get history
dev_cli(["history", "my-app", "session-id"])
```

### Using daemon_request directly (more control)

```python
from digitorn.core.cli.auth_helpers import daemon_request
import json, time

daemon = "http://127.0.0.1:8000"
app_id = "my-app"
session_id = "test-001"

# 1. Deploy
resp = daemon_request("post", f"{daemon}/api/apps/deploy", json={
    "yaml_path": "/absolute/path/to/app.yaml",
    "force": True,
})

# 2. Send message
resp = daemon_request("post",
    f"{daemon}/api/apps/{app_id}/sessions/{session_id}/messages",
    json={"message": "hello", "workspace": "/path/to/project"},
)

# 3. Poll until done
while True:
    time.sleep(2)
    r = daemon_request("get",
        f"{daemon}/api/apps/{app_id}/sessions/{session_id}",
    )
    data = r.json().get("data", {})
    if data.get("is_active") is False and data.get("turn_count", 0) > 0:
        break

# 4. Get response
r = daemon_request("get",
    f"{daemon}/api/apps/{app_id}/sessions/{session_id}/history",
)
messages = r.json()["data"]["messages"]
for m in messages:
    if m["role"] == "assistant" and m.get("content"):
        print(m["content"])
```

### Auto-approve from Python

```python
from digitorn.core.cli.auth_helpers import daemon_request

def auto_approve(daemon, app_id):
    """Approve all pending requests. Call this in your poll loop."""
    r = daemon_request("get", f"{daemon}/api/apps/{app_id}/approvals")
    for req in r.json().get("data", {}).get("pending", []):
        daemon_request("post", f"{daemon}/api/apps/{app_id}/approve",
            json={"request_id": req["request_id"], "approved": True},
        )

# In your poll loop:
while True:
    time.sleep(1)
    auto_approve(daemon, app_id)
    # ... check if session is done ...
```

## API Endpoints Used

The dev CLI talks to these daemon endpoints:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/apps/deploy` | POST | Deploy an app from YAML |
| `/api/apps/{app_id}` | GET | Get app status |
| `/api/apps/{app_id}/sessions` | GET | List sessions |
| `/api/apps/{app_id}/sessions/{sid}` | GET | Get session status |
| `/api/apps/{app_id}/sessions/{sid}/messages` | POST | Send a message |
| `/api/apps/{app_id}/sessions/{sid}/history` | GET | Get full history |
| `/api/apps/{app_id}/sessions/{sid}/abort` | POST | Abort current turn |
| `/api/apps/{app_id}/approvals` | GET | List pending approvals |
| `/api/apps/{app_id}/approve` | POST | Resolve an approval |

## Testing Workflow for Builder Agent

The Digitorn Builder agent follows this workflow to test apps it creates:

```
1. Write the app.yaml
2. Deploy: dev_cli(["deploy", "app.yaml", "--force"])
3. Smoke test: dev_cli(["chat", app_id, "-m", "say hello"])
4. Functional test: dev_cli(["chat", app_id, "-w", workspace, "-m", task])
5. Verify: dev_cli(["history", app_id, session_id])
6. If test fails: read error, fix app.yaml, redeploy, retry
```

**Example test script for Builder:**
```python
from digitorn.core.cli.auth_helpers import daemon_request
import time

def test_app(app_id, yaml_path, test_message, workspace, timeout=60):
    """Deploy and test an app. Returns (success, response)."""
    daemon = "http://127.0.0.1:8000"

    # Deploy
    r = daemon_request("post", f"{daemon}/api/apps/deploy",
        json={"yaml_path": yaml_path, "force": True})
    if not r.json().get("success"):
        return False, f"Deploy failed: {r.json().get('error')}"

    time.sleep(3)

    # Send test message
    sid = f"test-{int(time.time())}"
    daemon_request("post",
        f"{daemon}/api/apps/{app_id}/sessions/{sid}/messages",
        json={"message": test_message, "workspace": workspace})

    # Poll with auto-approve
    for i in range(timeout // 2):
        time.sleep(2)
        # Auto-approve
        try:
            ar = daemon_request("get", f"{daemon}/api/apps/{app_id}/approvals")
            for p in ar.json().get("data", {}).get("pending", []):
                daemon_request("post", f"{daemon}/api/apps/{app_id}/approve",
                    json={"request_id": p["request_id"], "approved": True})
        except Exception:
            pass

        r = daemon_request("get", f"{daemon}/api/apps/{app_id}/sessions/{sid}")
        if r.status_code == 200:
            d = r.json().get("data", {})
            if d.get("is_active") is False and d.get("turn_count", 0) > 0:
                # Get response
                rh = daemon_request("get",
                    f"{daemon}/api/apps/{app_id}/sessions/{sid}/history")
                msgs = rh.json()["data"]["messages"]
                for m in reversed(msgs):
                    if m["role"] == "assistant" and m.get("content"):
                        return True, m["content"]
                return True, "(no text response)"

    return False, "Timeout"


# Usage:
ok, response = test_app(
    app_id="my-new-app",
    yaml_path="/path/to/app.yaml",
    test_message="analyze this project briefly",
    workspace="/path/to/project",
)
print(f"Test {'PASSED' if ok else 'FAILED'}: {response[:200]}")
```

## Troubleshooting

### "Cannot connect to daemon"
The daemon is not running. Start it:
```bash
py -3.12 -m digitorn serve
```

### Session not found (404)
The agent turn crashed before persisting the session. Check daemon logs for `TURN_TASK_CRASHED` or `agent_turn_crashed` tracebacks.

### Agent blocks forever
The app requires approval (`capabilities.approve`) but the dev CLI can't approve fast enough. Either:
- Use `default_policy: auto` in the app YAML (recommended for testing)
- The dev CLI auto-approves, but there may be a timing gap

### Credential missing
The app uses `{{secret.API_KEY}}` but the key is not in the credential store. Fix:
- Add the key to `.env` in the project root and use `{{env.API_KEY}}`
- Or configure credentials via the Flutter client first
