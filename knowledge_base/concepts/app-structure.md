---
id: app-structure
title: "App Structure"
type: concept
keywords: [app, structure, layout, yaml, package.toml, prompts, skills, assets, preview, bundle, template, project, directory]
related: [what-is-digitorn, app-lifecycle, package, modules-overview]
source: docs/
---

# App Structure

## Overview

A Digitorn app ranges from a single `app.yaml` file to a full project directory with prompts, skills, assets, and a React preview client. The structure you choose depends on the complexity of your app.

## Minimal app -- single file

The simplest possible app is just an `app.yaml`:

```yaml
app:
  app_id: my-chatbot
  name: "My Chatbot"

agents:
  - id: main
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
      temperature: 0.3

execution:
  mode: conversation
  greeting: "Hello! How can I help?"
```

Deploy directly: `POST /api/apps/deploy {yaml_path: "/path/to/app.yaml"}`.

## Bundle app (recommended)

For production apps, use a bundle directory:

```
my-app/
  package.toml          -- manifest (name, version, permissions)
  app.yaml              -- app configuration
  prompts/              -- system prompts as .md files
    system.md
    coding.md
  skills/               -- reusable skills as .md files
    review.md
    commit.md
    debug.md
  assets/               -- icons, images
    icon.png
  preview/              -- React preview client (optional)
    package.json
    src/
      App.tsx
    dist/               -- built output served by daemon
      index.html
```

## package.toml

The manifest declares metadata, requirements, and permissions:

```toml
[package]
id = "my-app"
name = "My App"
version = "1.0.0"
description = "What this app does in one line."
author = "your-name"
license = "MIT"
category = "coding"          # coding, writing, research, data, devops, design, automation, general

[package.requirements]
modules = ["filesystem", "shell", "memory", "web"]
recommended_models = ["deepseek-chat", "claude-sonnet-4-20250514"]

[package.permissions]
risk_level = "medium"        # low, medium, high
network_access = true
filesystem_access = ["read", "write"]
shell_access = true
```

Key rules:
- Package id MUST be kebab-case, 3-64 characters (e.g. `my-app`, NOT `MyApp` or `my_app`)
- A package can't be installed twice with the same id (409 collision)
- You don't have to write `package.toml` by hand: `POST /api/discovery/generate-package-manifest` auto-generates one from compiled YAML

## File references in app.yaml

### Prompts -- `{{prompt.name}}`

Store system prompts in the `prompts/` directory as `.md` files. Reference them in YAML:

```yaml
agents:
  - id: main
    brain: { ... }
    system_prompt: "{{prompt.system}}"
```

This reads `prompts/system.md` and injects its content as the system prompt. Benefits:
- Prompts can be long without cluttering the YAML
- Version control friendly (diff .md files easily)
- Reusable across agents

### Skills -- `{{skill.name}}`

Store reusable skills in the `skills/` directory as `.md` files:

```
skills/
  review.md     -- code review methodology
  commit.md     -- git commit workflow
  debug.md      -- systematic debugging steps
```

Reference them in two ways:

**1. Explicit skill list (app-level):**

```yaml
skills:
  - command: "/review"
    description: "Code review"
    path: "./skills/review.md"
  - command: "/commit"
    description: "Smart git commit"
    path: "./skills/commit.md"
```

Skills defined this way become slash commands the user can invoke (e.g. typing `/review` in chat).

**2. Agent capabilities (auto-load):**

```yaml
agents:
  - id: main
    brain: { ... }
    capabilities: [review, commit, debug]
```

The compiler reads `skills/review.md`, `skills/commit.md`, `skills/debug.md` and appends their content to the agent's system prompt under an `## Available capabilities` section. This separates the agent's identity (system_prompt) from its skill definitions.

### Variables -- `{{variable}}`

Define reusable values in the `variables:` block:

```yaml
variables:
  workspace: "{{env.PWD}}"
  db_path: "{{workspace}}/data/app.db"
  model: "deepseek-chat"

agents:
  - id: main
    brain:
      model: "{{model}}"
    system_prompt: |
      Working directory: {{workspace}}
```

Special variable sources:
- `{{env.NAME}}` -- environment variable
- `{{secret.NAME}}` -- encrypted secret stored via API
- `{{workspace}}` -- the resolved workspace path

## app.yaml top-level blocks

A complete app.yaml has these top-level blocks:

