---
id: client-manifest
---

# Client Manifest

The the chat client / web client reads the deployed app's YAML
(through the daemon's app-detail surface) and uses the
**`ui:` block** plus a handful of `app:` and `runtime:`
fields to tailor its UI: which greeting to show, which
panels to hide, which accent colour to paint, which
`/slash` palette to render.

This page documents what the client actually consumes - the
**daemon never reads `ui:` itself**, it just passes the values
through. Every field on this page maps to a real Pydantic field;
entries are cited with file + line.

## What the client reads

| Source | Used for |
|--------|----------|
| `app.app_id`, `app.name`, `app.icon`, `app.color`, `app.category`, `app.tags`, `app.description` | App card in the catalog. |
| `app.quick_prompts` | One-click prompt suggestions on the empty conversation screen. |
| `runtime.mode` | Conversation vs one_shot vs background; client switches input UX (chat box vs single submit form). |
| `runtime.workdir_mode` | When `none`, the client hides the workspace path picker. |
| `ui.theme` | Accent + background colour overrides. |
| `ui.features` | 12 boolean toggles for individual UI panels / behaviours. |
| `ui.greeting` | Empty-state greeting under the input box. |
| `ui.slash_commands` | The `/`-palette entries. |
| `ui.quick_prompts` | Same shape as `app.quick_prompts`; client merges both lists. |
| `ui.workspace` | Renderer hint + layout (`render_mode`, `entry_file`, `title`, `position`, `width_pct`, `auto_open_on_first_tool`, `default_open`, `default_view`, `hidden_views`, `preview_chrome`). |
| `ui.templates` | One-click bootstrap gallery shown in the empty state. |
| `ui.widgets` | Declarative widget tree rendered in chat, sidebar, modals. |
| `ui.layout` | High-level chat preset (`default`, `code`, `builder`, `research`, `minimal`, `lovable`). |
| `ui.density` | Bubble spacing (`compact` / `comfortable`). |
| `ui.thinking` | Thinking-block visibility and initial collapsed state. |
| `ui.tool_calls` | Tool-chip collapse default and silent-tools visibility. |
| `ui.composer` | Composer toolbar (file upload, voice, slash, quick prompts). Wins over the matching `ui.features.X` keys. |
| `ui.visual` | Bubble accent / style / user alignment. |

The first three groups are covered in
[App Configuration → app](02-app-config.md#app---identity) and
[App Configuration → runtime](02-app-config.md#runtime---lifecycle-and-execution-policy);
this page focuses on the `ui:` block.

## `ui.features` - 12 toggles

`AppMeta.features` (`schema.py`) and `UIBlock.features`
(`schema.py`) are mirror dicts that share the same key set:

| Key | Default (when unspecified) | Effect when `false` |
|-----|----------------------------|---------------------|
| `voice` | `true` | Hides the voice-input button (microphone). |
| `attachments` | `true` | Hides the file/image attachment paperclip. |
| `tools_panel` | `true` | Hides the right-side panel showing tool calls in real time. |
| `snippets` | `true` | Hides the `@`-mention snippet picker. |
| `tasks_panel` | `true` | Hides the todos / tasks side panel (driven by `memory.task_create`). |
| `memory_panel` | `true` | Hides the memory snapshot panel (goal + facts). |
| `context_ring` | `true` | Hides the token-pressure ring around the input. |
| `markdown` | `true` | Renders assistant messages as plain text (no markdown parsing). |
| `slash_commands` | `true` | Hides the `/`-palette popup. |
| `message_actions` | `true` | Hides the per-message Edit / Retry / Copy hover actions. |
| `status_pills` | `true` | Hides the inline `running` / `done` status pills next to assistant messages. |
| `token_badges` | `true` | Hides the per-message token counts. |

Source of truth: the docstring at `schema.py` lists the
exact keys the chat client recognises today. Unknown keys are
ignored silently (the spec is forward-compatible).

```yaml
ui:
  features:
    voice: false
    attachments: false
    tasks_panel: false
    memory_panel: false
    context_ring: false
    token_badges: false
    # tools_panel, snippets, markdown, slash_commands, message_actions,
    # status_pills default to true → kept visible
```

> **Mirror.** `app.features` (`schema.py`) is a deprecated
> nesting that the compiler still accepts - it lifts to
> `ui.features` via the alias pass. Set `ui.features` directly
> in v2 YAML; the compiler emits a warning when you use the
> nested form.

## `ui.theme` - accent + background

`UIBlock.theme` (`schema.py`). Two recognised keys:

```yaml
ui:
  theme:
    accent: "#6EE7B7"        # hex; overrides app.color for fine control
    background: "#0F172A"    # hex; client may apply this to the chat surface
```

Other keys are passed through but unused by the current the chat client /
web clients. Treat `theme` as a forward-compat dict - only `accent`
and `background` are guaranteed.

`app.color` (`schema.py`) is the **catalog** accent (visible on
the app card). `ui.theme.accent` overrides it inside the app once
the user is in the conversation. Set them independently if you
want different colours in the catalog vs in the chat.

## `ui.greeting` - empty-state message

`UIBlock.greeting` (`schema.py`). The text shown above the
input field when a conversation has no messages yet.

```yaml
ui:
  greeting: |
    Hello! I'm your code-review assistant.
    Drop a file, paste a diff, or describe what you want reviewed.
```

Plain text by default; markdown when `ui.features.markdown: true`
(the default). Templated values (`{{app.name}}`, `{{sys.date}}`,
...) are resolved at compile time, not at render time.

## `ui.slash_commands` - `/` palette

`UIBlock.slash_commands` (`schema.py`). List of `SlashCommand`
(`typed_models.py`, `extra: allow`).

```yaml
ui:
  slash_commands:
    - command: /commit
      description: "Commit the current diff with a conventional message"
      template: "Run /commit using {{branch ?? 'the current branch'}}"

    - command: /review
      description: "Review the active file"
      template: "Review {{file}} for security issues"
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `command` | string (min 1) | yes | The `/foo` id. |
| `description` | string | no | One-line description in the palette. |
| `template` | string | no | Message template the client sends to the agent when the user picks this command. Supports `{{var}}` placeholders the client fills from a popup form. |

**Distinct from `dev.skills`** (server-side reusable workflow
markdown the agent loads via `use_skill`). Slash commands are
pure UI sugar - the agent never knows the slash palette existed,
it just sees the rendered `template` as a normal user message.

See [Skills System](21-skills.md) for the difference + the skills
that DO live server-side.

## `ui.quick_prompts` - empty-state buttons

`UIBlock.quick_prompts` (`schema.py`) - list of `QuickPrompt`
(`typed_models.py`, `extra: allow`).

```yaml
ui:
  quick_prompts:
    - label: "New PR"
      message: "Open a PR with the latest changes"
      icon: "rocket"
    - label: "Daily standup"
      message: "Summarize what I did yesterday"
      icon: "clipboard"
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `label` | string (min 1) | yes | Short button label. |
| `message` | string (min 1) | yes | Full prompt sent when the user clicks. |
| `icon` | string | no (default `""`) | Emoji or icon name. |

Mirror: `app.quick_prompts` (`schema.py`) holds the same shape.
The client **merges** both lists, deduping by `label`. Either is
fine; pick one place per app for clarity.

## `ui.workspace` - renderer hint + layout

`UIBlock.workspace` is `WorkspaceBlock` (`schema.py`,
`extra: forbid`). Tells the client this app uses the in-memory
virtual filesystem and how to position the viewer relative to the
chat.

Renderer fields (documented in [Workspace & Preview](41-preview.md)):

- `render_mode: str` (default `"auto"`) - `react`, `html`,
  `markdown`, `slides`, `code`, `latex`, `builder`, or `auto`. Auto
  detects from the first file the agent writes.
- `entry_file: str | null` - default file the renderer opens.
- `title: str | null` - workspace toolbar label.

Layout fields (added 2026-05-04, drive how the chat ↔ workspace
split looks):

- `position: str` (default `"right"`) - `right`, `bottom`,
  `hidden`, or `overlay`. `hidden` keeps the workspace off-screen
  even when files are written; `overlay` floats it over the chat.
- `width_pct: int` (default `50`, range `10..90`) - pane width as
  a percentage of the chat-vs-workspace split. Ignored when
  `position` is `hidden` / `overlay`.
- `auto_open_on_first_tool: bool` (default `true`) - when `true`
  (default), the client opens the workspace pane the first time
  the agent writes a file. Set `false` for chat-only apps that
  should not surface a renderer just because a tool wrote one log.
- `default_open: bool` (default `false`, added 2026-05-14) - when
  `true`, the workspace pane opens IMMEDIATELY on session mount,
  before any agent action. Right for Lovable-style apps where the
  workspace IS the product surface (templates gallery, live
  preview iframe).

View routing fields (added 2026-05-14, control which workspace tab
the user sees and which ones are reachable at all):

- `default_view: str` (default `"auto"`) - `code`, `preview`,
  `changes`, `activity`, or `auto`. `auto` picks `preview` when
  `render_mode` is anything other than `code`, else `code`.
- `hidden_views: list[str]` (default `[]`) - subset of
  `["code", "preview", "changes", "activity"]` to remove from the
  workspace mode menu. Right for hiding Monaco on apps where the
  user should never see the editor, or hiding Changes on
  auto-approve sandboxes. If the current view becomes hidden,
  the panel bounces to the first non-hidden one (preview → code →
  changes → activity).

Preview chrome (added 2026-05-14, per-feature flags for the
toolbar above the live preview iframe):

- `preview_chrome.enabled: bool` (default `true`) - master switch.
  `false` hides the entire chrome (bare iframe).
- `preview_chrome.refresh: bool` (default `true`) - refresh button.
- `preview_chrome.open_in_new_tab: bool` (default `true`) -
  external-link button. Auto-suppressed for daemon-bundled URLs.
- `preview_chrome.viewport_toggle: bool` (default `false`) -
  Mobile (375px) / Tablet (768px) / Desktop preset toggle.
- `preview_chrome.url_bar: str` (default `"auto"`) - `auto`,
  `always`, or `never`. `auto` reveals the URL pill once the
  iframe app has reported `digi:route-change` for at least two
  distinct routes (signals an SPA with routing).

```yaml
ui:
  workspace:
    render_mode: react
    entry_file: src/App.tsx
    title: My App
    position: right
    width_pct: 65
    auto_open_on_first_tool: true
    default_open: true
    default_view: preview
    hidden_views: []
    preview_chrome:
      enabled: true
      refresh: true
      open_in_new_tab: true
      viewport_toggle: true
      url_bar: auto
```

## `ui.widgets` - declarative widget tree

`UIBlock.widgets` (`schema.py`) is `WidgetsConfig`
(`schema.py`). Four sub-trees:

| Field | Type | Description |
|-------|------|-------------|
| `version` | int | Spec version. Daemon refuses unknown versions; only `1` today. |
| `chat_side` | `ChatSideWidget \| null` | Right-side panel rendered alongside the chat. |
| `workspace_tabs` | list[`WorkspaceTabWidget`] | Tabs in the workspace panel. |
| `modals` | dict[name, `ModalWidget`] | Named modals the agent can open via `widget.open` action. |
| `inline` | dict[name, `InlineWidget`] | Inline widgets the agent renders inside chat via `widget.render` with a `ref:`. |

Full surface - 43 widget primitives, 15 actions, server-side
template substitution, live `widget:*` Socket.IO events - is in
[Widgets](42-widgets.md). External widget files under
`./widgets/*.yaml` in the bundle dir are auto-loaded into
`inline` by the compiler (keyed by file stem, same pattern as
skills).

## `ui.layout` - high-level chat preset (2026-05-04)

`UIBlock.layout` is a `str` with default `"default"`. Allowed
values:

- `default` - historical conversational chat.
- `code` - code-editor-friendly chat (Cursor-style).
- `builder` - YAML-editor + smoke-test focus.
- `research` - long-form, citations and agent-group prominent.
- `minimal` - chat only, no workspace, terse chrome.
- `lovable` - workspace-dominant split with auto-open on first
  tool call.

The preset is a sugar layer: when the YAML omits a fine-grained
sub-block (`thinking`, `tool_calls`, `composer`, `visual`,
`workspace`), the client uses the preset's defaults. Any
sub-block the YAML DOES define ALWAYS wins over the preset, so
deriving from `lovable` and tweaking just `workspace.width_pct`
is supported.

## `ui.density` - bubble spacing (2026-05-04)

`UIBlock.density: str`, default `"comfortable"`. Allowed:
`compact`, `comfortable`. Applies to message bubbles and the
gap between consecutive messages.

## `ui.thinking` - thinking-block defaults (2026-05-04)

`UIBlock.thinking` is `ChatThinkingBlock` (`schema.py`,
`extra: forbid`). Two flags:

- `visible: bool` (default `true`) - when `false`, thinking
  blocks are hidden entirely. The agent can still emit them, the
  client just drops them at render time.
- `collapsed_default: bool` (default `true`) - initial collapsed
  state when `visible` is `true`. The user can still toggle.

```yaml
ui:
  thinking:
    visible: false           # production conversational app
```

## `ui.tool_calls` - tool-chip defaults (2026-05-04)

`UIBlock.tool_calls` is `ChatToolCallsBlock` (`schema.py`,
`extra: forbid`):

- `collapsed_default: bool` (default `true`) - initial collapsed
  state of every tool-call chip. The user can expand individual
  chips with the chevron.
- `show_silent: bool` (default `false`) - when `true`, plumbing
  tools (`memory.remember`, `agent_spawn` internals, discovery
  meta-tools like `search_tools` / `list_categories`) are
  rendered. Default `false` keeps them hidden so the chat reads
  as a clean conversation rather than an internals trace.
- `inject_intent: bool` (default `false`) - when `true`, the
  context builder prepends a required `intent` field to every
  tool schema; the model fills it with a short '-ing' phrase
  and the frontend renders a single shimmering line in place of
  the tool chip. Trade-off: ~10-20 extra tokens per tool call.
- `hide_details: bool` (default `false`) - only meaningful with
  `inject_intent: true`. When `true`, the intent line has no
  chevron and no drilldown - the user can never inspect raw
  tool plumbing. For consumer / demo surfaces.
- `strict_mode: bool` (default `false`) - Lovable-style full
  shimmer: extends the progressive line to thinking and
  intermediate text blocks too, not just tool calls. The final
  answer streams in clear; text before an `ask_user` is also
  revealed so the user sees the question's context. Strict opt-in:
  off = zero per-turn overhead, sub-agents are also bypassed
  (their `AgentContext` is built without the gate stash).
- `intent_phrases: IntentPhrasesConfig` - source for the
  shimmer phrases when `strict_mode: true` (ignored otherwise).
  `source: auto | llm | static`. The `llm` path goes through
  the gateway (single egress, never direct provider) and
  falls back to `static` on timeout / error when `source=auto`.
  Full schema (gateway model, prompt template, static phase
  matrix, timeouts) in [`02-app-config.md#uitool_callsintent_phrases`](./02-app-config.md#uitool_callsintent_phrases).

```yaml
# Standard - clean chip view
ui:
  tool_calls:
    collapsed_default: true
    show_silent: false

# Lovable-style full strict mode
ui:
  tool_calls:
    inject_intent: true
    hide_details: true
    strict_mode: true
    intent_phrases:
      source: auto
      llm:
        gateway_model: gpt-4o-mini
```

## `ui.composer` - composer toolbar (2026-05-04)

`UIBlock.composer` is `ChatComposerBlock` (`schema.py`,
`extra: forbid`). Mirrors the legacy `ui.features` flags for the
same concepts; when both are present the typed `composer.X` wins.

- `file_upload: bool` (default `true`) - paperclip / drag-drop
  attachment. Equivalent to `features.attachments`.
- `voice: bool` (default `false`) - microphone button. Default
  `false` here (opt-in for production privacy) vs `features.voice`
  which historically defaulted to `true`.
- `slash_commands: bool` (default `true`) - `/`-palette popup.
  Equivalent to `features.slash_commands`.
- `quick_prompts_visible: bool` (default `true`) - suggested
  prompt chips above the composer when the conversation is empty.

```yaml
ui:
  composer:
    file_upload: true
    voice: false
    slash_commands: true
    quick_prompts_visible: true
```

## `ui.visual` - bubble accent / style (2026-05-04)

`UIBlock.visual` is `ChatVisualBlock` (`schema.py`,
`extra: forbid`). Three knobs:

- `accent: str` (hex, default `""`) - accent colour for the
  send button, cursor, and any per-app highlights. Fallback
  chain: `visual.accent` → `theme.accent` → `app.color`. Empty
  here means "use the next level".
- `bubble_style: str` (default `"card"`) - `card` (rounded box
  with shadow), `flat` (filled background no shadow), or
  `minimal` (no background, just text + thin separator).
- `user_bubble_alignment: str` (default `"right"`) - `right`
  (default chat-room layout) or `left` (RTL or stacked layout
  variants).

```yaml
ui:
  visual:
    accent: "#10b981"
    bubble_style: flat
    user_bubble_alignment: right
```

## `ui.activity` - opt-in sub-agent observability pane (2026-05-06)

`UIBlock.activity` is `ActivityPanelBlock` (`schema.py`,
`extra: forbid`). Surfaces the live sub-agent fan-out, background
tasks, and recent terminal events as a dedicated workspace mode
(web `Activity ▾` entry / the chat client `Activity` mode in the
workspace toolbar).

**Opt-in contract.** When the YAML omits this block, both clients
**hide the Activity entry entirely** - a simple chat app that never
spawns sub-agents stays clean. Apps that orchestrate fan-out
(coordinator, multi-agent research, dev assistants) opt in The pane is driven by the daemon-resource protocol: snapshot on
mount + Socket.IO live updates + heartbeat-driven reconcile, so it
survives daemon restarts, socket drops, and tab-focus cycles
without zombie state. The wire contract (snapshot +
heartbeat + `turn_terminal` consolidated event) is the
daemon-resource protocol used by every workspace pane.

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `enabled` | bool | `true` | Master switch. Set `false` to disable the pane while keeping config (staged rollouts). |
| `position` | str | `"right"` | Where the pane attaches: `right`, `bottom`, `overlay`. |
| `title` | str \| null | `null` | Panel header label. Defaults to the localised "Activity" string. |
| `show_running` | bool | `true` | Render the live sub-agent strip at the top. |
| `show_recent` | bool | `true` | Render the recent-terminal-events scrollable list. |
| `show_stats` | bool | `true` | Render the aggregate stats footer (success rate, avg duration). Pulls from `digitorn_agent_*` Prometheus counters. |
| `show_bg_tasks` | bool | `true` | Interleave background shell tasks alongside sub-agents. |
| `max_recent` | int | `50` | Cap on the number of terminal events kept (range `5..500`). FIFO eviction past the cap. |
| `auto_open_on_spawn` | bool | `false` | Auto-switch to the Activity pane on first sub-agent spawn. Off by default - surface only when the user opens it explicitly. |

```yaml
ui:
  activity:
    enabled: true
    title: "Activity"
    position: right
    show_running: true
    show_recent: true
    show_stats: true
    show_bg_tasks: true
    max_recent: 50
    auto_open_on_spawn: false
```

**What the pane shows** (mirrored across all clients):

1. **Header strip** - pulse dot + status label + live counters
   (`3 running · 8 done · 1 failed`).
2. **Live agents** (sticky top) - one row per running sub-agent
   with name, current task, tool count, durée live (1 Hz local
   tick - keeps moving even when the daemon throttles
   `agent_progress`), and a Cancel button. Click → `POST
   /sessions/{sid}/agents/{id}/cancel` (cooperative cancel via
   `cancel_event` then hard `Task.cancel`).
3. **Recent** (scrollable) - collapsible rows for the last
   `max_recent` terminal events with one-line preview / error.
   Click to expand the full body.
4. **Stats footer** - success ratio, average duration, totals.
5. **Stale overlay** - soft pulsing badge ("Synchronisation…")
   when `isStale` (no heartbeat for 15 s, daemon restarted, etc.).

**Late-event safety.** The pane consumes the `lastTerminalSeq`
guard from the protocol primitive: any `agent_event` arriving with
`seq <= lastTerminalSeq[turn_id]` is dropped, so a turn that
already emitted `turn_terminal` can't have its agents re-armed by a
straggling event.

## `ui.tool_renderers` - custom tool-call rendering (2026-05-06)

`UIBlock.tool_renderers` is `ToolRenderersBlock` (lives in its own
`tool_renderers.py` module so it can be deleted without touching
`schema.py` beyond one forward-string field - see the **Rollback**
note further down). Maps tool names (and regex patterns) to inline
widget refs the client mounts in place of the legacy tool chip.

**Opt-in contract.** When the YAML omits the block (or sets
`enabled: false`), every tool call uses the client's legacy chip -
zero behaviour change. The block being **present + `enabled:
true`** is what flips the dispatcher to consult the maps.

**Bindings exposed to the widget tree** (template engine, same
syntax as the v1 widgets host):

| Token | Type |
| ----- | ---- |
| `{{tool.name}}` | str |
| `{{tool.params.X}}` | any |
| `{{tool.result.X}}` | any |
| `{{tool.result}}` | json |
| `{{tool.error}}` | str |
| `{{tool.duration_ms}}` | int |
| `{{tool.status}}` | str | `running` \| `success` \|

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `enabled` | bool | `false` | Master toggle. False keeps every tool on the legacy chip even if the maps are populated. |
| `by_name` | map | `{}` | Exact-match `tool_name → { ref: widget_id }`. Checked first; an exact hit short-circuits pattern lookup. |
| `by_pattern` | map | `{}` | Regex map. Each key is a `re.search` pattern tested against the tool name in iteration order. First match wins. |
| `fallback_on_error` | bool | `true` | When the renderer throws, fall back to the legacy chip. Set false during local renderer dev to surface failures inline. |

```yaml
ui:
  tool_renderers:
    enabled: true
    by_name:
      WsRead: file_card        # → ui.widgets.inline.file_card
      WsWrite: file_card
    by_pattern:
      "Bash.*": terminal_card  # any tool starting with "Bash"
      "memory.+": memory_chip  # memory.recall, memory.remember, …
    fallback_on_error: true

  widgets:
    inline:
      file_card:
        tree:
          type: card
          children:
            - type: row
              children:
                - { type: icon, name: "file-text" }
                - { type: text, text: "{{tool.params.path}}", variant: title }
            - { type: text, text: "{{tool.status}} · {{tool.duration_ms}}ms", variant: caption }

      terminal_card:
        tree:
          type: card
          children:
            - { type: text, text: "$ {{tool.params.command}}", variant: title }
            - { type: markdown, text: "```\n{{tool.result.stdout}}\n```" }

      memory_chip:
        tree:
          type: badge
          icon: "brain"
          label: "{{tool.name}}"
```

**Match priority** (single tool call, single matching renderer):

1. `by_name[tool.name]` - exact match wins.
2. `by_pattern` - first regex that matches (iteration order).
3. No match → legacy chip.

A regex that fails to compile (`(unbalanced`, etc.) is silently
skipped at the dispatch site so a typo doesn't crash the whole
chat. The compiler does **not** validate regex compilability - guard
client-side.

**Rollback.** The block lives in Removing the file
cleanly drops the field from `UIBlock` (the import is wrapped in
`try / except ImportError`) - every tool falls back to its legacy
chip. Client-side: web has a single `<ToolRendererOrLegacy>` adapter
in `tool-call-block.tsx`; the chat client has a single `_renderToolEntry`
method in `chat_bubbles.dart`. Replacing those
two adapters back to the original `<ToolCallRow>` / `_buildToolContent`
calls is the full client-side rollback.

**Common gotchas.**

- **Long renderers compete with the message column.** The widget
  card renders inline in the chat timeline at message-column width
  (`maxWidth: 720` on web, `800` on the chat client desktop). Trees taller
  than ~160 px push the rest of the conversation down - keep them
  card-sized.
- **`{{tool.result}}` is JSON-dumped raw.** No prettification, no
  syntax highlighting. Use `markdown` nodes with explicit triple
  backticks if you want code formatting (`"```json\n{{tool.result}}\n```"`).
- **Streaming tools fire with `status: running` first.** The widget
  receives an empty `result` and `error` until the daemon reports
  completion. Render a placeholder when `tool.status` is `"running"`
  to avoid showing `null` in the body.

## Recipes

### Lovable-clone

```yaml
ui:
  layout: lovable
  density: compact
  thinking: { visible: false }
  tool_calls: { collapsed_default: true, show_silent: false }
  composer: { file_upload: true, voice: false, quick_prompts_visible: false }
  workspace:
    render_mode: react
    position: right
    width_pct: 65
    auto_open_on_first_tool: true
  visual:
    accent: "#10b981"
    bubble_style: flat
```

### Minimal conversational

```yaml
ui:
  layout: minimal
  thinking: { visible: false }
  tool_calls: { collapsed_default: true }
  composer: { voice: false }
  workspace: { position: hidden }
  visual: { bubble_style: minimal }
```

### Research / long-form

```yaml
ui:
  layout: research
  density: comfortable
  thinking: { visible: true, collapsed_default: false }
  tool_calls: { collapsed_default: true, show_silent: true }
  workspace: { position: bottom, width_pct: 40 }
```

### Coordinator / multi-agent (with Activity pane)

```yaml
ui:
  layout: research
  density: comfortable
  thinking: { collapsed_default: true }
  activity:
    enabled: true
    title: "Sub-agents"
    auto_open_on_spawn: true
    show_bg_tasks: true
    max_recent: 100
  workspace: { position: right, width_pct: 55 }
```

Surfaces the Activity pane the moment the coordinator spawns its
first sub-agent. The pane stays in sync across daemon restarts /
socket drops thanks to the resource protocol - no zombie pulsing
dots, no stale "running" rows.

## What the daemon doesn't read

The `ui:` block is **purely passed through** - the daemon's tool
dispatcher, security gates, and behavior engine all ignore it. No
canvas-side check uses `ui.features.tools_panel` to gate anything
server-side; the gating is the client's job.

That separation matters for trust: a malicious client can ignore
`ui.features.tools_panel: false` and show the panel anyway. The
real security boundary is `tools.capabilities`
([Security](11-security.md)) - `ui.features` is purely cosmetic.

## Cross-references

- App-config block reference for the `ui:` block:
  [App Configuration → ui](02-app-config.md#ui---display-layer-daemon-never-reads)
- Workspace renderer + preview proxy:
  [Workspace & Preview](41-preview.md)
- Declarative widget primitives + actions:
  [Widgets](42-widgets.md)
- Skills (server-side, distinct from `ui.slash_commands`):
  [Skills System](21-skills.md)
- Bundle namespaces (where `{{prompt.X}}` / `{{include:}}` come
  from): [Bundle namespaces](38-bundle-namespaces.md)
- Building a custom React UI inside the preview iframe:
  [Preview SDK](47-preview-sdk.md) - `<DigiPreview>` provider,
  `useWorkspaceFiles`, `useSessionMeta`, `useSessionLifecycle`,
  hidden `__sdk__/` namespace, host ↔ iframe protocol.
