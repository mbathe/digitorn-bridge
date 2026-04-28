---
id: bundle-namespaces
title: "Bundle namespaces - compile-time filesystem injection"
---

# Bundle namespaces

An app isn't just one YAML file - it's a **bundle directory**: the
`app.yaml` plus dedicated subfolders for prompts, skills, and
assets. The Digitorn compiler reads these folders and injects file
content directly into the YAML at compile time via
**filesystem namespaces** - template placeholders that are resolved
before the app runs.

This is the single feature that turns an app into a proper
software project: code (YAML), prompts (markdown), skills
(markdown), and assets (images/files) each live in their own files
with IDE support, Git diffs, and separation of concerns.

## The bundle directory

```
my-app/
├── app.yaml              # main definition
├── package.toml          # manifest (for installable packages)
├── README.md
├── prompts/              # referenced via {{prompt.X}}
│   ├── system.md
│   ├── system.fr.md      # locale variant (i18n)
│   └── persona.md
├── skills/               # referenced via {{skill.X}} or capabilities:
│   ├── commit.md
│   ├── review.md
│   └── refactor.md
├── assets/               # referenced via {{asset.X}} - images, icons
│   ├── icon.png
│   └── logo.svg
├── behavior/             # referenced via {{behavior.X}} - custom profiles
│   ├── strict_dev.yaml
│   └── research.yaml
└── fragments/            # YAML fragments, referenced via {{include:}}
    └── main_brain.yaml
```

The subfolders are **conventional** - `prompts/`, `skills/`,
`assets/`, `behavior/`. The compiler looks for them by name. `fragments/` is
free-form; it's just a common place to put YAML fragments used
with `{{include:}}`.

## The 7 namespaces

### `{{prompt.X}}` - inline a prompt file

Reads `prompts/X.md` (or `.markdown`, `.txt`, `.prompt`, bare) and
inlines the text content as a string.

```yaml
# prompts/system_main.md contains: "You are a coding assistant."

agents:
  - id: main
    system_prompt: "{{prompt.system_main}}"
```
After compile, `agent.system_prompt = "You are a coding assistant."`

Extensions tried in order: `.md`, `.markdown`, `.txt`, `.prompt`, bare name.

Variables inside the prompt file ARE recursively resolved - so a
prompt containing `{{app.name}}` or `{{greeting}}` substitutes
correctly.

**Raises a compile error** if the file is missing. The error
message lists available files so typos are caught early.

### `{{skill.X}}` - inline a skill file

Same as `{{prompt.X}}` but reads from `skills/`. The distinction
is purely semantic: skills describe **what an agent can do**;
prompts describe **how an agent thinks**.

```yaml
agents:
  - id: main
    system_prompt: |
      {{prompt.persona}}
      
      ## Your skills
      {{skill.commit}}
      {{skill.review}}
```
### `{{asset.X}}` - return the asset URL

Returns the URL the Flutter client will use to fetch the file via
`GET /api/apps/{app_id}/assets/{rel_path}`. Does **not** inline the
content - returning 10 MB of PNG bytes as a YAML string would break
everything.

```yaml
app:
  icon: "{{asset.icon.png}}"
  #   → "/api/apps/my-app/assets/icon.png"

  quick_prompts:
    - label: "Show dashboard"
      icon: "{{asset.dashboard.svg}}"
```
**Filename fuzzy match**: `{{asset.logo}}` without an extension
tries `.png`, `.svg`, `.jpg`, `.webp`, `.gif`, `.ico`, `.pdf` in
that order.

**Raises a compile error** if no matching file is found - the
message lists available assets.

**Path traversal** (`../../etc/passwd`) is rejected.

### `{{asset_b64.X}}` - base64 data URI

Like `asset.X` but returns a `data:<mime>;base64,<payload>` URI.
Use when you want a small image inlined directly in a prompt or
HTML without a separate HTTP request.

