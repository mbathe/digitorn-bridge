---
id: tutorial-06-ui
title: "6. UI surfaces"
sidebar_label: "6. UI surfaces"
---

So far the agent has been a chat-only thing. In this step you give
it a **virtual workspace** - a side pane the user sees update in
real time as the agent writes files. No real disk required: the
files live in memory, stream over Socket.IO, and render in the
client.

Two new pieces:

- The `workspace` module exposes six file-like tools (`WsWrite`,
  `WsRead`, `WsEdit`, `WsGlob`, `WsGrep`, `WsDelete`).
- The `ui.workspace` block tells the client how to render the
  pane (renderer mode, entry file, position, width).

This is the same shape used by the in-product builder, the React
sandbox, and Lovable-style live-preview apps.

## Prerequisites

Same daemon and credential as the previous tutorials.

## The YAML

Save this as `workspace-bot.yaml`:

```yaml
app:
  app_id: workspace-bot
  name: Workspace Bot
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 6
  timeout: 90

agents:
  - id: main
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      credential:
        ref: deepseek_main
        scope: per_user
        provider: deepseek
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: https://api.deepseek.com/v1
      temperature: 0
      max_tokens: 512
    system_prompt: |
      You write files into a virtual workspace. Use WsWrite to
      create files. The user never has shell access; everything
      they see is what you stream into the workspace. After every
      file you write, reply with one short confirmation line
      naming the file you wrote.

tools:
  modules:
    workspace:
      config:
        render_mode: code
        entry_file: README.md
        title: Workspace Bot
        sync_to_disk: false
        lint: false
    preview: {}
  capabilities:
    default_policy: block
    grant:
      - module: workspace
        actions: [write, read, edit, glob, grep, delete]
      - module: preview
        actions: [set_state, get_state]

ui:
  workspace:
    render_mode: code
    entry_file: README.md
    title: Workspace Bot
    position: right
    width_pct: 60
  greeting: "Ask me to scaffold a project; I'll write files into the workspace pane."
```

Three things changed vs the previous tutorials.

The `workspace` module replaces `filesystem`. Where filesystem
operates on the real disk, `workspace` operates on a virtual
in-memory tree streamed to the client over Socket.IO. Same set of
operations (read, write, edit, glob, grep, delete) but the prefix
is `Ws` (`WsWrite`, `WsRead`, …).

The capability block is **deny-by-default** with explicit grants.
This is the canonical production shape: name only the actions the
agent actually needs. Auto-policy is fine for tutorials but you
want something tighter for live apps.

The `ui.workspace` block tells the client to mount the pane on the
right at 60 % width with the `code` renderer (other choices: `html`
for live previews, `slides`, `markdown`, `latex`, `builder`,
`auto`).

## Deploy and chat

```bash
digitorn dev deploy workspace-bot.yaml
digitorn dev chat workspace-bot
```

When you chat in the daemon's UI, the workspace pane appears on
the right and the agent's writes stream live into it.

## Live transcript

Real one-turn session. The user asks the agent to scaffold a tiny
landing page; the agent fires one tool call (a single multi-write
WsWrite isn't supported - it does two consecutive writes in one
turn, which the runtime collapses into one `tool_calls_count: 1`
because both writes share the action invocation envelope) and
confirms.

```text
> Scaffold a tiny landing page: write README.md with a one-line
  intro, and index.html with a single <h1>Hello</h1>.

Done - wrote **README.md** (one-line intro) and **index.html**
(with `<h1>Hello</h1>`).
```

Right after the turn, the workspace API returns the files the
agent wrote:

```json
[
  { "path": "README.md",  "size": 36,  "lines": 3,  "language": "markdown", "validation": "pending" },
  { "path": "index.html", "size": 215, "lines": 11, "language": "html",     "validation": "pending" }
]
```

Fetching `README.md` from the workspace endpoint returns the
real content the agent wrote:

```text
# Landing Page

A tiny landing page.
```

And `index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Landing Page</title>
</head>
<body>
  <h1>Hello</h1>
</body>
</html>
```

Note `validation: "pending"`. The workspace ships with an
**approval gate**: every file the agent writes lands in a pending
state until the user (or an `auto_approve` flag) accepts it. The
client displays a side-by-side diff against the previous version
and lets the user approve or reject before the file becomes the
new baseline. Combined with the per-file edit history, this gives
the agent room to iterate without blowing away work the user
wants to keep.

## Other render modes

`render_mode` switches the client-side renderer:

| Mode      | What the client renders                                    |
|-----------|------------------------------------------------------------|
| `code`    | Tree + Monaco-style editor with syntax highlighting        |
| `html`    | Live `<iframe>` rendering of the workspace's `entry_file`  |
| `markdown`| Rendered preview of the entry file (e.g. `README.md`)      |
| `slides`  | Reveal.js-style slide deck from a markdown file            |
| `latex`   | Live PDF preview of a LaTeX bundle                         |
| `builder` | The Digitorn builder UI (custom YAML editor + form panels) |
| `auto`    | Pick `html` if a `dist/` exists, else `code`               |

Switch modes by changing two strings; the agent code stays the
same. The workspace module is the source of truth - the client
only renders.

## Adding declarative widgets

Workspace files are one surface; **widgets** are another. Where
the workspace is "files the agent writes", widgets are "form
panels, lists, modals" the agent declares in YAML and pushes to
the client live.

The simplest example - a status card the agent updates each turn:

```yaml
ui:
  widgets:
    inline:
      status_card:
        tree:
          type: card
          children:
            - { type: text, text: "Status: {{state.status}}" }
            - { type: text, text: "Last update: {{state.last_seen}}" }
```

The `{{state.X}}` placeholders resolve client-side from the
per-session widget state map; the agent writes into that map with
`widget.set_state`. There is no DOM templating, no JSX, no React
in the YAML; the client interprets the tree and renders native
primitives.

The full primitive vocabulary (43 of them - text, image, list,
form, input, button, modal, table, chart, …) lives in
[Widgets](../language/42-widgets.md) and the
[widget module reference](../reference/modules/widget.md).

## When to use which

- **Workspace** - the agent produces *files* (code, docs, slides,
  static sites). The user wants to see and approve those files.
- **Widgets** - the agent produces *interactive UI* (forms,
  pickers, status panels). The user clicks buttons in the
  pane that fire tool calls.
- **Both** - rare in small apps but common in real ones. A code
  editor app might use the workspace for files and widgets for
  the side panel showing test results, lint warnings, and a
  "deploy" button.

Next: [7. Deploying](07-deploying.md) - hardening, TLS,
credentials vault, and the production checklist.
