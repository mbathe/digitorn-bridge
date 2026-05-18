---
id: advanced-02-bundle
title: "Advanced 2 - Bundle namespaces and skills"
sidebar_label: "Advanced 2: Bundles"
---

The previous tutorials kept the entire app in one YAML. That's
fine until the system prompt grows past a few paragraphs, or you
want reusable workflows triggered by slash commands.

A Digitorn **bundle** is a directory: the `app.yaml` plus
structured siblings (prompts, skills, behavior profiles, fragments,
assets, widgets). The compiler resolves cross-references at compile
time using template namespaces like `{{prompt.X}}` and
`{{skill.X}}`, and the slash command system exposes user-invokable
recipes the agent loads on demand.

## Bundle layout

```text
bundle-bot/
├── app.yaml           # the canonical 8-block YAML
├── prompts/
│   └── system.md      # referenced as {{prompt.system}}
├── skills/
│   └── review.md      # exposed as the `/review` slash command
├── behavior/          # optional, {{behavior.X}}
├── fragments/         # optional, {{include:fragments/X.yaml}}
└── assets/            # optional, {{asset.X}} for client URLs
```

The compiler walks the bundle once. Anywhere a `{{prompt.X}}` or
`{{skill.X}}` placeholder appears in the YAML, it gets substituted
with the file's content **before** Pydantic validates the schema.
The runtime never sees the placeholder, only the resolved string.

## prompts/system.md

```markdown
You are a careful code-review assistant.
Reply concisely. Cite specific lines when you reference code.
When the user invokes /review, follow the procedure described
in the review skill that is loaded into your system context.
When in doubt, ask one clarifying question.
```

A 1.5 KB system prompt would be unreadable inline; this is one
file, four lines, easy to evolve. Locale variants
(`system.fr.md`, `system.es.md`) are picked automatically when
the daemon's compile-time locale is set, falling back to the
default on miss.

## skills/review.md

```markdown
# /review skill

When invoked, perform this procedure in order:
1. Identify the language and the obvious purpose of the snippet.
2. Flag any clear correctness bug (off-by-one, null deref, missing await).
3. Flag any security concern (SQL injection, path traversal, command injection).
4. Suggest one concrete improvement.
5. Output a single overall rating: A, B, or C.

Always end your reply with one line:
RATING: <A|B|C>
```

A skill is a free-form markdown procedure. The user invokes it
with `/review`; the daemon resolves the slash command, prepends
the file content to the agent's system context for that turn,
and the agent follows the steps.

## app.yaml

```yaml
app:
  app_id: bundle-bot
  name: Bundle Bot
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 4
  timeout: 60

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
      max_tokens: 400
    system_prompt: "{{prompt.system}}"        # ← loaded from prompts/system.md

tools:
  modules:
    memory: {}
  capabilities:
    default_policy: auto

dev:
  skills:
    - command: /review
      description: "Run a code review on the snippet"
      path: skills/review.md                  # ← exposed as /review
```

Three things happen at compile time:

- `{{prompt.system}}` is replaced with the verbatim content of
  `prompts/system.md`. The system prompt the LLM sees is the
  expanded version, not the placeholder.
- The `dev.skills` list registers `/review` with the bundled
  procedure. The the chat client / web client's slash menu picks this up
  and shows it.
- The compiler walks for any other `{{namespace.X}}` references;
  the alias pass and Pydantic validate the resolved YAML.

## Live transcript

The user types a slash command followed by a code snippet. Real
transcript.