```yaml
agents:
  - id: main
    system_prompt: |
      Here's my logo: {{asset_b64.logo.svg}}
```
**Size cap**: 64 kB by default. Override with
`DIGITORN_ASSET_B64_MAX_BYTES`. Files over the cap raise a compile
error suggesting `{{asset.X}}` (URL form) instead.

### `{{behavior.X}}` - custom behavior profile

Reads `behavior/X.yaml` (or `.yml`), parses it, and returns the
profile dict as a JSON string. Used in the `behavior.profile` field
to load a custom behavioral profile.

```yaml
# behavior/strict_dev.yaml
name: strict_dev
description: "Ultra-strict developer rules"
extends: dev
rules:
  max_blind_reads: 1
prompt: |
  Always run tests after every change.
```

```yaml
# app.yaml
behavior:
  profile: "{{behavior.strict_dev}}"
  classify_turns: true
```

The resolver parses the YAML file and returns it as structured data.
The `extends` field inherits from a built-in profile (`dev`, `coding`,
etc.). Custom `rules`, `prompt`, and `custom` entries are merged on
top of the base.

**Raises a compile error** if the file is missing or not a valid
YAML mapping.

See [Behavior Engine](43-behavior.md) for the full custom profile
format and all available fields.

### `{{include:path}}` - YAML fragment

Reads a YAML file from any path within the bundle and substitutes
the parsed structure. Useful for factoring shared config blocks.

`fragments/main_brain.yaml`:

```yaml
provider: anthropic
model: claude-sonnet-4-5
config:
  api_key: "{{env.ANTHROPIC_API_KEY}}"
temperature: 0.1
```
`app.yaml`:

```yaml
agents:
  - id: main
    brain: "{{include:fragments/main_brain.yaml}}"
  - id: backup
    brain: "{{include:fragments/main_brain.yaml}}"
```
Both agents share the same brain config. Edit the fragment once,
both agents update.

### `capabilities: [...]` - auto-load skills into an agent

Instead of writing `{{skill.commit}}` manually in every agent's
system prompt, declare a list of skills via `capabilities` and the
compiler auto-injects them under a dedicated section.

```yaml
agents:
  - id: main
    system_prompt: "{{prompt.base_persona}}"
    capabilities: [commit, review, refactor]
```
At compile, the agent's `system_prompt` becomes:

```
[contents of prompts/base_persona.md]

## Available capabilities

### commit
[contents of skills/commit.md]

### review
[contents of skills/review.md]

### refactor
[contents of skills/refactor.md]
```

**Recommended pattern**: separates the "who am I" (base prompt)
from the "what I can do" (skill files). Each skill is then
individually versioned, testable, and reusable across agents.

**Compile error if a skill is missing.**

## Frontmatter on prompts and skills

Prompts and skills can carry YAML frontmatter - standard
Jekyll/Markdown convention:

```markdown
---
version: 2
description: "Main system prompt for the coding assistant"
max_tokens_estimate: 1200
min_model: claude-sonnet-4-5
variables_required: [user_name, company]
---

You are an expert coding assistant working for {{company}}...
```

Fields recognized:

| Field | Meaning | Enforcement |
|---|---|---|
| `version` | Your own versioning | Informational |
| `description` | Human-readable purpose | Informational |
| `max_tokens_estimate` | Rough token count | Compile error if > 200k |
| `min_model` | Minimum model recommendation | Informational |
| `variables_required` | Variables the prompt relies on | Compile error if missing from `variables:` block |

The frontmatter is **stripped from the body** before inlining -
`---` doesn't appear in the final prompt.

## Locale-suffixed prompts (i18n)

Add a locale suffix to a prompt filename for per-language variants:

```
prompts/
├── system.md         # default (fallback)
├── system.en.md
├── system.fr.md
├── system.es.md
└── system.pt-BR.md
```

Supported formats: `en`, `fr`, `pt-BR`, `zh-CN` - anything
matching `[a-z]{2}(-[A-Z]{2})?`. The compiler tries
`X.<locale>.md` first, falls back to `X.md` when the locale
variant is missing.

