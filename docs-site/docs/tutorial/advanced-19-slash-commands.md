---
id: advanced-19-slash-commands
title: "Advanced 19 - Slash commands: builtin dispatch and client templates"
sidebar_label: "Advanced 19: Slash commands"
---

Slash commands give the chat composer a `/<word>` palette
that fires either a **builtin** (dispatched server-side by
the daemon, no LLM call) or a **template** (rendered
client-side with variable substitution, sent to the agent
as a normal message). Both flavours are declared under
`ui.slash_commands:` in the same YAML list, picked apart by
the presence of an `action:` block.

## The two flavours at a glance

| Flavour | Declared with | Dispatched by | LLM cost | Persisted in history |
|---|---|---|---|---|
| Builtin | `action: {type: builtin, name: ...}` | Daemon HTTP layer | 0 (synthetic) | No (SSE only) |
| Template | `template: "...{{var}}..."` | Chat client (renders + sends rendered text) | 1 normal turn | Yes (as user + assistant message) |

The builtin handlers shipped today:
`help`, `compact_session`, `undo_session`. Template commands
do not require any Python change: they live entirely in the
YAML.

## The YAML

```yaml
app:
  app_id: tuto-slash-commands
  name: Tuto - Slash Commands
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: none
  max_turns: 4
  timeout: 60
  tool_injection: direct

agents:
  - id: main
    role: assistant
    brain:
      provider: openai
      backend: openai_compat
      model: gpt-5-mini
      config:
        api_key: placeholder
        base_url: https://api.openai.com/v1
      temperature: 0.5
      max_tokens: 1024
    system_prompt: |
      You are a friendly assistant. When asked to greet
      someone, respond with brief greetings in multiple
      languages. When asked to summarise a text, produce a
      one-paragraph summary.

tools:
  modules: {}
  capabilities:
    default_policy: auto
    max_risk_level: low

# Variables referenced by ``{{name}}`` / ``{{text}}`` in
# template slash commands MUST be declared here, otherwise
# the compiler rejects the YAML with
# "placeholder references undefined variable".
dev:
  variables:
    name: "there"
    text: ""

ui:
  slash_commands:
    # ── Builtins (server-side dispatch, no LLM call) ────────
    - command: /help
      description: List every command declared by this app.
      action:
        type: builtin
        name: help

    - command: /compact
      description: Trim older messages to free context window.
      action:
        type: builtin
        name: compact_session

    - command: /undo
      description: Undo the last turn (restore previous state).
      action:
        type: builtin
        name: undo_session

    # ── Templates (client-side fill, sent as user message) ─
    - command: /greet
      description: Greet someone in 3 languages.
      template: |
        Reply with a short greeting for {{name}} in three
        different languages. One line per language, format:
        `<language>: <greeting>`.

    - command: /summarise
      description: One-paragraph summary of a piece of text.
      template: |
        Summarise the following text in one paragraph
        (5-8 sentences max), keeping the most important
        facts:

        {{text}}
```

Three things to know:

- **Path matters.** `slash_commands:` lives under `ui:`,
  not under `runtime:` or `app:`. Putting it elsewhere
  silently registers nothing.
- **Variables must be declared.** Any `{{var}}` in a
  template must have a matching key in `dev.variables`. The
  declared value is the default; the chat client supplies
  the real value when the user fills the form. Without the
  declaration the compiler rejects the YAML with
  `placeholder '{{var}}' references undefined variable`.
- **Variable values are strings.** `dev.variables.name: "there"`
  works; `name: {description: ..., default: ...}` fails with
  *"schema error at 'dev.variables.name': Input should be a
  valid string"*.

## Deploy and verify the palette

```bash
digitorn dev deploy tuto-slash-commands.yaml
```

Inspect the registered commands via the app summary:

```bash
curl -sS -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/apps/tuto-slash-commands \
  | jq '.data.slash_commands[]|"\(.command) - \(.description)"'
```

Captured output:

```text
"/help - List every command declared by this app."
"/compact - Trim older messages to free context window."
"/undo - Undo the last turn (restore previous state)."
"/greet - Greet someone in 3 languages."
"/summarise - One-paragraph summary of a piece of text."
```

## Sample flow: builtin `/help` (server-side dispatch)

Send the slash command as a plain user message; the daemon
intercepts it before the agent loop:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "/help", "queue_mode": "async"}' \
  http://127.0.0.1:8000/api/apps/tuto-slash-commands/sessions/<sid>/messages
```

Response time: ~20 milliseconds. No LLM call. The handler's
response (the formatted help text listing every declared
command) streams via the `token` SSE channel so the chat UI
renders it like a normal assistant message. The text is NOT
persisted in the session message history (builtins are
stateless UI sugar).

## Sample flow: template `/greet` (client-side fill)

When the user types `/greet Paul` in the chat composer,
the client looks up the template, fills `{{name}}` with
`"Paul"`, and POSTs the rendered string as a regular user
message. The daemon never sees the slash form.

Simulating that from the API:

```bash
filled='Reply with a short greeting for Paul in three different languages. One line per language, format: `<language>: <greeting>`.'

curl -sS -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"message\": $(printf '%s' "$filled" | jq -Rs '.'), \"queue_mode\": \"async\"}" \
  http://127.0.0.1:8000/api/apps/tuto-slash-commands/sessions/<sid>/messages
```

The agent runs a normal turn and replies:

```text
[user] Reply with a short greeting for Paul in three
       different languages. One line per language, format:
       `<language>: <greeting>`.

[assistant]
       English: Hi Paul!
       Spanish: ¡Hola, Paul!
       French: Salut Paul!
```

Both the user and assistant turns are persisted in the
session message history, exactly like any other turn.

## When to reach for each flavour

**Builtin** when the action does not need the LLM at all:
help text, session housekeeping (`/compact`, `/undo`),
state inspection, manual `/abort`-style controls. Cheap and
fast (no model call), limited to handlers shipped in the
daemon.

**Template** when the LLM is doing the work but the prompt
follows a stable pattern: `/commit <msg>`, `/translate <lang> <text>`,
`/code-review <file>`, `/explain <symbol>`. The client
form-fills the variables before sending; the agent gets a
clean instruction without the user typing the boilerplate.

The two flavours are independent: a command cannot be both
a builtin and a template. If you give it an `action:` block,
the daemon runs the builtin and ignores any `template:` field.

## Going further

- Add a custom builtin: implement an async handler in the
  daemon's slash dispatch and declare it in your YAML.
- Combine with skills: declare an app skill under
  `skills:` and invoke it via the framework's
  `/use_skill <name>` shortcut (different mechanism, see
  [Advanced 2](advanced-02-bundle-skills.md)).
- Let users author their own templates per-app: set
  `dev.allow_user_skills: true` so the chat composer's
  skill picker stores user-defined entries in
  `user_skills` (per-user, per-app).
