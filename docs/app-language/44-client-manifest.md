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
| `ui.features` | 11 boolean toggles for individual UI panels / behaviours. |
| `ui.greeting` | Empty-state greeting under the input box. |
| `ui.slash_commands` | The `/`-palette entries. |
| `ui.quick_prompts` | Same shape as `app.quick_prompts`; client merges both lists. |
| `ui.workspace` | Renderer hint (`render_mode`, `entry_file`, `title`). Drives which viewer the client opens. |
| `ui.preview` | When set, the client embeds the proxied dev server in an iframe panel. |
| `ui.widgets` | Declarative widget tree rendered in chat, sidebar, modals. |

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

## `ui.workspace` — renderer hint

`UIBlock.workspace` (`schema.py:2561`) is `WorkspaceBlock`
(`schema.py:2717`). Tells the client this app uses the in-memory
virtual filesystem and which viewer to open.

Documented in [Workspace & Preview](41-preview.md). Three fields:
`render_mode` (8 values), `entry_file` (default file to open),
`title` (toolbar label).

## `ui.preview` — embedded dev server

`UIBlock.preview` (`schema.py:2570`) is `PreviewConfig`
(`schema.py:2757`). When set, the client embeds the daemon's
proxied dev server in an iframe panel. The client polls
`/api/apps/<app_id>/preview-server/status` to know when the server
is ready.

Documented in [Workspace & Preview](41-preview.md). 10 fields
(command, port, env, install_command, health_path, ...).

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
