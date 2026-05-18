---
id: bundle-namespaces
title: "Bundle namespaces - compile-time filesystem injection"
---

# Bundle namespaces

An app isn't just one YAML file - it's a **bundle directory**: the
YAML plus a structured set of supporting files (prompts, skills,
behavior profiles, assets, and YAML fragments). Six template
namespaces let YAML reference these files; the compiler resolves
them inline at compile time.

Every behaviour and field on this page maps to real code; entries
are cited with file + line.

## Bundle layout

```text
my-app/
├── app.yaml
├── prompts/
│   ├── system.md            # {{prompt.system}}
│   ├── coordinator.md       # {{prompt.coordinator}}
│   └── system.fr.md         # locale-suffixed variant (see below)
├── skills/
│   ├── commit.md            # {{skill.commit}}
│   └── review.md            # {{skill.review}}
├── behavior/
│   └── strict_dev.yaml      # {{behavior.strict_dev}}
├── assets/
│   ├── logo.svg             # {{asset.logo}} or {{asset.logo.svg}}
│   ├── icon.png             # {{asset_b64.icon}} (small files only)
│   └── docs/
│       └── intro.md         # markdown images auto-rewrite
├── fragments/
│   ├── main_brain.yaml      # {{include:fragments/main_brain.yaml}}
│   └── shared_modules.yaml
├── widgets/                 # auto-loaded into ui.widgets.inline
│   └── stat_card.yaml
└── agents/                  # auto-loaded via dev.include convention
    └── reviewer.yaml
```

The compiler walks the YAML, finds every `{{namespace.X}}` /
`{{include:path}}` placeholder, and replaces it with the file
content (or URL) BEFORE Pydantic validates.

## The 6 namespaces

| Pattern | Folder | Resolves to |
|---------|--------|-------------|
| `{{prompt.X}}` | `prompts/X.md` | File content (raw markdown) |
| `{{skill.X}}` | `skills/X.md` | File content |
| `{{behavior.X}}` | `behavior/X.yaml` | Parsed YAML, returned as a JSON string |
| `{{asset.X}}` | `assets/X` | URL: `/api/apps/<app_id>/assets/assets/X` |
| `{{asset_b64.X}}` | `assets/X` | `data:<mime>;base64,<payload>` URI |
| `{{include:path}}` | `<bundle>/path` | Parsed YAML fragment inlined into the parent structure |

## File extension fallback chain

`_TEXT_EXTENSIONS` (`variables.py`) for `prompt` / `skill`
namespaces. The resolver tries each in order; first match wins:

1. `.md`
2. `.markdown`
3. `.txt`
4. `.prompt`
5. bare name (no extension)

So `{{prompt.system}}` finds `prompts/system.md`,
`prompts/system.markdown`, `prompts/system.txt`,
`prompts/system.prompt`, or `prompts/system` (in that order). If
the key already has an extension (`{{prompt.system.txt}}`), it's
tried verbatim first.

## Locale variants

`_read_text_file` (`variables.py`). When the compile-time
locale is set, **locale-suffixed variants win** over the default:

```text
prompts/
├── system.md          # default
├── system.fr.md       # used when locale=fr
└── system.es.md       # used when locale=es
```

`{{prompt.system}}` with `locale=fr` resolves to
`prompts/system.fr.md` if present, falls back to `prompts/system.md`
otherwise. Useful for multilingual apps shipped with the same
YAML.

## YAML frontmatter on prompt files

`variables.py` `_FRONTMATTER_PATTERN`. Standard markdown
convention - when a prompt file starts with a `---` block, it's
parsed as YAML metadata and **stripped from the inlined content**:

```markdown
---
version: 2
max_tokens_estimate: 1200
min_model: claude-sonnet-4-5
variables_required: [user_name, company]
description: "Main system prompt for the assistant"
---

You are an assistant...
```

The body (`You are an assistant...`) is what gets inlined into the
YAML at the `{{prompt.X}}` callsite. The frontmatter dict is
recorded by the compiler (`collected_prompt_metadata` at
`variables.py`) for later validation - version checks, model
compatibility, required-variables enforcement.

## Markdown image rewriting

