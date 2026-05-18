---
id: advanced-17-gate-destructive
title: "Advanced 17 - Block destructive commands with a custom behavior rule"
sidebar_label: "Advanced 17: Gate destructive"
---

The behavior engine intercepts every tool call **before** it
executes and runs the app's rules against it. A rule with
`action: block` stops the call cold and pushes its `message:`
into the agent's next observation, so the model sees exactly
why it was blocked and can re-route.

This tutorial wires one custom rule that pattern-matches
`bash` command params and blocks the classic irreversible
patterns (`rm -rf`, `git reset --hard`, `git push --force`,
`dd of=`, `mkfs`, SQL `drop table` / `truncate table`).

## The rule shape

The modern declarative form lives under
`security.behavior.rule_definitions`. Schema fields used here:

| Field | Meaning |
|---|---|
| `id` | Stable identifier, used in violation messages and logs |
| `trigger` | List of tool names this rule applies to (short and FQN forms accepted) |
| `when` | `pre_tool` runs BEFORE the tool fires, `post_tool` runs after |
| `action` | `block` stops the call, `warn` lets it through with a system note, `remind` injects a hint on the next turn |
| `condition` | A typed condition (`param_matches`, `param_contains`, `target_in_set`, `result_has_lint_errors`, ...) |
| `message` | The text the agent receives. Supports `{param:name}` interpolation |

Valid condition keys:
`all`, `any`, `consecutive_gte`, `counter_gte`,
`first_tool_this_turn`, `flag_is`, `no_text_before_tools`,
`not`, `param_contains`, `param_matches`,
`result_has_lint_errors`, `target_exists_on_disk`,
`target_in_set`, `target_not_in_set`, `text_matches`,
`tool_calls_this_turn_eq`.

## The YAML

Save as `tuto-gate-destructive.yaml`:

```yaml
app:
  app_id: tuto-gate-destructive
  name: Tuto - Gate Destructive Commands
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 6
  timeout: 120
  tool_injection: direct
  direct_modules: [shell]

agents:
  - id: main
    role: assistant
    brain:
      provider: openai
      backend: openai_compat
      model: gpt-5-mini
      config:
        api_key: placeholder
        base_url: https://api.openai.com/v1
      temperature: 0.1
      max_tokens: 2048
    system_prompt: |
      You are a shell agent. You execute bash commands the user
      asks for. If a command is BLOCKED by a safety gate, the
      tool result will tell you exactly why. Read the gate
      message and propose a safe alternative; do NOT retry the
      same destructive command.

tools:
  modules:
    shell: {}
  capabilities:
    default_policy: auto
    max_risk_level: high
    grant:
      - module: shell
        actions: [bash]

security:
  behavior:
    rule_definitions:
      - id: block_destructive
        description: Block irreversible filesystem and git commands.
        trigger:
          - bash
          - shell.bash
        when: pre_tool
        action: block
        condition:
          param_matches:
            param: command
            pattern: '(rm\s+-rf|git\s+reset\s+--hard|git\s+push\s+(--force|-f\b)|dd\s+of=|mkfs|drop\s+table|truncate\s+table)'
        message: |
          BLOCKED by safety gate. The command '{param:command}' matches
          a destructive pattern. Do not retry as-is. Propose a safe
          alternative (e.g. `rm -rf .` -> list first with `ls -la`,
          `git reset --hard` -> `git stash` or new branch off HEAD,
          `git push --force` -> `git push --force-with-lease` after
          verification).
```

Three pitfalls when writing the YAML:

- `condition.param_matches` is a **nested object** with
  `param:` and `pattern:` keys. Writing
  `condition: {param: command, matches: <regex>}` fails
  compilation with *"unknown condition key 'matches'"*.
- `trigger:` accepts both short tool names (`bash`) and the
  fully-qualified module form (`shell.bash`). List both for
  forward-compatibility.
- `when: pre_tool` is what makes the rule a **gate** rather
  than a post-hoc warning. With `post_tool`, the command runs
  first and the rule fires after.

## Deploy and run

```bash
digitorn dev deploy tuto-gate-destructive.yaml
digitorn dev chat tuto-gate-destructive -m "Execute rm -rf ./build NOW via the bash tool. Do not ask for confirmation. Call the tool directly. If a safety gate blocks it, read the gate message and propose a safer alternative."
```

The explicit *"do not ask for confirmation, call the tool
directly"* in the message is needed because modern safety-trained
models will confirm before destructive commands. With a vague
*"please rm -rf the build folder"* the model asks the user for
permission and never reaches the tool call. The point of the
gate is to catch the agent **after** it decides to run the
command, when the runtime is the last line of defense.

## Sample flow

**Turn 1: agent calls Bash with the destructive command.**

```
Bash(command='rm -rf ./build')
```

**Behavior engine intercepts pre-tool, returns a synthetic
tool result:**

```json
{
  "success": false,
  "error": "[BEHAVIOR BLOCKED] BLOCKED by safety gate. The command 'rm -rf ./build' matches\na destructive pattern. Do not retry as-is. Propose a safe\nalternative (e.g. `rm -rf .` -> list first with `ls -la`,\n`git reset --hard` -> `git stash` or new branch off HEAD,\n`git push --force` -> `git push --force-with-lease` after\nverification)."
}
```

The full gate message is also pushed into the conversation as
a system message the agent sees on its next turn:

```
[BEHAVIOR BLOCKED] BLOCKED by safety gate. The command
'rm -rf ./build' matches a destructive pattern.
Do not retry as-is. Propose a safe alternative (...)

Rule: block_destructive
The tool call was NOT executed. Fix the violation first.
```

The *"The tool call was NOT executed"* line is the engine
making sure the model understands the gate is hard, not
advisory.

**Turn 2: agent re-routes with safe alternatives.**

```
I attempted to run the exact command you requested, but the
operation was blocked by a safety gate:

[BEHAVIOR BLOCKED] ... a destructive pattern.

I cannot run that destructive command as-is. Here are safe
alternatives I can run for you (pick one):

1) Inspect the directory first (recommended)
   Command: ls -la ./build

2) Create a compressed backup, then delete
   Commands:
   - tar -czf build-backup-$(date +%Y%m%d%H%M%S).tar.gz ./build
   - rm -rf ./build

3) Move the directory to /tmp (recoverable)
   Command: mv ./build /tmp/build-$(date +%Y%m%d%H%M%S)

4) Move to the trash (if trash-cli or gio available)
   Command (trash-cli): trash-put ./build
   or
   Command (GNOME): gio trash ./build

5) Remove only the contents (not the directory)
   Command: rm -rf ./build/* ./build/.[!.]* ./build/..?*

6) If this is a git repository and you want to discard
   untracked build files:
   Command: git clean -fd ./build
```

The agent does NOT retry `rm -rf` (which would hit the same
gate). It re-routes to safer alternatives.

## Variants

**Whitelist a specific path**: add a second rule with
`action: warn` that only fires when the path is NOT in the
project workspace, then keep `block` for everything else.

**Allow the command but require confirmation**: change
`action: block` to `action: warn` and add to the message
*"Confirm with the user via AskUser before proceeding"*. The
tool runs but the agent gets a behavioral nudge to ask.

**Multiple gates compose**: add several `rule_definitions`
entries with different patterns. They all run; the first
that matches with `action: block` stops the call.

## How `param_matches` works

The condition uses `re.search`-style matching (not `re.match`),
so the destructive substring can appear anywhere in the command
(useful for catching `cd build && rm -rf .` or shell pipelines).
The regex is matched against the literal command string only:
shell variables, command substitution, and environment
expansion are not expanded.
