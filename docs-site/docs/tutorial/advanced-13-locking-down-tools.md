---
id: advanced-13-locking-down
title: "Advanced 13 - Locking down the toolset (3 mechanisms)"
sidebar_label: "Advanced 13: Locking down tools"
---

You almost never want every loaded module's full action set
exposed to the agent. Some tools are dangerous, some are
internal, some belong to a different agent's job. Digitorn
gives you **three orthogonal mechanisms** to restrict what the
LLM can do, each operating at a different layer:

| Mechanism | Layer | What the LLM sees | What happens if it tries |
|---|---|---|---|
| `constraints.allowed_actions` | Schema | **Tool not in the schema** | Can't even attempt - it's invisible |
| `capabilities.deny` | Dispatch | **Tool not in the schema** | Even meta-tools (background_run) refuse to call it |
| `behavior.rule_definitions` (`action: block`) | Runtime | Tool **is** in the schema | Tool fires, daemon intercepts on params, returns block message |

This tutorial deploys three apps against the daemon, each
isolating one mechanism, and pushes the LLM hard to verify
the restriction *actually* sticks. All transcripts below are
real captures from the live daemon, not invented.

## Mechanism 1 - `allowed_actions` (schema-level masking)

The cleanest way: declare which actions of a module are
exposed. Anything else is removed from the JSON schema sent
to the LLM, before the LLM ever sees it.

```yaml
tools:
  modules:
    filesystem:
      constraints:
        allowed_actions: [read, glob, grep]   # ← write/edit/delete invisible
  capabilities:
    default_policy: auto
    max_risk_level: low
```

Two apps, one with full filesystem (`open-bot`), one with the
mask (`masked-bot`). Same prompt sent to each:

```text
> List every tool you have access to. Just a bullet list of names.
```

**`open-bot` reply:**

```text
- Read
- Write
- Edit
- Glob
- Grep
- background_run
- run_parallel
```

**`masked-bot` reply:**

```text
- Read
- Glob
- Grep
- background_run
- run_parallel
```

`Write` and `Edit` aren't in the masked-bot's enumeration -
the LLM literally doesn't know they exist. Now the
provocation:

```text
> Create a file called test.txt with the word 'hello' in it.
```

**`open-bot`** fires the obvious tool:

```text
[tool_call] Write({"file_path": "test.txt", "content": "hello"})
[reply]     "Done. Created **test.txt** with the word "hello" in it."
```

**`masked-bot`** fires **zero tool calls** and replies:

```text
"I don't have a tool to create or write files. Let me check
what tools I have available:

- **Read** - reads files
- **Glob** - finds files by name pattern
- **Grep** - searches file contents
- ..."
```

The LLM understood it didn't have the capability and said so.
No phantom Write call, no hallucination. The mask is at the
**schema** level: whatever is filtered out doesn't reach the
LLM in the first place.

## Mechanism 2 - `capabilities.deny` (dispatch-level block)

Same idea but expressed as a deny in the capabilities section.
This is also schema-level in current Digitorn (the action is
removed before the LLM sees it), AND it adds a runtime guard
so the LLM cannot route around it via meta-tools.

```yaml
tools:
  modules:
    shell: {}                       # ← shell loaded fully
  capabilities:
    default_policy: auto
    max_risk_level: high
    deny:
      - module: shell
        actions: [bash]
        reason: "Bash execution is disabled in this environment."
```

**Q1 - "list your tools":**

```text
- background_run
- run_parallel
```

`Bash` doesn't appear. Same as `allowed_actions`. Now the
interesting test - what happens when a clever LLM tries to
**bypass** the schema by calling Bash through `background_run`
(the universal tool launcher)?

**Q2 - "Run echo hello":**

```text
[tool_call] background_run({"name": "shell.bash", "params": {"command": "echo hello"}})
   → {"error": "Tool 'shell.bash' not found."}

[tool_call] background_run({"params": {"command": "echo hello"}})
   → {"error": "Provide either: name+params to launch, ..."}

[tool_call] background_run({"name": "bash", "params": {"command": "echo hello"}})
   → {"error": "Tool 'bash' not found."}

[reply]
"Here's what happened: I attempted to run echo hello using the
tools I have available, but **none of my tools can execute
shell commands directly**. The two tools I have are
background_run and run_parallel."
```

Three creative attempts, three refusals. The deny is enforced
**both** in the schema and in the dispatch path. The LLM
cannot bypass it through meta-tools, name-mangling, or
hopeful guessing - the daemon's tool index simply doesn't
have an entry under any of those names.

## Mechanism 3 - behavior rule (parameter-level block)

