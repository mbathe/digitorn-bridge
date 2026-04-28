# Digitorn Code

Terminal-grade coding assistant - multi-agent architecture inspired
by Claude Code, ported to Digitorn.

## What it does

Digitorn Code spawns a **coordinator agent** that triages every
request into one or more **specialist workers** (research,
implementation, verification, general). Each worker has its own
role, system prompt, and tool set:

- **filesystem** - read, write, edit, glob, grep
- **shell.bash** - for git, build tools, test runners
- **lsp** - Python diagnostics via ruff, plus other languages on
  demand
- **memory** - todos, goals, persistent context
- **web** - search + fetch for documentation lookups
- **agent_spawn** - the coordinator orchestrates workers via
  spawn_agent / agent_wait / agent_result

## Why it ships built-in

Digitorn Code is the canonical demonstration of what a multi-agent
Digitorn app looks like. It also doubles as a real productivity
tool - many developers will use it day-to-day for coding tasks
and never touch any other app.

## Architecture

The package is a verbatim port of the ``examples/opencode/app.yaml``
reference. The coordinator + 4 specialist workers pattern is the
same one Claude Code itself uses.

## Permissions

- ✅ Network (web search + fetch)
- ✅ Filesystem (read, write, edit - but with **per-call user
  approval** for destructive ops)
- ✅ Shell execution (with per-call user approval)
- 🔴 Risk level: **high**

The high risk level is honest: a coding assistant can break
things. Digitorn Code mitigates this with the
``requires_approval`` mechanism - every ``filesystem.write``,
``filesystem.edit``, and ``shell.bash`` call shows a confirmation
prompt before it runs. The user can ``always allow`` to skip
future prompts within a session.

## Credentials needed (default install)

The default brain uses **DeepSeek** (``deepseek-chat`` via the
``openai_compat`` backend), which is cheap and fast for coding
work. The package installs without the key - ``{{env.X}}``
templates are lenient and pass through at compile time. The agent
will fail at first call if ``DEEPSEEK_API_KEY`` isn't set in the
environment or stored in the credential store (per-user scope).

## How to switch the model

The five agents (coordinator + 4 specialists) all share the same
brain configuration block. To switch every agent to a different
provider, edit each ``brain:`` block in ``app.yaml``:

```yaml
brain:
  provider: deepseek                    # or openai, anthropic, mistral, ...
  model: deepseek-chat
  backend: openai_compat                # required for non-Anthropic
  config:
    api_key: "{{secret.DEEPSEEK_API_KEY}}"
    base_url: "https://api.deepseek.com/v1"
```

Then store the credential via the dashboard (Credentials tab) or
the CLI:

```bash
digitorn credentials set --scope per_user deepseek api_key sk-...
```

The original ``examples/opencode/app.yaml`` (the verbatim Claude
Code port that uses DeepSeek by default) is preserved for
reference under ``examples/opencode/`` in the source tree.

## Customisation

Like all built-ins, this package is a starting point. To make a
flavour you control:

1. Copy the directory to your own location
2. Edit the system prompts, brain config, capabilities
3. Install your copy via ``digitorn package install /path/to/yours``
4. The original ``digitorn-code`` keeps tracking the wheel-shipped
   version
