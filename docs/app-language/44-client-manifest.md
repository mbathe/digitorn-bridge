---
id: client-manifest
---

# Client Manifest Contract

The Flutter / web client reads the deployed app's YAML (via
`GET /api/apps/{app_id}`) and uses specific top-level blocks to tailor
its UI: which empty-state greeting to show, which panels to hide,
which accent colour to paint, which `/slash` commands to suggest.

This page documents every block the client parses today, what each
one controls, and what the defaults are when a block is omitted.

> **Rule of thumb**: blocks in this doc change what the USER sees;
> blocks everywhere else in the app-language docs change what the
> AGENT does. The two sets are disjoint — mixing them is not an
> error, just unnecessary noise.

## Where each block lives

| Block | Level | What it controls |
|---|---|---|
| `app:` | top-level | App identity, icon, colour, empty-state chips |
| `execution.workspace_mode` | under `execution:` | Workspace toggle visibility + auto-open |
| `execution.greeting` | under `execution:` | Empty-state large text |
| `features:` | top-level OR `app.features:` | Which chat UI panels are visible |
| `theme:` | top-level | Accent / background colour overrides |
| `slash_commands:` | top-level | Custom `/command` palette entries |
| `capabilities:` | top-level | Which module actions the agent may run |
| `modules:` | top-level | Which modules are loaded |

> **Nesting compat**: the client accepts `features:` and `theme:`
> both at the top level and nested under `app:`. The daemon merges
> both — top-level wins on conflict.

---

## 1. `app:` — identity & empty state

```yaml
app:
  app_id: digitorn-chat            # required — API/socket routing
  name: "Digitorn Chat"            # header + empty-state title
  version: "1.0"
  description: "…"                 # marketplace card
  icon: "💬"                       # emoji badge (header + empty state)
  color: "#4f8cff"                 # accent (badges, chips, pressure bar)
  category: "general"              # marketplace filter
  author: "Digitorn"
  tags: [chat, assistant]
  quick_prompts:                   # clickable chips in the empty state
    - label: "Explain something"
      icon: "💡"
      message: "Explain the following concept in simple terms:"
    - label: "Search the web"
      icon: "🔍"
      message: "Search for:"
```

| Field | Type | Required | Default | Client effect |
|---|---|---|---|---|
| `app_id` | string | **yes** | — | Routes all API + Socket.IO calls |
| `name` | string | **yes** | — | Header title, empty-state heading |
| `icon` | string | no | `""` | Emoji or URL in header/empty state |
| `color` | hex | no | `""` | Accent across UI |
| `category` | string | no | `"general"` | Marketplace grouping |
| `quick_prompts` | list | no | `[]` | Chips in empty state — empty = just input field |
| `tags` | list | no | `[]` | Marketplace filter tags |

---

## 2. `execution:` — runtime + workspace

```yaml
execution:
  mode: conversation               # conversation | background | one_shot
  max_turns: 20
  timeout: 120
  workspace_mode: auto             # none | optional | required | auto
  greeting: |
    Hello! I'm Digitorn Chat.
    What can I do for you?
```

### `execution.mode`

The **three** modes surfaced to the chat UI:

| Mode | Use case |
|---|---|
| `conversation` | Classic chat — the user sends messages, the agent replies turn by turn. |
| `background` | Driven by triggers (cron, webhook, file watcher …). No chat UI needed, though a read-only activity view is shown. |
| `one_shot` | Single request → single response. Good for pipelines. |

(A fourth mode, `pipeline`, exists in the daemon schema for chained-app composition. It is not a chat UI mode — the client treats pipeline apps like `one_shot`.)

### `execution.workspace_mode`

Controls whether the file-workspace panel appears:

| Value | Effect |
|---|---|
| `none` | Workspace completely hidden (no toggle, no auto-open) |
| `optional` | Toggle visible, closed by default |
| `required` | Forced open + "select workspace" banner |
| `auto` *(default)* | Toggle visible, opens on first tool_call |

### `execution.greeting`

Multi-line string rendered as the large empty-state text before the user sends their first message.

---

## 3. `features:` — chat-UI feature toggles

```yaml
features:                # top-level (canonical)
  voice: false                     # hide the mic button
  attachments: false               # hide the paperclip / attach menu
  tools_panel: false               # hide the Tools button
  snippets: true
  tasks_panel: false               # hide Background tasks drawer
  memory_panel: false              # hide memory drawer
  context_ring: true               # pressure gauge
  markdown: true                   # fallback to plain text when false
  slash_commands: false            # disable "/" palette
  message_actions: true            # copy / retry / copy-md
  status_pills: true               # Live / Reconnecting / Interrupted
  token_badges: false              # per-message token footer
```

All keys default to **true** (feature visible). Set a key to `false` to hide its UI.

The block is accepted at the top level OR nested under `app:`:

```yaml
app:
  app_id: locked-chat
  features:
    voice: false
```

When both locations are present, the **top-level block wins** on conflict.

### Feature table

