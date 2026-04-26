---
id: bundle-namespaces
title: "Bundle namespaces (compile-time prompt/skill/asset/include/i18n injection)"
type: concept
keywords: [prompt, skill, asset, include, frontmatter, i18n, locale, capabilities, bundle, compile, template, markdown, hot_reload, prompts_folder, skills_folder, assets_folder]
related: [package, agents, skills, app-structure, builder-state-machine, credentials-schema]
source: docs/app-language/38-bundle-namespaces.md
---

# Bundle namespaces — the compile-time template system

## What it is

An app is **not just a single YAML file**. It's a **bundle directory**
with the app.yaml alongside dedicated subfolders for prompts, skills,
and assets. At compile time the Digitorn compiler reads files from
these folders and injects them directly into the YAML via **filesystem
namespaces** — template placeholders like `{{prompt.system}}` that are
resolved into actual content before the app runs.

This turns an app into a proper software project: code (YAML), prompts
(markdown), skills (markdown), and assets (images/files) each live in
their own files with proper IDE support, version control diffs, and
separation of concerns.

## Directory layout

```
my-app/
├── package.toml              # manifest (for installable packages)
├── app.yaml                  # main app definition
├── README.md
├── prompts/                  # referenced via {{prompt.X}}
│   ├── main_system.md
│   ├── persona.md
│   ├── system.fr.md          # locale-suffixed variant (i18n)
│   └── system.es.md
├── skills/                   # referenced via {{skill.X}} or capabilities: [...]
│   ├── commit.md
│   ├── review.md
│   └── refactor.md
├── assets/                   # referenced via {{asset.X}} — images, icons, docs
│   ├── icon.png
│   ├── logo.svg
│   └── welcome_banner.png
└── fragments/                # YAML fragments, referenced via {{include:...}}
    └── main_brain.yaml
```

The three subfolders are **conventional** — `prompts/`, `skills/`,
`assets/`. The compiler looks for them by name when resolving the
namespaces. `fragments/` is free-form; it's just the convention when
using `{{include:}}`.

## The 6 compile-time namespaces

### 1. `{{prompt.X}}` — inline a prompt file

Reads `prompts/X.md` (or `.markdown`, `.txt`, `.prompt`, bare) and
inlines the content as a string. The first matching extension wins.

**Example:**

```yaml
# prompts/system_main.md contains:
#   You are a helpful coding assistant.

agents:
  - id: main
    system_prompt: "{{prompt.system_main}}"
```

After compile, `agent.system_prompt = "You are a helpful coding assistant."`

- **File extensions tried in order**: `.md`, `.markdown`, `.txt`, `.prompt`, bare
- **Raises at compile if missing** — typos caught early, with a list of available files
- **Variables inside prompt files ARE resolved recursively** — a prompt
  that contains `{{app.name}}` will be substituted
- **Frontmatter is stripped** before inlining (see frontmatter section below)
- **Markdown image paths are rewritten** (see markdown rewrite section)

### 2. `{{skill.X}}` — inline a skill file

Same as `{{prompt.X}}` but reads from `skills/` instead of `prompts/`.
Skills are just another category of prompt file — the separation is
semantic (skills describe **what an agent can do**; prompts describe
**how an agent thinks**).

```yaml
agents:
  - id: main
    system_prompt: |
      {{prompt.persona}}

      ## Your skills
      {{skill.commit}}
      {{skill.review}}
```

### 3. `{{asset.X}}` — return the asset URL

Returns the URL the Flutter client (or any HTTP client) will use to
fetch the file via `GET /api/apps/{app_id}/assets/{rel_path}`. Does
NOT inline the content — binary files like PNG would be non-sensical.

**Filename resolution:**

- `{{asset.logo.png}}` → looks for `assets/logo.png` exactly
- `{{asset.logo}}` → tries `assets/logo.png`, `assets/logo.svg`,
  `assets/logo.jpg`, `assets/logo.webp`, `assets/logo.gif`,
  `assets/logo.ico`, `assets/logo.pdf`, …

**Example:**

```yaml
app:
  app_id: my-app
  name: My App
  icon: "{{asset.icon.png}}"
  #   → "/api/apps/my-app/assets/icon.png"

  quick_prompts:
    - label: "Show dashboard"
      icon: "{{asset.dashboard.svg}}"
      prompt: "Open the dashboard"
```

**Raises at compile if the file doesn't exist** — lists the available
assets in the error message so the user can fix the typo.

**Path traversal blocked** — `{{asset.../../etc/passwd}}` is rejected
because `../..` escapes the `assets/` directory.

### 4. `{{asset_b64.X}}` — base64 data URI

Like `asset.X` but returns a `data:<mime>;base64,<payload>` URI instead
of a URL. Use when you want to inline a small image directly into a
prompt or HTML without a separate HTTP request.

- **Size cap: 64 kB** by default. Override via `DIGITORN_ASSET_B64_MAX_BYTES`.
- Files larger than the cap raise an error — suggests using `{{asset.X}}` (URL) instead.

**Example:**