`variables.py` `_rewrite_markdown_assets`. When a prompt or
skill markdown file contains image references, the compiler
rewrites them to client-fetchable asset URLs at inlining time:

| Original markdown | Rewritten URL |
|-------------------|---------------|
| `![logo](./logo.svg)` | `![logo](/api/apps/<app_id>/assets/assets/logo.svg)` |
| `<img src="docs/screenshot.png">` | `<img src="/api/apps/<app_id>/assets/assets/docs/screenshot.png">` |

Resolution is relative to the markdown file's directory, then
mapped under `assets/`. `_MD_IMAGE_PATTERN` (`variables.py`)
catches both markdown image syntax (`![alt](path)`) and HTML
`<img>` tags.

## `{{prompt.X}}` - system prompts

The most common use case: factor an agent's system prompt into a
separate markdown file.

```yaml
agents:
  - id: assistant
    brain: { ... }
    system_prompt: "{{prompt.assistant_system}}"
```

`prompts/assistant_system.md`:

```markdown
---
version: 1
description: "Main system prompt"
variables_required: [workspace]
---

You are a helpful coding assistant.

## Workspace
You operate in {{workspace}}. Read files via Read tool, edit
via Edit, search via Grep.

## Workflow
1. Plan before acting (use TodoCreate).
2. Read before editing.
3. Test after writing.
```

The whole markdown body becomes the agent's `system_prompt`. Any
nested `{{...}}` placeholders (here `{{workspace}}`) are resolved
recursively up to `_MAX_DEPTH = 10` levels (`variables.py`).

## `{{skill.X}}` - slash-command skill files

Skills are reusable workflows the agent loads via `use_skill`. See
[Skills System](21-skills.md). Two ways to ship them:

1. **Declared explicitly** under `dev.skills` with
   `{command, description, path}`. The agent calls
   `use_skill('/cmd')` and gets the file content.
