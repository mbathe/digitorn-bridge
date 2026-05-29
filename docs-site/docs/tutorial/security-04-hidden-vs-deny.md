---
id: security-04-hidden
title: "Security 4 - Hidden actions vs deny"
sidebar_label: "Security 4: Hidden vs deny"
---

[Security 1](security-01-approval.md) covered `approve` and
[Security 2](security-02-gates.md) covered the gate sequence. Two
adjacent capability primitives are easy to confuse: **`hidden_actions`**
and **`deny`**. From the LLM's point of view they look identical -
the action is not in the tool list. Internally they behave
differently and you reach for them in different situations.

## The single-line summary

| Field             | Visible to the LLM   | Callable from setup pipelines, hooks, channels    | Audit log entry on attempt           |
|-------------------|----------------------|---------------------------------------------------|--------------------------------------|
| `hidden_actions`  | No                   | **Yes**                                           | Filtered before reaching the audit   |
| `deny`            | No                   | **No** (gate 4 blocks)                            | `denied` event with `gate1_hidden` or `gate4_policy` |

`hidden` is a **declutter** mechanism: stop showing the LLM
options it shouldn't reach for, but keep them callable from your
infrastructure. `deny` is a **prohibition**: this action is never
legitimate in this app, by anyone, period.

## Two YAMLs side by side

`hidden-bot.yaml`:

```yaml
tools:
  modules:
    filesystem: {}
  capabilities:
    default_policy: auto
    hidden_actions:                   # filtered from the LLM index
      - module: filesystem
        actions: [glob]
```

`deny-bot.yaml`:

```yaml
tools:
  modules:
    filesystem: {}
  capabilities:
    default_policy: auto
    deny:                             # blocked at gate 4
      - module: filesystem
        actions: [glob]
```

Same structure, one keyword different. Each file builds an app
where the agent has access to all of the filesystem actions
**except** `Glob`.

## Live verification

Both apps were deployed against a real daemon. The same prompt
went to each: *"List the filesystem tools you have available"*.

### hidden-bot

```text
I have these filesystem tools available:
- Read
- Write
- Edit
- Grep
```

`Glob` is missing. The schema-builder filtered it out before the
LLM saw the tool list.

### deny-bot

```text
I don't have a Glob tool available in my toolset.

Here are the tools I do have:
- Read
- Write
- Edit
- Grep
- background_run
- run_parallel
```

Same outcome from the LLM perspective: `Glob` is gone.

### Forcing the issue

Now ask `deny-bot` to use Glob explicitly:

```text
> Use Glob to find files matching pattern *.ts. Call Glob explicitly.

I don't have a Glob tool available in my toolset. Let me check
what I do have.

Here are the tools I do have:
1. Read - Read a file
2. Write - Write a file
3. Edit - Edit a file
4. Grep - Search file contents by regex
5. background_run - Run tools in background
6. run_parallel - Run multiple tools in parallel

None of these are a Glob tool.
```

Zero tool calls. The model has no way to invoke `Glob` because
it is not in the tool index. With `hidden`, the same demo would
print exactly the same output - but a hook or middleware running
inside the daemon could still call `filesystem.glob` on the
agent's behalf.

## Why pick one over the other

**Use `hidden_actions` when:**

- A hook needs to call the action but you don't want the LLM to
  attempt it independently. The classic case is `lsp.diagnostics`:
  the post-write hook calls it; the LLM never should.
- You want to **declutter** a wide module. Loading `channels` for
  its `send_message` action exposes ten admin-only ones at once;
  hide the nine you don't want the LLM to consider.
- A trigger pipeline needs the action at activation time but
  the agent loop should not.

**Use `deny` when:**

- The action is **never** legitimate in this app, regardless of
  caller. `workspace.delete` on a builder app, `http.post` on
  an offline assistant, `shell.bash` on a research-only bot.
- You want a **future-proof block**. New code paths that try to
  call the action (a hook you haven't written, an MCP server
  someone connects later) all hit gate 4 with the same denial.
- You want an **audit trail**. Every denied attempt writes a
  `gate4_policy` row to the audit log, useful for forensics.

The two compose. A `deny` row in `deny`, plus a corresponding
entry in `hidden_actions` for redundancy, is a defence-in-depth
pattern: even if the future code path bypasses gate 1's hidden
filter, gate 4 still rejects.

## How they interact with grant

Resolution order at gate 4 is fixed:

```text
deny  >  approve  >  grant  >  default_policy
```

A row in `deny` always wins. A row in `approve` requires user
confirmation. A row in `grant` auto-allows. The `default_policy`
catches anything not matched.

`hidden_actions` is **out of band** - it doesn't go through gate
4 at all. The schema-builder pre-filters the action before the
LLM ever sees the tool list, regardless of any grant or deny
entries that might have referenced it.

So the canonical "hide and deny" pattern is:

```yaml
tools:
  capabilities:
    default_policy: auto
    grant: []                         # nothing extra
    deny:
      - module: filesystem
        actions: [delete]             # never callable from anywhere
    hidden_actions:
      - module: filesystem
        actions: [delete]             # also stay out of the LLM's view
```

The hidden_actions row is technically redundant once the deny is
in place - `deny` already makes the action unreachable, and the
gate-1 hidden filter would never get to fire. Including it
anyway documents intent and makes the YAML readable.

## Going further

- The seven-gate sequence including gate 1 (hidden) and gate 4
  (deny): [Security 2 - Seven gates](security-02-gates.md).
- The full capability schema:
  [Security architecture](../language/11-security.md).
- For the related concept of **per-agent module restriction**
  (a different mechanism that hides actions per-agent rather
  than per-app):
  [Advanced 1 - Sub-agent isolation](advanced-01-sub-agent-isolation.md).