```yaml
app:                    # REQUIRED -- identity (app_id, name, version, icon, color, category)
variables:              # optional -- template variables
modules:                # optional -- module configurations
channels:               # optional -- output channel instances (webhook, slack, email, etc.)
agents:                 # REQUIRED -- at least one agent with a brain
execution:              # optional -- mode, triggers, hooks, context config
capabilities:           # optional -- security grants/denies (recommended for production)
middleware:             # optional -- app-level middleware pipeline
skills:                 # optional -- slash command skills
pipeline:               # optional -- pipeline steps (pipeline mode only)
preview:                # optional -- dev server config (Vite/Next)
workspace:              # optional -- workspace config for live previews
widgets:                # optional -- declarative UI widgets
```

## app.yaml -- app block

```yaml
app:
  app_id: my-app                   # unique identifier (kebab-case)
  name: "My App"                   # human-readable name
  version: "1.0"                   # version string
  description: "What it does"      # optional description
  author: "your-name"              # optional author
  icon: "💻"                       # emoji, icon name, URL, or base64
  color: "#8B5CF6"                 # hex color for UI
  category: "coding"               # UI grouping
  quick_prompts:                   # suggested prompts shown in UI
    - label: "Review code"
      message: "Review the code in the current directory"
      icon: "🔍"
    - label: "Write tests"
      message: "Write tests for the recent changes"
      icon: "✅"
```

## Modules block

Each module is configured with up to 4 fields: `config`, `setup`, `constraints`, `middleware`.

```yaml
modules:
  filesystem: {}                   # load with defaults

  shell: {}                        # load with defaults

  memory:
    config:                        # static config pushed at bootstrap
      working_memory: true
      todo_list: true

  database:
    setup:                         # actions executed at bootstrap
      - action: connect
        params:
          connection_id: main
          driver: sqlite
          database: "{{workspace}}/data.db"
    constraints:                   # runtime restrictions
      allowed_actions: [fetch_results, list_tables]
      blocked_actions: [execute_query]

  lsp:
    config:
      python: "ruff check --output-format=json"
```

IMPORTANT: Module config MUST be under the `config:` key. Putting config keys directly under the module block is silently dropped:

```yaml
# WRONG -- config keys are silently ignored
modules:
  rag:
    backend:
      type: qdrant        # <-- IGNORED, never reaches the module

# CORRECT
modules:
  rag:
    config:
      backend:
        type: qdrant       # <-- reaches the module via on_config_update()
```

## Agents block

```yaml
agents:
  - id: main                       # unique within this app
    role: coordinator               # coordinator, specialist, worker
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat        # openai_compat or anthropic
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
      temperature: 0.2
      max_tokens: 4096
      context:                      # context window management
        max_tokens: 131072           # 0 = auto-detect
        strategy: summarize          # truncate or summarize
        keep_recent: 10
        compression_trigger: 0.75
        auto_compact: true
    system_prompt: "{{prompt.system}}"
    plan_first: true                # explain plan before acting
    capabilities: [review, commit]  # auto-load skills
    pool:
      max_workers: 3                # sub-agent pool size

  - id: explore
    role: specialist
    specialty: "Fast codebase exploration. Read-only."
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
      temperature: 0.0
      max_tokens: 2048
    system_prompt: |
      You are a fast codebase explorer. Find information quickly.
    modules: [filesystem, memory]   # restrict module access for specialists
```

### Brain providers

| Provider | Backend | Notes |
|----------|---------|-------|
| `anthropic` | `anthropic` | Claude models, supports vision |
| `openai` | `openai_compat` | GPT-4o, o1, etc. |
| `deepseek` | `openai_compat` | DeepSeek-Chat, DeepSeek-Coder |
| `groq` | `openai_compat` | Fast inference (Llama, Mixtral) |
| `mistral` | `openai_compat` | Mistral models |
| `together` | `openai_compat` | Open-source models |
| `ollama` | `openai_compat` | Local models |
| `lm_studio` | `openai_compat` | Local models |
| `minimax` | `openai_compat` | MiniMax models |

Special `api_key` value: `"claude-code"` reads the OAuth token from `~/.claude/.credentials.json`.

## The CLI command

Generate a new app project with the CLI:

```bash
digitorn package new my-app --template chat
```

Available templates:
- `chat` -- conversational assistant
- `coding` -- coding assistant with filesystem + shell
- `background` -- trigger-driven automation
- `pipeline` -- multi-step processing

The command creates the directory structure, `package.toml`, and a starter `app.yaml`.

## See also

- what-is-digitorn -- the big picture
- app-lifecycle -- from idea to production
- package -- packaging and distribution
- modules-overview -- available modules and their actions
