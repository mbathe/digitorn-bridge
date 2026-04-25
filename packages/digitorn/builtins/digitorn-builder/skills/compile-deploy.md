---
version: 1
description: How to compile, fix errors, deploy, configure secrets, and reload
---

## Compile & deploy skill

### Step 1 — Compile

ALWAYS compile before showing YAML to the user:

```
http.json_api(method="POST",
  url="http://127.0.0.1:8000/api/discovery/compile",
  json_body={"yaml": <current_yaml>})
```

Response:
```json
{
  "success": true,
  "data": {
    "valid": true,
    "errors": [],
    "warnings": [],
    "summary": { "app_id": "...", "mode": "...", "agents": [...] }
  }
}
```

Track compile status via tasks, not state files. If you already have
a "Compile" task on your todo list, flip it to `in_progress` while
compiling and `completed` once `valid: true`. On failure, the task
description can record the latest attempt count + first error.

### Step 2 — Fix errors (up to 5 attempts)

If `valid == false`, read each error and fix:

Common errors and fixes:
- `agents[0].brain.provider: invalid value` → check /api/credentials/providers
- `module 'X' not found` → check /api/discovery/modules
- `action 'X' not found on module 'Y'` → check /api/discovery/modules/Y
- `entry_agent 'X' not found` → agent id must match agents[].id
- `capabilities: unknown module` → typo, verify with discovery API

After 5 failed attempts: STOP, show errors, ask user for help.

### Step 3 — Save as draft

First save:
```
http.json_api(method="POST",
  url="http://127.0.0.1:8000/api/builder/drafts",
  json_body={"name": "<app name>", "initial_yaml": "<yaml>"})
```
→ remember draft_id with `memory.remember`

Update existing draft:
```
http.json_api(method="PATCH",
  url="http://127.0.0.1:8000/api/builder/drafts/<id>",
  json_body={"current_yaml": "<yaml>"})
```

### Step 4 — Propose deployment

NEVER deploy without consent. Use ask_user:
```
ask_user(
  question="The app validates cleanly. Deploy it?",
  content=<the YAML>,
  choices=["deploy now", "keep as draft", "change something"]
)
```

Deploy:
```
http.json_api(method="POST",
  url="http://127.0.0.1:8000/api/builder/drafts/<id>/deploy")
```

### Step 5 — Configure secrets

After deploy, check required secrets:
```
http.get(url="http://127.0.0.1:8000/api/apps/<app_id>/required-secrets")
```

If secrets are missing, guide the user:
```
ask_user(
  question="The app needs an API key. Enter your key:",
  form=[
    {"type": "text", "name": "api_key", "label": "API Key", "placeholder": "sk-..."}
  ]
)
```

Then set it:
```
http.json_api(method="PUT",
  url="http://127.0.0.1:8000/api/apps/<app_id>/secrets/<key>",
  json_body={"value": "<user_provided_key>"})
```

### Step 6 — Reload

After setting secrets, reload the app:
```
http.json_api(method="POST",
  url="http://127.0.0.1:8000/api/apps/<app_id>/reload")
```

### Step 7 — Verify

Check app status:
```
http.get(url="http://127.0.0.1:8000/api/apps/<app_id>/status")
```

Check preview (if applicable):
```
http.get(url="http://127.0.0.1:8000/api/apps/<app_id>/preview-server/status")
```

### Step 8 — Package (optional)

If user wants to package:
```
http.json_api(method="POST",
  url="http://127.0.0.1:8000/api/discovery/generate-package-manifest",
  json_body={"yaml": "<yaml>"})
```

Show the package.toml to user for review. Then install:
```
workspace.write(path="packages/<app_id>/package.toml", content=<toml>)
workspace.write(path="packages/<app_id>/app.yaml", content=<yaml>)

http.json_api(method="POST",
  url="http://127.0.0.1:8000/api/packages/install",
  json_body={"source_type": "local", "source_uri": "<path>", "accept_permissions": false})
```

First call returns 409 with permissions → show to user → if approved:
```
http.json_api(method="POST",
  url="http://127.0.0.1:8000/api/packages/install",
  json_body={"source_type": "local", "source_uri": "<path>", "accept_permissions": true})
```

### Avoiding collisions

Before deploying, check existing apps:
```
http.get(url="http://127.0.0.1:8000/api/apps")
```

If app_id already exists, warn the user or suggest a different name.
