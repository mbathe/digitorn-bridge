---
id: module-concept-context_builder
title: "context_builder module - overview"
type: module-concept
module: context_builder
isolation: session
keywords: [context_builder, context_builder-module, watch_start, watch_stop, watch_pause, watch_resume, watch_status, watch_list, watch_history, background_run, search_tools, get_tool, execute_tool, list_categories, browse_category, run_parallel, use_skill]
version: 2.0.0
---

# `context_builder` module

- **Isolation**: `session` (per-session state)
- **Version**: `2.0.0`
- **Actions**: 17 visible, 0 internal

## Description (from class docstring)

ContextBuilderModule - Tool Discovery Engine + Primitive Capabilities.

System module that manages tool discovery (5 meta-tools) and exposes
universal primitive capabilities for parallel execution, background task
management, persistent watchers, and scheduling.

Architecture: thin facade composing 3 action mixins:
    - MetaToolsMixin      - search/get/execute/list/browse + run_parallel
    - BackgroundActionsMixin - background_run/status/result/cancel/list/wait
    - WatcherActionsMixin - watch_start/stop/pause/resume/status/list/history

Scheduling lives in the dedicated cron_native module (schedule + cancel_schedule).

> Class-level summary: Tool Discovery Engine + Primitive Capabilities + Persistent Watchers.

    Manages a pre-computed ToolIndex and exposes:
    - meta-tools for agent-driven tool discovery and execution
    - primitive capabilities for parallel execution and background tasks
    - watcher actions for persistent periodic monitoring

## Configuration

Set under `modules.context_builder.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon. |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `watch_start` | `WatchStart` |  | medium | Start a persistent watcher that periodically executes a tool and reports back ONLY when something interesting happens... |
| `watch_stop` | `WatchStop` |  | low | Stop and remove a watcher. The watcher is cancelled and its history is discarded. |
| `watch_pause` | `WatchPause` |  | low | Pause a running watcher. The timer continues but checks are skipped. History is preserved. Use watch_resume to restart. |
| `watch_resume` | `WatchResume` |  | low | Resume a paused watcher. Checks restart immediately. |
| `watch_status` | `WatchStatus` |  | low | Get detailed status of a watcher: metrics, last result, configuration, and recent history. |
| `watch_list` | `WatchList` |  | low | List all watchers with their current status, check counts, and notification counts. Running watchers are shown first. |
| `watch_history` | `WatchHistory` |  | low | Get the last N check results from a watcher's history. Each entry includes timestamp, result/error, and whether a not... |
| `background_run` | `BackgroundRun` |  | medium | Run any tool in the background - returns task_id immediately. |
| `search_tools` | `SearchTools` |  | low | Search for tools by keyword or description. Returns matching tools with full parameter schemas so you can call Execut... |
| `get_tool` | `GetTool` |  | low | Get the full schema for a specific tool. Internal - SearchTools now returns schemas directly. |
| `execute_tool` | `ExecuteTool` |  | medium | Execute a tool by name. Use SearchTools first to find the tool and see its parameter schema. |
| `list_categories` | `ListCategories` |  | low | List all available tool categories (modules) with their descriptions and tool counts. Use this to get an overview of ... |
| `browse_category` | `BrowseCategory` |  | low | Browse all tools in a specific category (module). Shows tool names, descriptions, and risk levels. Paginated (20 tool... |
| `run_parallel` | `RunParallel` |  | medium | Execute multiple tool calls in parallel. |
| `use_skill` | `UseSkill` |  | low | Load a skill -- a reusable workflow with detailed instructions. Skills provide step-by-step methodology for specific ... |
| `call_app` | `CallApp` |  | medium | Call another deployed Digitorn app and return its result. The target app must be deployed on the daemon and in one_sh... |
| `ask_user` | `AskUser` |  | low | Ask the user a question and WAIT for their response. The agent pauses until the user replies. Supports: simple questi... |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [watch_start, watch_stop, watch_pause, watch_resume, watch_status, watch_list, watch_history, background_run, search_tools, get_tool, execute_tool, list_categories, browse_category, run_parallel, use_skill, call_app, ask_user]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {context_builder: [watch_start, watch_stop, watch_pause, watch_resume, watch_status]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/context_builder-*.md`.
