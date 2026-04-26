---
id: module-concept-dev_tools
title: "dev_tools module — overview"
type: module-concept
module: dev_tools
isolation: shared
keywords: [dev_tools, dev_tools-module, app, chat, run]
version: 3.0.0
---

# `dev_tools` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `3.0.0`
- **Actions**: 3 visible, 0 internal

## Description (from class docstring)

Dev Tools Module — 3 ultra-powerful tools for testing & building Digitorn apps.

Design philosophy: few tools, many modes (like Shell: 1 tool, 5 modes).
The Builder agent needs only 3 tools to do everything a human can do with
the Flutter client AND everything the Builder backend needs to craft apps.

Tools:
  1. App   — lifecycle, discovery, packages, MCP, drafts, security, compile
  2. Chat  — sessions, queue, approvals, memory, workspace, live events
  3. Run   — one-shot, triggers, background sessions, background tasks, pipeline

> Class-level summary: Dev tools for testing + building Digitorn apps — 3 ultra-powerful tools.

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `app` | `DevToolsApp` |  | medium | App lifecycle + discovery + packages + MCP + drafts + security. |
| `chat` | `DevToolsChat` |  | low | Chat with a deployed app — sessions, queue, approvals, workspace, live events. |
| `run` | `DevToolsRun` |  | low | Run non-conversational apps — one-shot, pipeline, triggers, background, watchers. |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: dev_tools
      actions: [app, chat, run]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {dev_tools: [app, chat, run]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/dev_tools-*.md`.