2. **Inlined** via `{{skill.X}}` in another field (e.g. inside
   another agent's prompt). Same file, same content - different
   delivery path.

```markdown
<!-- skills/commit.md -->

# /commit - Stage and push the diff

1. Run `git status`
2. Group changes by intent
3. Commit with conventional-commits messages
4. `git push`
```

```yaml
dev:
  skills:
    - command: /commit
      description: "Stage + commit + push"
      path: skills/commit.md
```

## `{{behavior.X}}` - custom behavior profiles

Reference a YAML profile defined under `behavior/`. The file is
parsed and returned as a JSON string the engine then loads:

```yaml
security:
  behavior:
    profile: "{{behavior.strict_dev}}"
```

`behavior/strict_dev.yaml`:

```yaml
name: strict_dev
description: "Ultra-strict dev rules"
extends: dev               # build on top of the built-in dev profile

rules:
  read_before_edit: true
  test_after_changes: true
  max_blind_reads: 1

prompt: |
  Additional behavioral instructions appended to the agent's
  system prompt.

custom:
  - id: protect_migrations
    rule: "Never modify migration files without asking"
    trigger: edit
    action: block
```

Resolution requires an actual YAML mapping - non-mapping content
raises a clear error (`variables.py`). Full Behavior Engine
reference: [Behavior Engine](43-behavior.md).

## `{{asset.X}}` - asset URLs

Returns a client-fetchable URL. The the chat client / web client GETs
`/api/apps/<app_id>/assets/<path>`.

```yaml
ui:
  greeting: |
    Welcome! Here's what I can do:

    ![architecture]({{asset.docs/architecture.svg}})
```

`{{asset.X}}` resolution at compile time:

- **With extension** - `{{asset.logo.svg}}` → URL pointing at
  `assets/logo.svg`.
- **Without extension** - `{{asset.logo}}` → tries
  `_ASSET_EXTENSIONS` (`.png`, `.jpg`, `.jpeg`, `.svg`, `.webp`,
  `.gif`, `.ico`, `.pdf`, `.json`, `.yaml`, `.yml`, `.csv`,
  `.txt`, bare name; `variables.py`). First match wins.

Path traversal is guarded - every resolved path must stay under
`<bundle>/assets/`.

## `{{asset_b64.X}}` - inlined base64

Returns a `data:<mime>;base64,<payload>` URI. Useful for small
icons embedded directly in HTML, SVG, or LLM prompts (avoids an
HTTP round-trip from the client).

```yaml
ui:
  workspace:
    title: "Editor"
  # Inline a small icon directly into the system prompt
agents:
  - id: assistant
    system_prompt: |
      ![icon]({{asset_b64.icon}})
      You are an assistant.
```

**Size cap: 64 kB** (`variables.py` `_asset_b64_cap`).
Larger assets raise an error pointing at `{{asset.X}}` (URL form)
as the fix. Override the cap via the `DIGITORN_ASSET_B64_MAX_BYTES`
env var.

The MIME type is detected via `mimetypes.guess_type`; unknown
types fall back to `application/octet-stream`.

## `{{include:path}}` - YAML fragment inlining

Inlines a YAML fragment file into the parent structure. Lets
authors factor shared blocks between agents:

```yaml
agents:
  - id: main
    brain: "{{include:fragments/main_brain.yaml}}"
  - id: backup
    brain: "{{include:fragments/main_brain.yaml}}"
```

`fragments/main_brain.yaml`:

```yaml
provider: deepseek
model: deepseek-chat
backend: openai_compat
config:
  api_key: "{{secret.DEEPSEEK_API_KEY}}"
temperature: 0.2
fallback:
  provider: anthropic
  model: claude-haiku-4-5
  config:
    api_key: "{{secret.ANTHROPIC_API_KEY}}"
```

The included file is parsed as YAML and returned as a Python
object, so `{{include:fragments/main_brain.yaml}}` drops directly
into a mapping field like `brain:`. Path traversal is guarded
(`variables.py`). Recursion depth is the same `_MAX_DEPTH =
10` shared with the variable resolver.

## Auto-loaded directories (no explicit `include:` needed)

Two directories are auto-loaded by the compiler with no YAML
declaration:

| Directory | Auto-loaded into |
|-----------|-----------------|
| `agents/*.yaml` | Appended to `agents:` (each file = one agent definition) |
| `hooks/*.yaml` | Appended to `runtime.hooks` |
| `widgets/*.yaml` | Merged into `ui.widgets.inline` (key = file stem) |

Override the auto-load by declaring an explicit `dev.include`
block (`schema.py`):

```yaml
dev:
  include:
    agents: [./roster/triage.yaml, ./roster/refund.yaml]
    hooks: ./shared/hooks/
```

When `dev.include.agents` is set, the convention auto-load is
**replaced** (not merged with) the explicit list.

## Compile-time guarantees

Every namespace uses the same defensive coding:

- **Bundle-context required** - when no `bundle_dir` is set in
  the resolver context (e.g. tests, pre-existing callers), the
  template is **passed through unresolved**. Lets debugging
  callers see the bad reference.
- **Path traversal blocked** - every resolved path is checked
  with `Path.resolve.relative_to(base)`. Escapes raise
  `ValueError`.
- **Missing files** - raise `ValueError` with a `Available: [...]`
  list of files that ARE present (sampled via `_sample_dir`).
- **Recursion depth** - capped at `_MAX_DEPTH = 10`
  (`variables.py`); cycles raise an error.

## Hot reload (dev mode)

When the daemon runs in dev mode (`server.reload: true` or
`digitorn start --reload`), changes to `prompts/`, `skills/`,
`behavior/`, `assets/`, `fragments/`, `widgets/`, and `agents/`
trigger a recompile. The next agent turn picks up the new content
without restarting the daemon.

In production (`reload: false`, the default), the bundle is read
once at deploy time and cached. To pick up changes, redeploy:

```bash
digitorn dev deploy ./my-app/app.yaml --force
```

## Cross-references

- Variables overview (the namespaces are part of the broader
  template syntax):
  [App Configuration → Variables](02-app-config.md#variables)
- Expressions (truthful template language reference):
  [Expressions](10-expressions.md)
- Skills system (where `{{skill.X}}` files come from):
  [Skills System](21-skills.md)
- Behavior profiles (where `{{behavior.X}}` files come from):
  [Behavior Engine](43-behavior.md)
- Widget files auto-loaded into `ui.widgets.inline`:
  [Widgets](42-widgets.md)
