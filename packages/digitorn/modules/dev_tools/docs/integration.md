# dev_tools - Integration Guide

`dev_tools` is the module the **builder agent** uses to test apps it
generates. It exposes **3 super-actions** (`App`, `Chat`, `Run`) that
each dispatch to the daemon's live REST surface - deploy the YAML,
open sessions, send messages, inspect history - without making the LLM
stitch together half a dozen lower-level tool calls.

## Actions

| Action | What it covers |
|---|---|
| `App` | App lifecycle: deploy, list, inspect, reload, disable, package install/uninstall, draft CRUD, MCP discovery. |
| `Chat` | Session lifecycle + chat: create/delete, send, history, approvals, memory, workspace snapshot, live events. |
| `Run` | One-shot / pipeline execution on a deployed app (non-conversational). |

Each action routes on a small set of keyword parameters (`deploy_path=`,
`create_draft_yaml=`, `list_apps=True`, ...) so the agent picks a mode
by filling one field instead of remembering 30 tool names.

## Typical flow (builder scenario)

```
builder agent
    │
    ├─ App(deploy_path="./my-app.yaml", draft_id="xyz")
    │       → daemon compiles + deploys
    │
    ├─ Chat(app_id="my-app", message="run the smoke test",
    │       session_id="tmp-1")
    │       → daemon starts session, streams tool calls & text
    │
    ├─ Chat(app_id="my-app", session_id="tmp-1", get_history=True)
    │       → read back what happened
    │
    └─ App(delete_app_id="my-app")    # cleanup
```

## How it reaches the daemon

`dev_tools` lives inside the daemon process and calls its **own REST
routes** via `http://127.0.0.1:<port>/api/*`. The loopback auth bypass
in `auth/middleware.py::_is_loopback_self_call` whitelists
`/api/apps/`, `/api/discovery/`, `/api/credentials/providers`, and
`/api/health` for this module - no JWT is required for these in-process
self-calls.

## Why 3 super-actions instead of ~30 granular ones

Each super-action boils down ~10 REST endpoints into one schema the
LLM can understand from a single tool description. The builder agent
sees 3 tools with well-documented modes, not 30 tools it needs to
keep straight. Internally each mode is a clean dispatch on one or two
boolean/path parameters.

## Constraints

No module-level constraints. All safety comes from the daemon's REST
auth + security profile attached to the target app (`dev_tools` can
only deploy or delete apps the caller's user is allowed to manage).

## When NOT to use

- Outside the builder context. Normal end-user apps should not have
  `dev_tools` granted - it's a privileged "admin of other apps" surface.
- For scripted integration tests from outside the daemon - use the
  `digitorn.testing.DevClient` SDK instead, which talks to the same
  REST surface from outside the process.

## Related

- `packages/digitorn/core/api/apps_v2/lifecycle.py` - underlying deploy route
- `packages/digitorn/testing/client.py` - out-of-process counterpart
