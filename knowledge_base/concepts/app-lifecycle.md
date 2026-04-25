---
id: app-lifecycle
title: "App Lifecycle"
type: concept
keywords: [lifecycle, deploy, validate, compile, secrets, test, reload, debug, package, install, upgrade, status, run, session]
related: [what-is-digitorn, app-structure, common-errors, secrets-credentials, modules-overview, package]
source: docs/
---

# App Lifecycle

## Overview

The complete lifecycle of a Digitorn app from idea to production. Every step has a corresponding API call.

## Step 1 -- Init

Create the app project. Two options:

**Option A: CLI scaffold**
```bash
digitorn package new my-app --template chat
```

**Option B: Write app.yaml manually**

Create a file with the minimum required blocks:

```yaml
app:
  app_id: my-app
  name: "My App"

agents:
  - id: main
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
      temperature: 0.2
    system_prompt: |
      You are a helpful assistant.

execution:
  mode: conversation
```

## Step 2 -- Validate (compile without deploying)

Test that the YAML is valid before deploying:

```
POST /api/discovery/compile
{
  "yaml": "<full YAML string>"
}
```

The compiler runs the full validation pipeline:
1. Parse YAML and validate against the Pydantic schema (`AppDefinition`)
2. Resolve `{{variables}}` in all params and constraints
3. Validate that referenced modules exist in the registry
4. Validate that referenced actions exist on each module
5. Validate params against each action's `params_model`
6. Validate constraints against `ConstraintSpec`
7. Build `SecurityProfile` from capabilities

**On success**, the response includes:
- `compiled` -- the resolved app structure
- `graph` -- agent/module dependency graph
- `warnings` -- non-fatal issues

**On failure**, the response includes:
- `errors` -- list of every problem found (all at once, not one-by-one)
- Each error has: message, location (dotted path), category

## Step 3 -- Fix errors

Read the error messages from the compile response. Common patterns:

```json
{
  "errors": [
    {
      "message": "module 'filesystem' not found in registry",
      "location": "modules.filesystem",
      "category": "module_not_found"
    },
    {
      "message": "action 'write_file' not found on module 'filesystem' (did you mean 'write'?)",
      "location": "modules.filesystem.setup[0].action",
      "category": "action_not_found"
    }
  ]
}
```

Fix the YAML and compile again. The compiler reports ALL errors at once so you can fix them in batches. Aim for zero errors in under 5 attempts.

Use the discovery API to find valid module/action names:
- `GET /api/discovery/modules` -- list all available modules
- `GET /api/discovery/modules/{module_id}` -- list actions with their parameters

## Step 4 -- Deploy

Deploy the validated app to the daemon:

**Option A: Deploy from YAML string**
```
POST /api/apps/deploy
{
  "yaml": "<full YAML string>",
  "force": false
}
```

**Option B: Deploy from file path**
```
POST /api/apps/deploy
{
  "yaml_path": "/path/to/app.yaml",
  "force": false
}
```

**Option C: Deploy from builder draft**
```
POST /api/builder/drafts/{draft_id}/deploy
```

**Option D: Deploy with file upload**
```
POST /api/apps/deploy/upload
Content-Type: multipart/form-data

yaml_file: <file>
force: false
secrets: '{"DEEPSEEK_API_KEY": "sk-..."}'
```

The `force` flag:
- `false` (default) -- refuse if an app with the same `app_id` is already deployed
- `true` -- redeploy, replacing the existing app

Inline secrets can be passed at deploy time via the `secrets` field. These are set in the secret store immediately.

## Step 5 -- Configure secrets

Check what secrets the app needs:

```
GET /api/apps/{app_id}/required-secrets
```

Response:
```json
{
  "secrets": [
    {
      "key": "DEEPSEEK_API_KEY",
      "set": false,
      "locations": ["agents[0].brain.config.api_key"],
      "providers": ["deepseek"],
      "agents": ["main"]
    }
  ],
  "missing_count": 1,
  "total_required": 1
}
```

Set each missing secret:

```
PUT /api/apps/{app_id}/secrets/DEEPSEEK_API_KEY
{
  "value": "sk-..."
}
```

By default, setting a secret auto-reloads the app. Pass `?reload=false` to stage multiple secrets and reload once at the end.

Set multiple secrets at once:

```
PUT /api/apps/{app_id}/secrets
{
  "secrets": {
    "DEEPSEEK_API_KEY": "sk-...",
    "OPENAI_API_KEY": "sk-..."
  }
}
```