| Key | Default | Hides |
|---|---|---|
| `voice` | true | Microphone button |
| `attachments` | true | Paperclip / attach menu |
| `tools_panel` | true | "Tools" button |
| `snippets` | true | Snippets menu |
| `tasks_panel` | true | Background tasks drawer |
| `memory_panel` | true | Memory drawer |
| `context_ring` | true | Context-pressure gauge |
| `markdown` | true | Rich text rendering (false = plain text) |
| `slash_commands` | true | `/` palette |
| `message_actions` | true | Copy / retry / copy-markdown |
| `status_pills` | true | Live / Reconnecting / Interrupted pills |
| `token_badges` | true | Per-message token footer |

---

## 4. `theme:` — colour overrides

```yaml
theme:
  accent: "#6EE7B7"                # overrides app.color
  background: "#0B1220"            # reserved (client-side, not yet used)
```

Narrow hook for cases where the marketplace `app.color` hue is correct
but the chat surface needs a different shade (e.g. dark-mode tuning).

---

## 5. `slash_commands:` — command palette (parsed, rendering in phase 2)

```yaml
slash_commands:
  - command: deploy
    description: "Deploy to an environment"
    template: "Deploy to {env}"
```

Each entry: `{command, description, template}`. The daemon parses them; the Flutter client will surface them in the `/` palette in a future release. Safe to include today — the schema is stable.

---

## 6. `capabilities:` — permissions & drawer

```yaml
capabilities:
  default_policy: auto              # auto | prompt | deny
  grant:
    - module: memory
      actions: [set_goal, remember, task_create, task_update]
    - module: web
      actions: [search, fetch, extract]
    - module: context_builder
      actions: [ask_user]
```

The client reads `capabilities.grant` to decide which modules surface in the "Tools / Capabilities" drawer. The daemon enforces the same list at runtime. See [Security](11-security.md).

---

## 7. `modules:` — loaded modules

```yaml
modules:
  memory:
    config:
      auto_remember: true
  web: {}
  context_builder: {}
```

The client extracts only the **names** for the capabilities drawer. Accepted as a map (preferred) or a list (`modules: [memory, web, context_builder]`). Module configuration is read by the daemon, ignored by the client.

---

## Ignored by the client (parsed by the daemon only)

These blocks never affect the UI. The client reads over them without error. They stay in the YAML because the daemon needs them.

- `agents[]` — LLM config (brain, model, system_prompt, context, fallback)
- `agents[].system_prompt` — prompt content
- `modules[].config` — per-module internal configuration
- `middleware`, `hooks`, `behavior`, `skills`, `pipeline`, `triggers` — daemon-side logic

---

## Minimal working example

```yaml
app:
  app_id: my-app
  name: "My App"
  icon: "🚀"
  color: "#ff6b6b"

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config: { api_key: "claude-code" }
    system_prompt: "You are a helpful assistant."

execution:
  workspace_mode: none
  greeting: "Hello!"
```

Everything else defaults: `features` all true, `quick_prompts` empty, no workspace, `mode: conversation` (the schema default).

## Locked-down chat example

```yaml
app:
  app_id: simple-chat
  name: "Simple Chat"
  icon: "💭"
  color: "#8b5cf6"

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config: { api_key: "claude-code" }
    system_prompt: "Be concise."

execution:
  workspace_mode: none
  greeting: "Ask me anything."

features:
  attachments: false
  tools_panel: false
  snippets: false
  tasks_panel: false
  memory_panel: false
  slash_commands: false
  # voice, markdown, message_actions, status_pills, context_ring, token_badges stay true
```

Result: an ultra-clean chat surface with only the microphone, the input field, and the send button.

---

## Multi-tenant awareness

The `/api/apps` response also carries `scope` (`"system"` | `"user"`) and
`owner_user_id` (`""` or a user id). Render a badge so users know which
version of an app they're looking at:

- `scope="system"` → 🌐 **System** (installed globally by an admin)
- `scope="user"`, `owner_user_id == <me>` → 👤 **Private** (your install)
- `scope="user"`, `owner_user_id != <me>` (admin view only) → 👤 **Private (alice)**

Full semantics (deploy flow, scope-aware delete/disable/enable, per-user
vs system isolation, admin override query params) are documented in
[Multi-Tenant Installs](45-multi-tenant.md).

---

## Where the data lives in the daemon response

`GET /api/apps/{app_id}` returns every block above in a single envelope:

```jsonc
{
  "success": true,
  "data": {
    "app_id": "my-app",
    "scope": "system",              // "system" | "user"
    "owner_user_id": "",            // "" for system, "<uid>" for user-scoped
    "name": "My App",
    "icon": "🚀",
    "color": "#ff6b6b",
    "category": "general",
    "quick_prompts": [...],
    "greeting": "Hello!",
    "workspace_mode": "none",
    "features": { ... },
    "theme": { ... },
    "slash_commands": [...],
    "capabilities": null,           // merged from the app's security profile
    "modules": ["memory", "web"],
    "agents": ["main"],
    "mode": "conversation",
    "trigger_types": []
  }
}
```

The client can rely on `features`, `theme`, `slash_commands`, `scope`, and `owner_user_id` being present (with empty defaults) — no need to guard with null checks.

---

## Planned extensions (schema reserved, not parsed yet)

- `workspace.panels[]` — named sub-panels inside the workspace (e.g. file tree + preview + logs)
- `chat.slash_commands[]` — structured slash command argument schemas
- `approvals[]` — declarative approval flows for high-risk tool calls

These names are reserved in the schema namespace. Feel free to draft them in YAML today; the client will pick them up when the implementation lands.