```yaml
agents:
  - id: main
    system_prompt: |
      Here's my logo in SVG form: {{asset_b64.logo.svg}}
      
      Include this in every reply.
```

Useful for tiny icons, SVG glyphs, or anything that needs to be
self-contained inside the prompt.

### 5. `{{include:path}}` — YAML fragment include

Reads a YAML file from any path within the bundle, parses it, and
substitutes the parsed structure into the parent. Use this to factor
shared config blocks (brain settings, module lists) across multiple
agents.

**Example:**

`fragments/main_brain.yaml`:

```yaml
provider: anthropic
model: claude-sonnet-4-5
config:
  api_key: "{{env.ANTHROPIC_API_KEY}}"
temperature: 0.1
max_tokens: 8192
```

`app.yaml`:

```yaml
agents:
  - id: main
    brain: "{{include:fragments/main_brain.yaml}}"
  - id: backup
    brain: "{{include:fragments/main_brain.yaml}}"
```

Both agents get the same brain config without duplicating. If you
update the fragment, both agents are updated.

- **Path is relative to the bundle root**
- **Path traversal blocked** — `../../etc/passwd` is rejected
- **Returns JSON-encoded structure** which the YAML parser re-parses
  as a dict/list — works for scalars, dicts, lists
- **Recursion guarded** via the existing `_MAX_DEPTH` limit

### 6. `capabilities: [...]` — auto-load skills into an agent

Instead of manually writing `{{skill.commit}}` inside your
`system_prompt`, declare a `capabilities` list on the agent and the
compiler auto-injects the skills under a dedicated section.

**Example:**

```yaml
agents:
  - id: main
    brain: { provider: anthropic, model: claude-sonnet-4-5, config: {...} }
    system_prompt: "{{prompt.base_persona}}"
    capabilities: [commit, review, refactor]
```

At compile, the agent's final `system_prompt` becomes:

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

**Recommended pattern** for multi-skill agents — separates the
"who am I" (base prompt) from the "what I can do" (skill definitions).
Each skill is now:

- **Individually versioned** (one file per skill)
- **Testable in isolation** (unit tests can load one skill at a time)
- **Reusable across agents** (two agents can share the same skill)
- **Reviewable by non-devs** (designer prompt-engineers can edit markdown)

## Frontmatter — optional metadata on prompts/skills

Prompts and skills can carry YAML frontmatter at the top of the file,
following the standard Jekyll / Markdown convention:

```markdown
---
version: 2
description: "Main system prompt for the coding assistant"
max_tokens_estimate: 1200
min_model: claude-sonnet-4-5
variables_required: [user_name, company_name]
---

You are an expert coding assistant working for {{company_name}}...
```

**Fields recognized by the compiler:**

| Field | Meaning | Enforcement |
|---|---|---|
| `version` | Your own versioning | Informational, not checked |
| `description` | Human-readable purpose | Informational |
| `max_tokens_estimate` | Rough token count | Compile error if > 200k |
| `min_model` | Recommended minimum model | Informational (no check in v1) |
| `variables_required` | Variables the prompt relies on | Compile error if missing from app's `variables:` block |

The frontmatter block is **stripped from the body** before inlining —
you never see `---` or the metadata fields in the final prompt.

**Best practice**: add frontmatter to every prompt so the builder agent
and IDE tooling can surface metadata without re-reading the body.

## i18n — locale-suffixed prompts

Create per-locale variants of a prompt by adding a locale suffix to
the filename:

```
prompts/
├── system.md         # default (used when no locale set, or fallback)
├── system.en.md
├── system.fr.md
├── system.es.md
└── system.pt-BR.md
```

At compile time, pass a `locale` to `bundle_context` (or use the CLI
`--locale` flag on a compile command). The resolver tries
`X.<locale>.md` first, falls back to `X.md` when the locale-specific
file is missing.

**Supported locale formats**: `en`, `fr`, `pt-BR`, `zh-CN`, etc.
Anything that matches `[a-z]{2}(-[A-Z]{2})?` works.

**Accessing the list of available locales**: at install time, call
`digitorn.core.app.variables.list_available_locales(bundle_dir)` —
returns `["en", "es", "fr", "pt-BR"]` sorted.

## Markdown image rewrite

When a prompt file contains markdown image references that point to
real asset files, the compiler **rewrites the paths** to proper asset
URLs so the Flutter client's markdown renderer can load them:

**prompts/docs.md**:

```markdown
# Architecture

![overall design](../assets/diagram.svg)

See the dashboard: <img src="assets/dashboard.png">

And this external link: ![logo](https://example.com/logo.png)
```

After compile, the inlined text becomes:

```markdown
# Architecture

![overall design](/api/apps/my-app/assets/assets/diagram.svg)

See the dashboard: <img src="/api/apps/my-app/assets/assets/dashboard.png">

And this external link: ![logo](https://example.com/logo.png)
```