## Step 6 -- Test

**One-shot mode:**
```
POST /api/apps/{app_id}/run
{
  "input": "Analyze this code: def foo(): return 42"
}
```

**Conversation mode:**

1. Create a session:
```
POST /api/apps/{app_id}/sessions
{
  "workspace": "/path/to/project"
}
```

Response includes `session_id`.

2. Send a message:
```
POST /api/apps/{app_id}/sessions/{session_id}/messages
{
  "message": "What files are in this project?"
}
```

The response is an SSE stream with events:
- `text_delta` -- streaming text from the LLM
- `thinking_delta` -- thinking/reasoning content
- `tool_call` -- tool invocation with params and result
- `done` -- turn complete with token usage
- `error` -- error with classification

**Background mode:**

Sessions are created automatically (mono) or via API (multi). Triggers fire and activate the agent. Check results:

```
GET /api/apps/{app_id}/sessions/{session_id}/history
```

## Step 7 -- Check status

```
GET /api/apps/{app_id}/status
```

Response includes:
- Deployment status (deployed, running, error)
- Active session count
- Module health
- Secret status (all required secrets set?)
- Last error (if any)

For apps with preview servers:
```
GET /api/apps/{app_id}/preview-server/status
```

## Step 8 -- Reload (hot-reload)

After changing secrets, credentials, or module config:

```
POST /api/apps/{app_id}/reload
```

This recompiles the YAML with current secrets and reconfigures all modules without restarting the daemon. Active sessions continue with the updated configuration.

## Step 9 -- Debug

When something goes wrong:

1. **Check required secrets:**
```
GET /api/apps/{app_id}/required-secrets
```
Missing secrets are the #1 cause of runtime errors.

2. **Check credentials schema:**
```
GET /api/apps/{app_id}/credentials/schema
```
Shows what external services the app expects.

3. **Check app status:**
```
GET /api/apps/{app_id}/status
```

4. **Check preview server logs (if applicable):**
```
GET /api/apps/{app_id}/preview-server/logs?limit=200
```

5. **Re-validate the YAML:**
```
POST /api/discovery/compile
{
  "yaml": "<current YAML>"
}
```

6. **Check available modules:**
```
GET /api/discovery/modules
```

7. **Check specific module actions:**
```
GET /api/discovery/modules/{module_id}
```

## Step 10 -- Package

Turn a deployed app into an installable package:

1. Generate the manifest:
```
POST /api/discovery/generate-package-manifest
{
  "yaml": "<app YAML>"
}
```

This returns a `package.toml` with auto-detected requirements and permissions.

2. Create the package directory with `package.toml` + `app.yaml`.

3. Install as a package:
```
POST /api/packages/install
{
  "source": "/path/to/my-app/",
  "scope": "system"
}
```

Scope: `system` (visible to all users, admin only) or `user` (personal, default).

## Step 11 -- Upgrade

When you update a packaged app:

```
POST /api/packages/{package_id}/upgrade
```

The daemon:
1. Patches files in-place (no rename swap, safe on Windows)
2. Recompiles the YAML
3. Reloads the running app
4. Preserves `node_modules`, `dist`, `.cache`, and other build artifacts

## Session management

Beyond the basic lifecycle, sessions support advanced operations:

| Operation | Endpoint | Description |
|-----------|----------|-------------|
| Compact | `POST /{app_id}/sessions/{sid}/compact` | Compress context window |
| Undo | `POST /{app_id}/sessions/{sid}/undo` | Undo last agent turn |
| Fork | `POST /{app_id}/sessions/{sid}/fork` | Clone session at current state |
| Abort | `POST /{app_id}/sessions/{sid}/abort` | Cancel running turn |
| Resume | `POST /{app_id}/sessions/{sid}/resume` | Resume after interruption |
| Export | `GET /{app_id}/sessions/{sid}/export` | Export full conversation |
| Delete | `DELETE /{app_id}/sessions/{sid}` | Delete session and history |
| Memory | `GET /{app_id}/sessions/{sid}/memory` | Get goal, facts, todos |
| Workspace | `GET /{app_id}/sessions/{sid}/workspace` | Get virtual file tree |

## See also

- what-is-digitorn -- the big picture
- app-structure -- how to organize the project
- common-errors -- troubleshooting guide
- secrets-credentials -- managing API keys
- package -- packaging and distribution