```text
> /review

  function parseAge(s) {
    return parseInt(s);
  }

Here's my review of the snippet:

**1. Language & Purpose:** JavaScript - a small function
`parseAge` that attempts to parse a string into an integer
(presumably an age).

**2. Correctness Bug:** `parseInt(s)` will happily return `NaN`
if `s` is not a valid number (e.g., `"abc"`, `null`, `undefined`).
The function silently returns `NaN` with no indication of
failure. For an age, this is likely a bug - callers won't know
the parse failed.

**3. Security Concern:** None directly - `parseInt` is safe from
injection. However, if the result is used in a database query
or comparison without validation, a `NaN` could cause unexpected
behavior.

**4. Concrete Improvement:** Validate the input and return a
sensible default or throw an error:

    function parseAge(s) {
      const age = parseInt(s, 10);
      if (isNaN(age) || age < 0 || age > 150) return null;
      return age;
    }

Key changes:
- Always pass radix `10` to `parseInt` (avoids legacy octal behavior).
- Check for `NaN` and out-of-range values.
- Return `null` (or throw) on failure instead of `NaN`.

**RATING: B**
```

The agent followed every step from `skills/review.md` in order,
ended with the canonical `RATING: B` line, and stayed within the
terse-but-cite-lines guidance from `prompts/system.md`. Both
files were resolved at compile time; neither shows up as a
placeholder in the runtime prompt.

## The six namespaces

| Pattern              | Folder        | Resolves to                                                                |
|----------------------|---------------|----------------------------------------------------------------------------|
| `{{prompt.X}}`       | `prompts/X.md`    | File content (raw markdown)                                            |
| `{{skill.X}}`        | `skills/X.md`     | File content (also exposed as a slash command via `dev.skills`)        |
| `{{behavior.X}}`     | `behavior/X.yaml` | Parsed YAML profile loaded by the [behaviour engine](../language/43-behavior.md) |
| `{{asset.X}}`        | `assets/X`        | URL `/api/apps/<app_id>/assets/<X>` for client fetching                |
| `{{asset_b64.X}}`    | `assets/X`        | `data:<mime>;base64,…` data URI (small files only)                     |
| `{{include:path}}`   | `<bundle>/path`   | Parsed YAML fragment inlined into the parent structure                 |

`{{asset.X}}` is the right choice for icons, logos, screenshots
the client renders. `{{asset_b64.X}}` is for files small enough
to inline; the compiler refuses anything > 64 KB.

## Locale-aware prompts

The compiler accepts a `locale=` flag. When set, the resolver
prefers `prompts/system.<locale>.md` over `prompts/system.md`.
Drop a French translation in `system.fr.md` and the same YAML
serves both audiences without templating logic in the prompt.

## Auto-loaded directories

The compiler also scans two well-known directories without an
explicit `include:` reference:

- `widgets/*.yaml` are merged into `ui.widgets.inline`, keyed by
  file stem. Drop `widgets/source_card.yaml` in the bundle and
  the agent can `widget.render(zone="inline", ref="source_card")`.
- `agents/*.yaml` are merged into the top-level `agents:` list.
  One specialist per file keeps multi-agent apps readable.

This is documented in detail in
[Bundle namespaces](../language/38-bundle-namespaces.md).

## Editing the bundle while the daemon runs

The compiler watches the bundle directory and **hot-reloads** on
file changes. Save a new `prompts/system.md`, and the next agent
turn picks it up without redeploying. The same applies to skills
and widgets.

This shortens the iteration loop on prompt engineering. There's no
"redeploy" round-trip - edit the file, send a message, see the
new behaviour.

## When to use a bundle

- The system prompt is **longer than 30 lines** and you want to
  edit it as a markdown file with version control.
- You ship **slash commands** as user-facing recipes
  (`/commit`, `/review`, `/scaffold`).
- You distribute the app as a **package** the user installs;
  packaging assumes the bundle directory layout already.
- You **localise** the app and want translations in their own
  files instead of an in-YAML lookup table.

For one-off scripts or examples in docs, the inline form is
cleaner. For anything you maintain over time, the bundle is the
canonical shape.

## Going further

- Full bundle documentation:
  [Bundle namespaces](../language/38-bundle-namespaces.md).
- Skill system surface (slash command syntax, frontmatter
  metadata, version checks, RAG-backed help):
  [Skills](../language/21-skills.md).
- Behavior profile namespace and the engine that consumes it:
  [Behavior Engine](../language/43-behavior.md).