- **Relative paths** that resolve to real files are rewritten
- **External URLs** (`http://`, `https://`, `data:`) pass through untouched
- **Broken relative paths** (file doesn't exist) pass through so the
  author can see the bad link in the rendered output

## Hot reload in dev mode

Set `settings.app.hot_reload = true` in your daemon config. Every
deployed app starts a `BundleHotReloader` that polls the bundle's
`prompts/`, `skills/`, and `assets/` directories every second. When
a file changes, the app is automatically redeployed with the new
content — no restart, no manual deploy.

```yaml compile=skip
# ~/.digitorn/config.yaml — this is daemon config, not app.yaml
app:
  hot_reload: true
```

- **Scope**: only `prompts/`, `skills/`, `assets/` trigger reloads.
  Changes to `app.yaml`, `package.toml`, module config still require
  a manual `digitorn app deploy`.
- **Debounce**: 500 ms quiet period after the last change — saving a
  file that your editor writes multiple times triggers one reload.
- **Production**: leave `hot_reload: false`. It's a dev-only convenience.

## Live preview endpoint — `/api/discovery/prompt-preview`

To iterate on a prompt without deploying an app (useful for the
builder agent and IDE tooling), POST to:

```
POST /api/discovery/prompt-preview
{
  "bundle_dir": "/path/to/my-app",
  "prompt_name": "system_main",
  "variables": {"user_name": "Alice"},
  "locale": "fr",
  "app_id": "_preview"
}
```

Response:

```json
{
  "compiled_text": "You are ...",
  "token_estimate": 187,
  "referenced_assets": ["/api/apps/_preview/assets/logo.png"],
  "referenced_variables": ["user_name"],
  "frontmatter": { "version": 2, ... },
  "warnings": []
}
```

Also accepts inline content instead of a bundle path:

```json
{
  "content": "You are {{app.name}}. {{prompt.extra}}",
  "bundle_dir": "/path/to/my-app",
  "variables": {"app.name": "Test"}
}
```

## CLI scaffolding — `digitorn package new`

Create a new app bundle from a template:

```bash
digitorn package new my-app --template chat
```

Available templates:

- **chat** — single-agent interactive chat
- **background** — cron-triggered background worker
- **multi-agent** — coordinator + specialist workers with capabilities
- **rag** — knowledge assistant with context_builder index
- **researcher** — deep-research agent with web tools

The result is a directory with:

- `package.toml` pre-filled (id, name, version, category)
- `app.yaml` using the namespaces (`{{prompt.X}}`, `{{asset.icon}}`)
- `prompts/*.md` template prompts with frontmatter
- `skills/*.md` (for multi-agent template)
- `assets/icon.png` placeholder 1x1 transparent PNG
- `README.md` with setup instructions
- `.gitignore`

After scaffolding:

```bash
cd my-app
# edit prompts/*.md and app.yaml to taste
digitorn app deploy ./app.yaml
```

## Security guards

Every filesystem namespace has these guards:

1. **Path traversal blocked**: any resolved path that escapes the
   bundle directory raises a compile error
2. **`.digitorn/` denied**: the daemon-managed area inside a package
   (where manifest.lock, state, etc. live) is invisible to templates
3. **Size caps**: `asset_b64` is capped at 64 kB by default to prevent
   inlining a PDF by accident
4. **Compile-time validation**: a missing prompt/skill/asset raises a
   ValueError at compile, never at runtime — typos catched when the
   user runs `digitorn app deploy`, not later when a real user sends
   a message

## When to use which namespace

| Scenario | Use |
|---|---|
| Agent system_prompt longer than 5 lines | `{{prompt.X}}` |
| Skill definition that can be reused across agents | `{{skill.X}}` + `capabilities: []` |
| Icon/logo referenced in `app.icon` or `quick_prompts` | `{{asset.X}}` |
| Small inline image inside a prompt body | `{{asset_b64.X}}` |
| Shared brain/module config between agents | `{{include:fragments/X.yaml}}` |
| Locale-specific prompt variants | `prompts/X.<locale>.md` + runtime locale |
| Prompt metadata for tooling | frontmatter block at head of the file |
| Markdown prompts with diagrams | Markdown rewrite (automatic — no action needed) |

## Anti-patterns

**Don't** write a 500-line `system_prompt` inline in the YAML.
Use `{{prompt.X}}` instead. YAML is for structure, not content.

**Don't** duplicate the same brain config across agents. Use
`{{include:fragments/brain.yaml}}`.

**Don't** hardcode asset URLs like `/api/apps/my-app/assets/icon.png`
in the YAML. Use `{{asset.icon}}` — the compiler builds the URL and
verifies the file exists.

**Don't** use `{{asset.X}}` for a 5 kB SVG you want to inline in a
prompt. Use `{{asset_b64.X}}` instead.

**Don't** use `{{include:X}}` to compose entire YAML files recursively.
It's for small fragments only. For larger composition, use multiple
agents with shared module config.

## Related concepts

- `package` — how a bundle becomes an installable app
- `agents` — how the `capabilities` field works with agent definitions
- `skills` — the legacy single-file skills field (use `capabilities` instead)
- `credentials-schema` — how `{{secret.X}}` and `{{env.X}}` compose with
  the filesystem namespaces
- `app-structure` — overall bundle directory layout conventions