At compile time, pass the locale via `bundle_context(locale="fr")`
or the runtime resolver picks the user's locale from the profile.

## Markdown image rewrite

When a prompt file contains markdown images pointing to real
assets, the compiler **rewrites the paths** to proper asset URLs:

**prompts/docs.md**:

```markdown
![architecture](../assets/diagram.svg)
```

After inline:

```markdown
![architecture](/api/apps/my-app/assets/assets/diagram.svg)
```

External URLs (`http://`, `https://`, `data:`) pass through
unchanged. Broken relative paths also pass through so authors see
the bad link.

## Hot reload in dev mode

```yaml
# ~/.digitorn/config.yaml
app:
  hot_reload: true
```
Every deployed app starts a `BundleHotReloader` that polls
`prompts/`, `skills/`, `assets/` every second. Changes trigger an
automatic redeploy with 500 ms debounce.

Changes to `app.yaml` itself still require a manual
`digitorn app deploy` - hot reload is for **content iteration**,
not structural changes.

## Live preview endpoint

`POST /api/discovery/prompt-preview` lets you iterate on a prompt
without deploying the whole app:

```json
{
  "bundle_dir": "/path/to/my-app",
  "prompt_name": "system_main",
  "variables": {"user_name": "Alice"},
  "locale": "fr"
}
```

Response:

```json
{
  "compiled_text": "Tu es ...",
  "token_estimate": 187,
  "referenced_assets": ["/api/apps/_preview/assets/logo.png"],
  "referenced_variables": ["user_name"],
  "frontmatter": { "version": 2, "description": "..." }
}
```

Useful for IDE tooling, the builder agent's "edit prompt" flow,
and CI that wants to validate prompts before merging.

## Scaffolding a new bundle

```bash
digitorn package new my-app --template chat
```

Creates a directory pre-filled with `package.toml`, `app.yaml`,
`prompts/main.md` (with frontmatter), `assets/icon.png` (1x1
placeholder), `README.md`, and `.gitignore`. Ready to
`digitorn app deploy`.

Templates shipped:

- **chat** - single-agent interactive chat
- **background** - cron-triggered background worker
- **multi-agent** - coordinator + specialist workers with capabilities
- **rag** - knowledge assistant with context_builder index
- **researcher** - deep-research agent with web tools

## Asset resize

`GET /api/apps/{app_id}/assets/{path}?size=128` returns a resized
variant at most 128 px on the longest side. Works for PNG, JPG,
WebP. Requires Pillow (`pip install Pillow`); falls back to the
original when Pillow isn't installed.

Results are cached under `.digitorn/resized/` in the bundle dir
and invalidated when the source file changes.

## Security

Every namespace has these guards:

1. **Path traversal blocked** - any resolved path that escapes the
   bundle directory raises a compile error
2. **`.digitorn/` denied** - the daemon-managed area inside a
   package is invisible to templates
3. **Size caps** - `asset_b64` is capped at 64 kB by default
4. **Compile-time validation** - a missing file raises at compile,
   never at runtime

## Anti-patterns

❌ **Don't** write a 500-line `system_prompt` inline in YAML. Use
`{{prompt.X}}` instead.

❌ **Don't** duplicate brain config across agents. Use
`{{include:fragments/brain.yaml}}`.

❌ **Don't** hardcode asset URLs like
`/api/apps/my-app/assets/icon.png`. Use `{{asset.icon}}`.

❌ **Don't** use `{{asset.X}}` for a 5 kB SVG you want to inline in
a prompt - use `{{asset_b64.X}}`.

❌ **Don't** compose entire YAML files via `{{include:}}`. It's for
small fragments only.

## See also

- [21-skills.md](21-skills.md) - the legacy single-file skills system
- [03-agents.md](03-agents.md) - agent definition with brain + capabilities
- [22-composition.md](22-composition.md) - YAML composition patterns
