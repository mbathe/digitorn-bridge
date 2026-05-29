---
id: advanced-03-discovery
title: "Advanced 3 - Discovery mode for large toolsets"
sidebar_label: "Advanced 3: Discovery"
---

The earlier tutorials all ran in **direct** tool injection: every
tool's full JSON schema was shipped to the model up front, in the
system prompt's tool list. That's the right call for small apps -
the model picks from a handful of tools and the schemas pay for
themselves.

It stops working when the toolbox grows. Twenty modules each with
ten actions is two hundred tool entries; their schemas eat tens of
thousands of input tokens **before the user has typed anything**.
The model also gets noisier as the haystack grows: with two
hundred tools in front of it, it picks the wrong one more often.

Digitorn's answer is **discovery mode**. The model sees only five
meta-tools - `list_categories`, `browse_category`, `search_tools`,
`get_tool`, `execute_tool` - and **navigates the catalogue at
runtime** to find the tool it needs. The full schemas are loaded
on demand, one at a time, when the model is ready to use them.

The compiler picks the right mode automatically based on the size
of the toolbox; you can force it via `runtime.tool_injection`.

## The three modes

| Mode             | What the model sees                                                               | When the compiler picks it                       |
|------------------|-----------------------------------------------------------------------------------|--------------------------------------------------|
| `direct`         | Every tool's full JSON schema                                                     | Tool tokens fit in 20% of the context window     |
| `compact_direct` | Tool name + one-line description, no schema; full schema on demand via `get_tool` | Names + descriptions fit but full schemas don't  |
| `discovery`      | Only the five meta-tools                                                          | Even the compact list does not fit               |

The 20 % budget is.  The decision
table sits. You can short-circuit
the algorithm with `runtime.tool_injection: discovery` to force the
mode regardless of toolbox size.

## The YAML

Save this as `discovery-bot.yaml`. The app loads four modules
(filesystem, memory, web, shell) and forces discovery mode even
though the toolbox is small enough to fit in direct - the point is
to **see the meta-tools at work**.

```yaml
app:
  app_id: discovery-bot
  name: Discovery Bot
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 10
  timeout: 180
  tool_injection: discovery        # force discovery

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
    system_prompt: |
      You have many tools available but you do NOT see their schemas
      up front. Use the discovery tools to find what you need:
        1. list_categories() - see broad categories
        2. browse_category(category="...") - list tools in a category
        3. get_tool(name="...") - read one tool's full schema
        4. execute_tool(name="...", params={...}) - run a tool you've inspected

      Be concise. Pick the smallest reasonable path.

tools:
  modules:
    filesystem: {}
    memory: {}
    web: {}
    shell: {}
  capabilities:
    default_policy: auto
```

## The five meta-tools

The agent sees only these in its tool list:

| Tool              | Purpose                                                        |
|-------------------|----------------------------------------------------------------|
| `list_categories` | Returns the top-level categories (filesystem, memory, web, …)  |
| `browse_category` | Returns the tools inside one category, with one-line summaries |
| `search_tools`    | Semantic search over the full tool index                       |
| `get_tool`        | Returns the full JSON schema for a single tool name            |
| `execute_tool`    | Runs the named tool with the given parameters                  |

The agent's job becomes a small loop: **explore → narrow →
inspect → execute**. Each meta-call is small (a few hundred
tokens of output) so the context grows incrementally instead of
front-loading every schema.

## Live transcript

The user asks the agent to **find a tool** without running it.
Sample transcript.

```text
> Find a tool that can grep file contents and tell me what it
  requires. Do not run it; just discover and describe.

Here's what I found:

**Tool:** `filesystem.grep`

**What it does:** Searches file contents for a regex pattern.
It's powered by **ripgrep** for speed, and is meant to be used
*before* `Read` to locate what you need to edit.

**Parameters:**

| Parameter | Type   | Required | Default        | Description                  |
|-----------|--------|----------|----------------|------------------------------|
| `pattern` | string | Yes      | -              | The regex pattern to search  |
| `path`    | string | No       | `.` (cwd)      | File or directory to search  |

**Key points:**
- Only **1 required parameter**: `pattern`
- `path` is optional - defaults to the current working directory
- It's for **content** search (inside files), not filename search;
  use `filesystem.glob` for finding files by name.
```

The session log captured exactly **2 tool calls**
(`tool_calls_count: 2`). The agent didn't call `filesystem.grep`
itself; it used `browse_category` to scan the filesystem section,
then `get_tool` to fetch the full schema for the candidate, and
returned the human-readable summary.

If the user had said "search for the word 'pong' in /tmp", the
agent would have continued with one more call - `execute_tool` -
to actually run `filesystem.grep`. Discovery mode does not change
**what** the agent can do; it changes **when the schema is
loaded**.

## What this buys you

- **Token savings up front**. A 200-tool app pays roughly 60 K
  tokens for the schemas in `direct` mode. In `discovery` it
  pays the cost of the five meta-tool entries plus the
  category index, ~2 K tokens. The 58 K saved is room for
  longer conversations.
- **Less wrong-tool selection**. With only the meta-tools
  visible, the model doesn't have to scan a 200-tool list each
  turn to pick `filesystem.write`. It pulls the relevant
  category, gets a small list, picks one.
- **Per-category gating** comes for free. A capability rule
  that hides a whole module also hides its category from
  `list_categories`. The agent never sees the option.

The cost is two or three extra tool calls per turn. For
short-lived conversations the schema-up-front cost dominates and
direct wins; for long sessions or large toolsets, discovery wins
big.

## Hybrid: compact_direct

`compact_direct` is the in-between. The agent sees every tool's
**name** and a **one-line description**, but no schema. When it
decides to call a specific tool, it fetches the schema with
`get_tool` first.

This is the sweet spot when you have ~50 tools - too many for
their full schemas to fit, but few enough that the names list is
manageable. The agent does one more call than direct (the
`get_tool` lookup) but skips the category-walk overhead of
discovery.

## Forcing the mode

The compiler picks automatically based on the budget calculation:

```python
# tool-injection budget decision (run at app bootstrap)
budget         = context_window * 0.20            # _MAX_CONTEXT_RATIO
tool_tokens    = sum(len(json.dumps(t)) // 4 for t in tools)
compact_tokens = total_tools * 30                 # name + one-liner per tool

if tool_tokens <= budget:
    return "direct"
elif compact_tokens <= budget:
    return "compact_direct"
else:
    return "discovery"
```

Override with `runtime.tool_injection: direct | compact_direct |
discovery`. The forced mode bypasses the budget calculation
entirely. Force `discovery` for apps where you intentionally want
the agent to deliberate before binding to a tool; force `direct`
for apps where every millisecond counts and the toolbox is
known-small.

## Going further

- The full mode reference is in
  [Tools - Adaptive tool injection](../language/04-tools.md#adaptive-tool-injection).
- The auto-decision algorithm and threshold table:
  [Tool injection](../language/04-tools.md#adaptive-tool-injection).
- For the meta-tool semantics (their schemas, their return shapes,
  the cache they share):
  [Built-in tools](../language/04b-builtin-tools.md).