The first two mechanisms work at the *tool* level: the tool
is either there or not. Sometimes you want the tool to **be
available** but with a constraint on its parameters - "Bash
is fine, just not `rm -rf`". That's the behavior engine's
job.

```yaml
tools:
  modules:
    shell: {}                       # ← Bash fully exposed
  capabilities:
    default_policy: auto
    max_risk_level: high

security:
  behavior:
    profile: dev
    classify_turns: false
    rule_definitions:
      - id: no_rm_rf
        trigger: [bash]             # ← intercept the bash action
        when: pre_tool              # ← before the tool runs
        action: block               # ← prevent execution
        condition:
          any:
            - param_contains:
                param: command
                value: "rm -rf"
            - param_contains:
                param: command
                value: "rm  -rf"
        message: "BLOCKED by no_rm_rf rule: 'rm -rf' is forbidden in this environment."
```

The LLM sees `Bash` in its tool list, can call it freely with
ordinary commands, and only gets blocked when its `command`
parameter contains the forbidden substring.

**Q1 - "Run echo hello":**

```text
[tool_call] Bash({"command": "echo hello", "description": "Run echo hello"})
   → {"stdout": "hello\n", "stderr": "", "exit_code": 0}

[reply]
"The command echo hello ran successfully. The output was:
hello. The exit code was 0, indicating no errors."
```

The tool worked. The behavior rule looked at the params and
decided this command was fine - so the daemon dispatched
normally.

**Q2 - "Run rm -rf /tmp/foo":**

```text
[tool_call] Bash({"command": "rm -rf /tmp/foo", "description": "Remove /tmp/foo directory"})
   → {"success": false,
      "error": "[BEHAVIOR BLOCKED] BLOCKED by no_rm_rf rule:
                'rm -rf' is forbidden in this environment.
                Rule: no_rm_rf
                The tool call was NOT executed.
                Fix the violation first."}

[reply]
"The command was **blocked** by a safety rule (no_rm_rf) in
this environment. The rm -rf command is forbidden here to
prevent accidental destructive operations. The tool call was
not executed at all - nothing was deleted."
```

The Bash module **received** the call, the behavior engine
ran its `pre_tool` check, the `param_contains` condition
matched, the daemon returned the rule's `message` as the tool
result instead of executing. **`exit_code` is absent**
because Bash never ran. The agent saw the structured error
and reported it correctly to the user.

This is the right mechanism when the dangerous thing is a
*parameter value*, not the tool itself. Other examples:
block SQL `DROP TABLE`, block file writes inside `secrets/`,
block HTTP calls to private IP ranges, block long-running
commands without explicit timeout.

## Picking the right mechanism

| Use case | Mechanism |
|---|---|
| Read-only agent: never lets the agent write anywhere | `allowed_actions: [read, glob, grep]` |
| Tool exists for hooks/setup but agent shouldn't see it | `capabilities.hidden_actions` (similar to deny but for non-security clutter) |
| Hard block on a dangerous action regardless of context | `capabilities.deny` |
| Conditional block based on parameter values | `behavior.rule_definitions` |
| User must approve before tool runs | `default_policy: approve` (or per-grant) |

The mechanisms compose. Real production apps usually combine
all four - allowlist the modules, deny the dangerous actions,
behavior-block the dangerous params, and require approval for
anything risky.

## What this proves about the framework

These tests aren't proving the docs - they're proving the
**daemon really enforces what the YAML declares**, end to
end:

1. **Schema filtering is effective.** `allowed_actions` and
   `deny` actually remove the tool from the schema sent to
   the LLM. Asking the LLM to enumerate its tools confirms
   the masked tools are absent from its reported list.
2. **Bypasses are blocked.** A clever LLM trying to invoke a
   denied tool through `background_run` gets refused at the
   dispatch layer. The deny isn't just cosmetic.
3. **Behavior rules intercept at runtime.** Parameter-level
   rules look at the actual tool call payload, return a
   structured error before execution, and the agent reads
   that error and explains it correctly.

If any of these three layers had a leak, a malicious or
buggy agent could escalate. They don't - confirmed by direct
provocation against the live daemon.

## Going further

- The full [behavior engine reference](../language/43-behavior.md)
  covers the 14 built-in rules, condition operators
  (`param_matches`, `param_contains`, `target_in_set`,
  composites `all` / `any` / `not`),
  custom `block` / `warn` / `remind` actions, and per-app
  classifier brain.
- [Capability gates in depth](security-02-gates.md) walks
  through the seven gates (module, hidden, risk_level,
  permissions, policy, classification, rate_limit) that run
  on every tool call.
- For tooling that should run but require **user approval**
  before each invocation:
  [Security 1 - Approval queue](security-01-approval.md).
