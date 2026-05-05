---
id: client-manifest
---

# Client Manifest

The Flutter / web client reads the deployed app's YAML (via
`GET /api/apps/{app_id}`) and uses the **`ui:` block** plus a
handful of `app:` and `runtime:` fields to tailor its UI: which
greeting to show, which panels to hide, which accent colour to
paint, which `/slash` palette to render.

This page documents what the client actually consumes — the
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
| `ui.workspace` | Renderer hint + layout (`render_mode`, `entry_file`, `title`, `position`, `width_pct`, `auto_open_on_first_tool`). |
| `ui.widgets` | Declarative widget tree rendered in chat, sidebar, modals. |
| `ui.layout` | High-level chat preset (`default`, `code`, `builder`, `research`, `minimal`, `lovable`). |
| `ui.density` | Bubble spacing (`compact` / `comfortable`). |
| `ui.thinking` | Thinking-block visibility and initial collapsed state. |
| `ui.tool_calls` | Tool-chip collapse default and silent-tools visibility. |
| `ui.composer` | Composer toolbar (file upload, voice, slash, quick prompts). Wins over the matching `ui.features.X` keys. |
| `ui.visual` | Bubble accent / style / user alignment. |

The first three groups are covered in
[App Configuration → app](02-app-config.md#app--identity) and
[App Configuration → runtime](02-app-config.md#runtime--lifecycle-and-execution-policy);
this page focuses on the `ui:` block.

## `ui.features` — 12 toggles

`AppMeta.features` (`schema.py:84`) and `UIBlock.features`
(`schema.py:2550`) are mirror dicts that share the same key set:

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

Source of truth: the docstring at `schema.py:88-90` lists the
exact keys the Flutter client recognises today. Unknown keys are
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

> **Mirror.** `app.features` (`schema.py:84`) is a deprecated
> nesting that the compiler still accepts — it lifts to
> `ui.features` via the alias pass. Set `ui.features` directly
> in v2 YAML; the compiler emits a warning when you use the
> nested form.

## `ui.theme` — accent + background

`UIBlock.theme` (`schema.py:2543`). Two recognised keys:

```yaml
ui:
  theme:
    accent: "#6EE7B7"        # hex; overrides app.color for fine control
    background: "#0F172A"    # hex; client may apply this to the chat surface
```

Other keys are passed through but unused by the current Flutter /
web clients. Treat `theme` as a forward-compat dict — only `accent`
and `background` are guaranteed.

`app.color` (`schema.py:57`) is the **catalog** accent (visible on
the app card). `ui.theme.accent` overrides it inside the app once
the user is in the conversation. Set them independently if you
want different colours in the catalog vs in the chat.

## `ui.greeting` — empty-state message

`UIBlock.greeting` (`schema.py:2582`). The text shown above the
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

## `ui.slash_commands` — `/` palette

`UIBlock.slash_commands` (`schema.py:2574`). List of `SlashCommand`
(`typed_models.py:81`, `extra: allow`).

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
pure UI sugar — the agent never knows the slash palette existed,
it just sees the rendered `template` as a normal user message.

See [Skills System](21-skills.md) for the difference + the skills
that DO live server-side.

## `ui.quick_prompts` — empty-state buttons

`UIBlock.quick_prompts` (`schema.py:2578`) — list of `QuickPrompt`
(`typed_models.py:26`, `extra: allow`).

```yaml
ui:
  quick_prompts:
    - label: "New PR"
      message: "Open a PR with the latest changes"
      icon: "🚀"
    - label: "Daily standup"
      message: "Summarize what I did yesterday"
      icon: "📋"
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `label` | string (min 1) | yes | Short button label. |
| `message` | string (min 1) | yes | Full prompt sent when the user clicks. |
| `icon` | string | no (default `""`) | Emoji or icon name. |

Mirror: `app.quick_prompts` (`schema.py:72`) holds the same shape.
The client **merges** both lists, deduping by `label`. Either is
fine; pick one place per app for clarity.

## `ui.workspace` — renderer hint + layout

`UIBlock.workspace` is `WorkspaceBlock` (`schema.py`,
`extra: forbid`). Tells the client this app uses the in-memory
virtual filesystem and how to position the viewer relative to the
chat.

Renderer fields (documented in [Workspace & Preview](41-preview.md)):

- `render_mode: str` (default `"auto"`) — `react`, `html`,
  `markdown`, `slides`, `code`, `latex`, `builder`, or `auto`. Auto
  detects from the first file the agent writes.
- `entry_file: str | null` — default file the renderer opens.
- `title: str | null` — workspace toolbar label.

Layout fields (added 2026-05-04, drive how the chat ↔ workspace
split looks):

- `position: str` (default `"right"`) — `right`, `bottom`,
  `hidden`, or `overlay`. `hidden` keeps the workspace off-screen
  even when files are written; `overlay` floats it over the chat.
- `width_pct: int` (default `50`, range `10..90`) — pane width as
  a percentage of the chat-vs-workspace split. Ignored when
  `position` is `hidden` / `overlay`.
- `auto_open_on_first_tool: bool` (default `false`) — when
  `true`, the client opens the workspace pane the first time the
  agent writes a file. Useful for Lovable-style apps where the
  user lands on a chat-only screen and discovers the workspace
  the moment the agent generates code.

```yaml
ui:
  workspace:
    render_mode: react
    entry_file: src/App.tsx
    title: My App
    position: right
    width_pct: 65
    auto_open_on_first_tool: true
```

## `ui.widgets` — declarative widget tree

`UIBlock.widgets` (`schema.py:2557`) is `WidgetsConfig`
(`schema.py:3019`). Four sub-trees:

| Field | Type | Description |
|-------|------|-------------|
| `version` | int | Spec version. Daemon refuses unknown versions; only `1` today. |
| `chat_side` | `ChatSideWidget \| null` | Right-side panel rendered alongside the chat. |
| `workspace_tabs` | list[`WorkspaceTabWidget`] | Tabs in the workspace panel. |
| `modals` | dict[name, `ModalWidget`] | Named modals the agent can open via `widget.open` action. |
| `inline` | dict[name, `InlineWidget`] | Inline widgets the agent renders inside chat via `widget.render` with a `ref:`. |

Full surface — 43 widget primitives, 15 actions, server-side
template substitution, live `widget:*` Socket.IO events — is in
[Widgets](42-widgets.md). External widget files under
`./widgets/*.yaml` in the bundle dir are auto-loaded into
`inline` by the compiler (keyed by file stem, same pattern as
skills).

## `ui.layout` — high-level chat preset (2026-05-04)

`UIBlock.layout` is a `str` with default `"default"`. Allowed
values:

- `default` — historical conversational chat.
- `code` — code-editor-friendly chat (Cursor-style).
- `builder` — YAML-editor + smoke-test focus (digitorn-builder).
- `research` — long-form, citations and agent-group prominent.
- `minimal` — chat only, no workspace, terse chrome.
- `lovable` — workspace-dominant split with auto-open on first
  tool call.

The preset is a sugar layer: when the YAML omits a fine-grained
sub-block (`thinking`, `tool_calls`, `composer`, `visual`,
`workspace`), the client uses the preset's defaults. Any
sub-block the YAML DOES define ALWAYS wins over the preset, so
deriving from `lovable` and tweaking just `workspace.width_pct`
is supported.

## `ui.density` — bubble spacing (2026-05-04)

`UIBlock.density: str`, default `"comfortable"`. Allowed:
`compact`, `comfortable`. Applies to message bubbles and the
gap between consecutive messages.

## `ui.thinking` — thinking-block defaults (2026-05-04)

`UIBlock.thinking` is `ChatThinkingBlock` (`schema.py`,
`extra: forbid`). Two flags:

- `visible: bool` (default `true`) — when `false`, thinking
  blocks are hidden entirely. The agent can still emit them, the
  client just drops them at render time.
- `collapsed_default: bool` (default `true`) — initial collapsed
  state when `visible` is `true`. The user can still toggle.

```yaml
ui:
  thinking:
    visible: false           # production conversational app
```

## `ui.tool_calls` — tool-chip defaults (2026-05-04)

`UIBlock.tool_calls` is `ChatToolCallsBlock` (`schema.py`,
`extra: forbid`). Two flags:

- `collapsed_default: bool` (default `true`) — initial collapsed
  state of every tool-call chip. The user can expand individual
  chips with the chevron.
- `show_silent: bool` (default `false`) — when `true`, plumbing
  tools (`memory.remember`, `agent_spawn` internals, discovery
  meta-tools like `search_tools` / `list_categories`) are
  rendered. Default `false` keeps them hidden so the chat reads
  as a clean conversation rather than an internals trace.

```yaml
ui:
  tool_calls:
    collapsed_default: true
    show_silent: false
```

## `ui.composer` — composer toolbar (2026-05-04)

`UIBlock.composer` is `ChatComposerBlock` (`schema.py`,
`extra: forbid`). Mirrors the legacy `ui.features` flags for the
same concepts; when both are present the typed `composer.X` wins.

- `file_upload: bool` (default `true`) — paperclip / drag-drop
  attachment. Equivalent to `features.attachments`.
- `voice: bool` (default `false`) — microphone button. Default
  `false` here (opt-in for production privacy) vs `features.voice`
  which historically defaulted to `true`.
- `slash_commands: bool` (default `true`) — `/`-palette popup.
  Equivalent to `features.slash_commands`.
- `quick_prompts_visible: bool` (default `true`) — suggested
  prompt chips above the composer when the conversation is empty.

```yaml
ui:
  composer:
    file_upload: true
    voice: false
    slash_commands: true
    quick_prompts_visible: true
```

## `ui.visual` — bubble accent / style (2026-05-04)

`UIBlock.visual` is `ChatVisualBlock` (`schema.py`,
`extra: forbid`). Three knobs:

- `accent: str` (hex, default `""`) — accent colour for the
  send button, cursor, and any per-app highlights. Fallback
  chain: `visual.accent` → `theme.accent` → `app.color`. Empty
  here means "use the next level".
- `bubble_style: str` (default `"card"`) — `card` (rounded box
  with shadow), `flat` (filled background no shadow), or
  `minimal` (no background, just text + thin separator).
- `user_bubble_alignment: str` (default `"right"`) — `right`
  (default chat-room layout) or `left` (RTL or stacked layout
  variants).

```yaml
ui:
  visual:
    accent: "#10b981"
    bubble_style: flat
    user_bubble_alignment: right
```

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

## What the daemon doesn't read

The `ui:` block is **purely passed through** — the daemon's tool
dispatcher, security gates, and behavior engine all ignore it. No
canvas-side check uses `ui.features.tools_panel` to gate anything
server-side; the gating is the client's job.

That separation matters for trust: a malicious client can ignore
`ui.features.tools_panel: false` and show the panel anyway. The
real security boundary is `tools.capabilities`
([Security](11-security.md)) — `ui.features` is purely cosmetic.

## Cross-references

- App-config block reference for the `ui:` block:
  [App Configuration → ui](02-app-config.md#ui--display-layer-daemon-never-reads)
- Workspace renderer + preview proxy:
  [Workspace & Preview](41-preview.md)
- Declarative widget primitives + actions:
  [Widgets](42-widgets.md)
- Skills (server-side, distinct from `ui.slash_commands`):
  [Skills System](21-skills.md)
- Bundle namespaces (where `{{prompt.X}}` / `{{include:}}` come
  from): [Bundle namespaces](38-bundle-namespaces.md)
