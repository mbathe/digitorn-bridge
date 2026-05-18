---
id: tutorial-03-tool
title: "3. Add a tool"
sidebar_label: "3. Add a tool"
---

In step 2 the agent had memory but no way to look at the world.
Now you give it the ability to **read files in the user's
workspace**. The change is one new line under `tools.modules`,
and the agent picks up `Read`, `Write`, `Edit`, `Glob`, `Grep`
automatically. We only need `Read` for this tutorial.

## Prerequisites

Same as step 2 (running daemon, authenticated user, DeepSeek
credential `deepseek_main` provisioned). For this step the agent
also needs a **workspace** - a real directory on disk it can read
from. Anything works; this tutorial points at
`./workspace` containing a single file
called `notes.md`.

## A sample workspace

Create the directory and put one short file in it. On Windows:

```bash
mkdir -p ./workspace
printf 'shopping list\n- pasta\n- tomatoes\n- olive oil\n- basil\n' \
  > ./workspace/notes.md
```

The file content is a plain four-item shopping list.

## The YAML

Save this as `file-reader.yaml`:

```yaml
app:
  app_id: file-reader
  name: File Reader
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
      You can read files in the user's workspace using the Read tool.
      When asked about a file, call Read with its path, then answer
      with a one-line summary of what's inside. Keep replies short.

tools:
  modules:
    filesystem: {}
    memory: {}
  capabilities:
    default_policy: auto

ui:
  greeting: "Ask me about a file in your workspace and I'll read it."
```

## Deploy and chat

```bash
digitorn dev deploy file-reader.yaml
digitorn dev chat file-reader
```

When the chat starts, point the session at the workspace
directory you just created (the dev CLI prompts for this on the
first turn).

## Live transcript

Real exchange against a running daemon, captured by the live
test client:

```text
> Please read notes.md and tell me what's in it.
**notes.md** contains a shopping list with 4 items: pasta,
tomatoes, olive oil, and basil.
```

The session log confirms the agent fired one tool call before
answering (`tool_calls_count: 1` on the `result` event), and the
token accounting shows it actually loaded the file:

```text
usage: input_tokens=5786, output_tokens=22, cost_usd=0.004717
```

The 5786 input tokens are the system prompt + tool schemas + the
file content the `Read` tool returned. The 22 output tokens are
the one-line summary.

## What changed vs step 2

One line under `tools.modules`:

```yaml
tools:
  modules:
    filesystem: {}            # adds Read / Write / Edit / Glob / Grep
    memory: {}                # from step 2
  capabilities:
    default_policy: auto
```

That's all. The agent now sees the five filesystem tools alongside
its memory tools and decides on its own when to call `Read`.

## Tightening the surface

`default_policy: auto` grants every action the agent declares it
needs. For a real app you usually want to be more deliberate -
read-only access, no writes, no deletes. The capability block
makes that explicit:

```yaml
tools:
  modules:
    filesystem: {}
  capabilities:
    default_policy: block               # nothing by default
    grant:
      - module: filesystem
        actions: [read, glob, grep]     # explicit allowlist
```

With this version the agent can read and search files but cannot
write, edit, or delete.

## Going further

- Use `Glob` to find files (`Glob(pattern="**/*.md")`) and `Grep`
  to search inside them. Both ship with the `filesystem` module.
- See the [filesystem module reference](../reference/modules/filesystem.md)
  for the full action list (and the hidden parameters that handle
  encoding, fuzzy editing, image reads, etc.).
- For sandboxed workspaces (the agent never sees real disk, only
  a virtual file tree streamed to the client), see the
  [workspace module](../reference/modules/workspace.md). That's
  what live-canvas apps like the React sandbox use.

Next: [4. Multi-agent team](04-multi-agent.md) - one coordinator
dispatching work to specialists in parallel. Or jump to
[Build a RAG bot](../howtos/build-a-rag-bot.md) - a how-to that
puts the rag module to work over a folder of markdowns.
