---
id: behavior
title: "Behavior Engine — declarative runtime enforcement"
---

# Behavior Engine

The behavior engine is a fully YAML-driven runtime enforcement system. It monitors every tool call, tracks session state, detects violations, and injects corrections — all configurable per-app, for any domain.

Two enforcement layers:

1. **Rule engine** — declarative rules evaluated pre/post every tool call. Block, warn, or remind the agent.
2. **Semantic classifier** (optional) — a small LLM analyzes each user message, classifies the task, and injects behavioral directives before the agent acts.

Nothing is hardcoded. Rules, state tracking, complexity levels, approaches, risk levels, directive format — everything comes from the YAML.

## Quick start

```yaml
# Minimal — built-in profile
behavior:
  profile: coding

# With semantic classification
behavior:
  profile: dev
  classify_turns: true

# Full custom — no built-in profile, pure YAML
behavior:
  classify_turns: true
  rule_definitions:
    - id: backup_before_modify
      trigger: [database.execute]
      when: pre_tool
      action: block
      condition:
        all:
          - param_matches: {param: query, pattern: "(UPDATE|DELETE|DROP)"}
          - flag_is: {name: backup_created, value: false}
      message: "Create a backup before running '{param:query}'."
  state_tracking:
    flags:
      backup_created:
        set_on: [database.backup]
```

## Bundle directory

Custom behavior profiles live in the `behavior/` directory, alongside prompts and skills:

```
my-app/
  app.yaml
  prompts/
  skills/
  behavior/             # referenced via {{behavior.X}}
    strict_dev.yaml
    research.yaml
  fragments/
```

Reference a custom profile:

```yaml
behavior:
  profile: "{{behavior.strict_dev}}"
```

See [Bundle namespaces](38-bundle-namespaces.md) for the full namespace system.

### Custom profile format (`behavior/strict_dev.yaml`)

```yaml
name: strict_dev
description: "Ultra-strict developer rules for production code"
extends: dev                     # inherit from a built-in profile

rules:
  max_blind_reads: 1
  changes_before_test_reminder: 1

prompt: |
  You follow strict discipline:
  - NEVER edit a file you haven't read in this session
  - Run tests after EVERY change, no matter how small
  - If tests fail, fix them before moving on

custom:
  - id: protect_migrations
    rule: "Never modify migration files without asking"
    trigger: edit
    condition:
      param: file_path
      contains: "migration"
    action: block
```

| Field | Description |
|-------|-------------|
| `name` | Display name (shown in prompt section title) |
| `description` | One-line description (passed to classifier for context) |
| `extends` | Inherit from a built-in profile: `dev`, `coding`, `research`, `data`, `creative`, `assistant` |
| `rules` | Rule overrides merged on top of the base profile |
| `prompt` | Custom behavioral instructions injected into the system prompt |
| `custom` | Legacy custom rules appended to the profile's rule list |

---

## Configuration reference

```yaml
behavior:
  # ── Profile ──
  profile: dev                       # built-in or "{{behavior.X}}"

  # ── Legacy boolean rule flags (backward compat) ──
  rules:
    read_before_edit: true
    max_blind_reads: 2
    changes_before_test_reminder: 3

  # ── Declarative rules (preferred, works for ANY action) ──
  rule_definitions:
    - id: my_rule
      trigger: [edit]
      when: pre_tool
      action: warn
      condition: { target_not_in_set: read_files }
      message: "Read '{target}' first."

  # ── State tracking (what the session tracks) ──
  state_tracking:
    sets: { ... }
    counters: { ... }
    flags: { ... }

  # ── Legacy custom rules ──
  custom:
    - id: protect_env
      rule: "..."
      trigger: edit
      action: block

  # ── Semantic classifier ──
  classify_turns: true
  classifier:
    frequency: every_turn
    timeout: 15
    complexity_levels: [...]
    approaches: [...]
    risk_levels: [...]
    system_prompt: null

  # ── Classifier brain ──
  brain:
    provider: deepseek
    model: deepseek-chat
    config:
      api_key: "{{env.DEEPSEEK_API_KEY}}"
  use_agent_brain: true
```

### Top-level fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `profile` | string | `null` | Built-in preset or `{{behavior.X}}` reference |
| `rules` | dict | `{}` | Boolean flags + numeric thresholds (backward compat) |
| `rule_definitions` | list | `[]` | Fully declarative rules (see below) |
| `state_tracking` | object | `null` | Custom state tracking config (null = profile defaults) |
| `custom` | list | `[]` | Legacy custom rules (prefer `rule_definitions`) |
| `classify_turns` | bool | `false` | Enable the semantic classifier LLM |
| `classifier` | object | `{}` | Classifier configuration (see below) |
| `brain` | AgentBrain | `null` | LLM for classifier. Omit = coordinator's brain |
| `use_agent_brain` | bool | `true` | Reuse coordinator's brain when `brain` is null |

---

## Built-in profiles

Profiles load sensible defaults. Override any setting with `rules:` or `rule_definitions:`.

### `dev`

The highest standard. How a senior developer works: understand before acting, search before reading, plan before implementing, verify after changing, delegate when appropriate.

Injects an advanced behavioral guide into the system prompt (5000+ chars) covering: how to think, explore, implement, verify, handle uncertainty, use sub-agents, and communicate.

### `coding`

Like `dev` but with higher autonomy. Does small changes alone, asks before big ones.

### `research`

For research teams and report generation. Enables web search, delegation, and planning. Disables file-editing rules (research doesn't edit code). High verbosity.

### `data`

For data analysis and ETL pipelines. Enables read-before-edit, test-after-changes, lint checks, web search, and destructive protection.

### `creative`

For writing apps and content generation. Low autonomy (asks before acting), high verbosity, planning required.

### `assistant`

For general-purpose chatbots and Q&A. Minimal enforcement, only web search and destructive protection.

### Profile comparison

| Rule | dev | coding | research | data | creative | assistant |
|------|:---:|:------:|:--------:|:----:|:--------:|:---------:|
| read_before_edit | x | x | | x | x | x |
| search_before_read | x | x | | | | |
| test_after_changes | x | x | | x | | |
| verify_after_edit | x | x | | x | | |
| no_bash_for_files | x | x | x | x | x | x |
| confirm_destructive | x | x | x | x | x | x |
| plan_before_execute | x | x | x | x | x | |
| delegate_complex | x | x | x | x | | |
| web_search_when_unknown | x | | x | x | x | x |
| always_lint_check | x | x | | x | x | |

---

## Declarative rules (`rule_definitions`)

The core of the system. Each rule is a fully declarative definition that works for **any** tool, not just filesystem operations.

### Rule format

```yaml
rule_definitions:
  - id: my_rule                    # Unique identifier
    description: "Human explanation" # Injected into the system prompt
    trigger: [edit, write]          # Tool(s) that trigger this rule. "*" = all.
    when: pre_tool                  # pre_tool | post_tool | on_text
    action: warn                    # block | warn | remind
    condition:                      # When the rule fires (see conditions)
      target_not_in_set: read_files
    message: "You are editing '{target}' without reading it first."
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `id` | yes | | Unique rule identifier (shown in violation messages) |
| `description` | no | `""` | Human-readable description (injected in system prompt) |
| `trigger` | no | `"*"` | Tool names that trigger this rule. Matches bare names (`edit`), FQN (`filesystem.edit`), or `"*"` for all. |
| `when` | no | `pre_tool` | When to evaluate: `pre_tool` (before execution), `post_tool` (after), `on_text` (on agent text) |
| `action` | no | `warn` | What to do: `block` (prevent execution), `warn` (inject message), `remind` (post-tool hint) |
| `condition` | no | `{}` | When the rule fires. Empty = always fires. See conditions below. |
| `message` | no | `""` | Message template with placeholders (see message templates) |

### Action levels

| Action | Effect | Tool executes? |
|--------|--------|:--------------:|
| `block` | `[BEHAVIOR BLOCKED]` message injected. Tool call is **prevented**. | no |
| `warn` | `[BEHAVIOR WARNING]` message injected. Tool still executes. | yes |
| `remind` | `[BEHAVIOR REMINDER]` message injected after execution. | yes |

### Trigger matching

Triggers match tool names flexibly:

| Trigger value | Matches |
|---------------|---------|
| `"edit"` | `edit`, `filesystem.edit`, `filesystem__edit` |
| `"filesystem.edit"` | `filesystem.edit`, `edit` |
| `["edit", "write"]` | both tools |
| `"*"` | any tool |
| `"database.query"` | `database.query`, `query` |

---

## Conditions

Conditions determine **when** a rule fires. All conditions return true/false.

### State conditions

| Condition | Description | Example |
|-----------|-------------|---------|
| `target_not_in_set: X` | Target param NOT in tracked set X | `target_not_in_set: read_files` |
| `target_in_set: X` | Target param IS in tracked set X | `target_in_set: edited_files` |
| `counter_gte: {name, value}` | Counter >= threshold | `counter_gte: {name: changes_since_test, value: 3}` |
| `flag_is: {name, value}` | Flag equals value | `flag_is: {name: backup_created, value: false}` |

### Param conditions

| Condition | Description | Example |
|-----------|-------------|---------|
| `param_matches: {param, pattern}` | Param value matches regex | `param_matches: {param: command, pattern: "rm\\s+-rf"}` |
| `param_contains: {param, value}` | Param contains string (case-insensitive) | `param_contains: {param: file_path, value: "migration"}` |

### Turn conditions

| Condition | Description | Example |
|-----------|-------------|---------|
| `no_text_before_tools` | Agent didn't write text before first tool | `no_text_before_tools: true` |
| `first_tool_this_turn` | This is the first tool call of the turn | `first_tool_this_turn: true` |
| `consecutive_gte: N` | Same tool called N+ times in a row | `consecutive_gte: 5` |
| `tool_calls_this_turn_eq: N` | Exactly N tool calls this turn | `tool_calls_this_turn_eq: 8` |

### Result conditions

| Condition | Description | Example |
|-----------|-------------|---------|
| `target_exists_on_disk` | File exists on disk | `target_exists_on_disk: true` |
| `result_has_lint_errors` | Tool result contains lint errors | `result_has_lint_errors: true` |
| `text_matches: pattern` | Agent text matches regex (for `on_text` rules) | `text_matches: "not sure\|uncertain"` |

### Composite conditions

Combine conditions with `all`, `any`, `not`:

```yaml
condition:
  all:
    - param_matches: {param: query, pattern: "(UPDATE|DELETE)"}
    - flag_is: {name: backup_created, value: false}
    - not:
        target_in_set: verified_tables
```

```yaml
condition:
  any:
    - param_contains: {param: command, value: "rm -rf"}
    - param_contains: {param: command, value: "git reset --hard"}
```

```yaml
condition:
  not:
    flag_is: {name: user_confirmed, value: true}
```

---

## Message templates

Rule messages support placeholders that are resolved at runtime:

| Placeholder | Description | Example output |
|-------------|-------------|----------------|
| `{target}` | Primary target param (file_path, url, query...) | `src/auth.py` |
| `{tool}` | Current tool bare name | `edit` |
| `{turn}` | Current turn number | `3` |
| `{param:X}` | Any tool param by name | `{param:command}` -> `rm -rf /tmp` |
| `{counter:X}` | Counter value | `{counter:changes_since_test}` -> `5` |
| `{set_count:X}` | Size of a tracked set | `{set_count:read_files}` -> `12` |
| `{flag:X}` | Flag value | `{flag:backup_created}` -> `True` |

Example:

```yaml
message: >
  You have made {counter:changes_since_test} changes since last test.
  Files edited: {set_count:edited_files}. Run tests now.
```

---

## State tracking (`state_tracking`)

State tracking defines **what the session remembers** across tool calls. Fully declarative — works for any domain.

### Sets

Track targets (file paths, URLs, table names...) per tool:

```yaml
state_tracking:
  sets:
    read_files:
      add_on: [read, filesystem.read]    # tools that add to this set
      target: file_path                   # param name to extract
      aliases: [path, filepath]           # alternative param names
    fetched_urls:
      add_on: [web.fetch]
      target: url
    tables_queried:
      add_on: [database.query, database.execute]
      target: table
```

Rules reference sets via `target_not_in_set` / `target_in_set` / `{set_count:X}`.

### Counters

Track "how many X since last Y" patterns:

```yaml
state_tracking:
  counters:
    changes_since_test:
      increment_on: [edit, write]        # +1 when these tools are called
      reset_on: []                        # reset to 0 on these tools
      reset_when:                         # conditional reset
        tool: bash,shell.bash             # comma-separated tool names
        param: command                    # which param to check
        matches: "pytest|npm test"        # regex — reset when matched
    queries_since_schema:
      increment_on: [database.query]
      reset_on: [database.schema, database.describe]
    sources_since_synthesis:
      increment_on: [web.fetch]
      reset_on: [memory.remember]
```

Rules reference counters via `counter_gte: {name, value}` / `{counter:X}`.

### Flags

Track boolean states:

```yaml
state_tracking:
  flags:
    backup_created:
      set_on: [database.backup]           # set to True
      unset_on: []                        # set to False (optional)
    has_web_searched:
      set_on: [web.search, search]
    user_confirmed:
      set_on: [context_builder.ask_user]
```

Rules reference flags via `flag_is: {name, value}` / `{flag:X}`.

### Default tracking

When `state_tracking` is null, the engine uses defaults appropriate for the profile. The coding/dev profiles track: `read_files`, `edited_files`, `written_files`, `searched_patterns` (sets), `reads_since_search`, `changes_since_test` (counters), `has_web_searched` (flag).

---

## Examples by domain

### Coding assistant

```yaml
behavior:
  profile: dev
  classify_turns: true
  rules:
    max_blind_reads: 2
    changes_before_test_reminder: 2
  rule_definitions:
    - id: protect_migrations
      trigger: [edit, write]
      when: pre_tool
      action: block
      condition:
        param_contains: {param: file_path, value: "migration"}
      message: "Migration files are protected. Ask user first."
```

### Database pipeline

```yaml
behavior:
  classify_turns: true
  classifier:
    complexity_levels:
      - name: query
        when: "Single SELECT, no side effects"
        behavior: "Execute directly"
      - name: transform
        when: "UPDATE/INSERT affecting rows"
        behavior: "Verify row count after execution"
      - name: migration
        when: "ALTER/DROP/CREATE schema changes"
        behavior: "MUST get user approval, run in transaction"
    approaches:
      - name: direct
        label: "Execute directly"
        when: "Safe read-only query"
      - name: dry_run_first
        label: "Dry-run before executing"
        when: "Bulk data modification"
        behavior: "Run with LIMIT 10 first, then full query"
      - name: plan_and_confirm
        label: "Plan and get approval"
        when: "Schema changes or data deletion"
        behavior: "Show the SQL plan, wait for user approval"
    risk_levels:
      - name: read_only
        when: "SELECT queries"
      - name: write
        when: "INSERT/UPDATE operations"
      - name: destructive
        when: "DELETE/DROP/TRUNCATE"
        behavior: "MUST confirm with user"
    directive_prefix: "[DATA DIRECTIVE — {complexity}, {risk} risk]"
    high_risk_threshold: write

  rule_definitions:
    - id: backup_before_modify
      trigger: [database.execute]
      when: pre_tool
      action: block
      condition:
        all:
          - param_matches: {param: query, pattern: "(UPDATE|DELETE|ALTER|DROP)"}
          - flag_is: {name: backup_created, value: false}
      message: "BLOCKED: Create a backup before running '{param:query}'."

    - id: verify_row_count
      trigger: [database.execute]
      when: post_tool
      action: remind
      condition:
        param_matches: {param: query, pattern: "(UPDATE|DELETE)\\s"}
      message: "Verify the affected row count matches expectations."

    - id: no_select_star
      trigger: [database.query]
      when: pre_tool
      action: warn
      condition:
        param_matches: {param: query, pattern: "SELECT\\s+\\*\\s+FROM"}
      message: "Avoid SELECT * — specify columns to reduce data transfer."

    - id: schema_before_query
      trigger: [database.query]
      when: pre_tool
      action: warn
      condition:
        counter_gte: {name: queries_since_schema, value: 3}
      message: "You ran {counter:queries_since_schema} queries without checking the schema."

  state_tracking:
    sets:
      tables_queried:
        add_on: [database.query, database.execute]
        target: table
      tables_modified:
        add_on: [database.execute]
        target: table
    counters:
      queries_since_schema:
        increment_on: [database.query]
        reset_on: [database.schema, database.describe]
    flags:
      backup_created:
        set_on: [database.backup]
```

### Research team

```yaml
behavior:
  classify_turns: true
  classifier:
    complexity_levels: [quick, standard, deep]
    approaches:
      - name: answer_directly
        label: "Answer from knowledge"
        when: "Well-known fact, high confidence"
        behavior: "State the answer with a citation if available"
      - name: verify_then_answer
        label: "Verify first"
        when: "Probably correct but should double-check"
        behavior: "One quick search, then answer"
      - name: multi_source
        label: "Cross-reference sources"
        when: "Conflicting information or evolving topic"
        behavior: "Search 3+ independent sources, compare findings"
    risk_levels:
      - name: factual
        when: "Well-established facts"
      - name: uncertain
        when: "Evolving field, conflicting sources"
        behavior: "Flag uncertainty, present competing views"
      - name: speculative
        when: "Predictions, extrapolations"
        behavior: "Label as speculative, never present as fact"
    directive_prefix: "[RESEARCH — {complexity}, {risk}]"
    directive_footer: "Prioritize accuracy over speed."

  rule_definitions:
    - id: cite_sources
      trigger: [web.fetch]
      when: post_tool
      action: remind
      message: "You fetched a source. Remember to cite it."

    - id: cross_reference
      trigger: [memory.remember]
      when: pre_tool
      action: remind
      condition:
        counter_gte: {name: sources_collected, value: 3}
      message: "You have {counter:sources_collected} sources. Cross-reference before adding more."

    - id: max_fetches_per_angle
      trigger: [web.fetch]
      when: pre_tool
      action: warn
      condition:
        consecutive_gte: 5
      message: "5 fetches in a row. Synthesize what you have first."

  state_tracking:
    sets:
      fetched_urls:
        add_on: [web.fetch]
        target: url
      search_queries:
        add_on: [web.search]
        target: query
    counters:
      sources_collected:
        increment_on: [web.fetch]
        reset_on: [memory.remember]
    flags:
      synthesis_done:
        set_on: [memory.remember]
        unset_on: [web.fetch]
```

---

## Semantic classifier (`classifier`)

The classifier is a generic pre-turn analysis engine. Each app configures what it analyzes, when it runs, and what it produces.

### Classifier configuration

```yaml
classifier:
  # ── When to run ──
  frequency: every_turn        # every_turn | first_turn | every_n_turns | on_new_message
  frequency_n: 3               # for every_n_turns
  skip_followups: true         # auto-skip "yes", "ok", "continue", "oui", "valide"...
  timeout: 15                  # max seconds for classifier LLM

  # ── Output schema ──
  complexity_levels: [...]     # string or {name, when, behavior}
  approaches: [...]            # string or {name, label, when, behavior}
  risk_levels: [...]           # string or {name, when, behavior}
  max_directives: 5

  # ── What context the classifier receives ──
  context:
    tool_inventory: true       # tool names + descriptions
    session_state: true        # files read, edits, violations, turn...
    workspace_info: true       # project type, languages, file count
    recent_history: true       # last N messages with tool calls
    history_depth: 8

  # ── System prompt (null = built-in default) ──
  system_prompt: null
  # system_prompt: "{{prompt.my_classifier}}"

  # ── Directive formatting ──
  directive_prefix: "[BEHAVIOR DIRECTIVE — {complexity} complexity, {risk} risk]"
  high_risk_warning: "Risk: {risk}. Confirm with user before proceeding."
  high_risk_threshold: medium
  directive_footer: "Follow these directives."
```

### Frequency modes

| Mode | Description |
|------|-------------|
| `every_turn` | Before every agent turn. The classifier can skip via `skip_reason`. |
| `first_turn` | Only the first turn of a session. |
| `every_n_turns` | Every N turns (set `frequency_n`). |
| `on_new_message` | Only when a new user message is present. |

When `skip_followups: true`, simple messages like "ok", "yes", "continue", "oui", "valide" skip classification automatically.

### Structured levels and approaches

Each entry can be a plain string or a dict with `name`, `label`, `when`, `behavior`:

```yaml
# Simple — just names (classifier uses built-in guidance)
approaches: [direct, plan_and_confirm, delegate]

# Structured — full control over classifier behavior
approaches:
  - name: direct
    label: "Execute directly"
    when: "Task is trivial, clear path"
    behavior: "Go straight to tool calls"
  - name: ask_expert
    label: "Needs human expertise"
    when: "Domain knowledge the agent lacks"
    behavior: "Use AskUser, explain what you need to know"
```

| Field | Description | Where it appears |
|-------|-------------|-----------------|
| `name` | The value in the classifier's JSON output | Output schema |
| `label` | Human-readable text shown to the agent | `Approach: <label>` in directive |
| `when` | Guidance for the classifier on when to pick this value | Classifier system prompt |
| `behavior` | How the agent should behave when this is chosen | Classifier system prompt |

### What the classifier receives

The classifier sees a rich context (all configurable via `context:`):

1. **User message** — what the user just asked
2. **Tool inventory** — exact tool names with descriptions (not just module names)
3. **Session state** — generic snapshot: all tracked sets, counters, flags, recent tool history
4. **Workspace context** — project type, languages, file count, framework
5. **Active rules** — names of enforced rules
6. **Behavior profile** — custom profile name, description, instructions, custom rules
7. **Recent history** — last N messages including tool calls with arguments and results

### Custom system prompt

Replace the built-in behavioral model entirely:

```yaml
classifier:
  system_prompt: |
    You decide how the agent should approach each user request.
    Output JSON: {"complexity": "...", "approach": "...", "directives": [...]}
    
    For data questions: use "direct" approach.
    For schema changes: use "plan_and_confirm".
    For bulk operations: use "dry_run_first".
```

Or load from a prompt file:

```yaml
classifier:
  system_prompt: "{{prompt.classifier}}"
```

### Directive format customization

```yaml
classifier:
  directive_prefix: "[DATA DIRECTIVE — {complexity}, {risk} risk]"
  high_risk_warning: "DATA RISK: {risk}. Run dry-run first."
  high_risk_threshold: write              # from your risk_levels list
  directive_footer: "Follow data governance policy."
```

Placeholders in templates: `{complexity}`, `{approach}`, `{risk}`, `{approach_label}`.

### Classifier brain

```yaml
# Option 1: reuse coordinator's brain (default)
behavior:
  classify_turns: true

# Option 2: dedicated fast model (saves cost)
behavior:
  classify_turns: true
  brain:
    provider: deepseek
    model: deepseek-chat
    config:
      api_key: "{{env.DEEPSEEK_API_KEY}}"

# Option 3: local model for zero-cost classification
behavior:
  classify_turns: true
  brain:
    provider: ollama
    model: qwen2.5:3b
    config:
      base_url: "http://localhost:11434/v1"
```

---

## Session isolation

Each session has its own state. Two sessions running the same app never interfere. State is cleaned up when the session ends.

The session state is generic — it holds whatever the `state_tracking` config defines:

- **Sets**: named collections of strings (files, URLs, tables...)
- **Counters**: named integers for "X since Y" patterns
- **Flags**: named booleans
- **Tool history**: ordered list of recent tool calls with targets
- **Universal**: turn number, total tool calls, violations, consecutive tool count

### State snapshot (what the classifier sees)

```
Turn: 2
Total tool calls: 8
Files read: 3 (recent: src/auth.py, src/models.py, src/router.py)
Edited files: 1 (src/auth.py)
Changes Since Test: 1
Reads Since Search: 2
Active flags: has_web_searched
Recent actions: grep(auth) -> read(src/auth.py) -> read(src/models.py) -> edit(src/auth.py)
```

For a database app:

```
Turn: 1
Total tool calls: 5
Tables Queried: 4 (users, orders, t0, t1)
Queries Since Schema: 3
Active flags: backup_created
Recent actions: database.backup -> database.execute(UPDATE...) -> database.query(SELECT...)
```

---

## How enforcement works

```
User sends message
     |
     +-- behavior.on_turn_start() -> reset per-turn state
     |
     +-- behavior.classify_turn() -> classifier LLM (if enabled)
     |     -> frequency check (skip if not this turn's schedule)
     |     -> followup check (skip "ok", "yes", "continue"...)
     |     -> build context (tools, state, history, profile)
     |     -> call LLM -> parse JSON -> format directive
     |     -> inject [BEHAVIOR DIRECTIVE] message
     |
     +-- LLM generates response (sees directive + rules in prompt)
     |
     +-- For EACH tool call:
     |   +-- evaluate rule_definitions where when=pre_tool
     |   |   -> condition match? inject [WARNING/BLOCKED]
     |   |   -> blocked? tool does NOT execute
     |   |
     |   +-- tool executes (if not blocked)
     |   |
     |   +-- update_state() -> sets, counters, flags updated
     |   |
     |   +-- evaluate rule_definitions where when=post_tool
     |       -> condition match? inject [REMINDER]
     |
     +-- Next turn...
```

### Priority order

When both `rule_definitions` and legacy `rules` (boolean flags) are present:

1. Explicit `rule_definitions` take priority (matched by `id`)
2. Boolean flags from profile/rules expand to default definitions
3. Legacy `custom` rules are converted to the new format

This means you can gradually migrate from boolean flags to full rule definitions without breaking anything.

---

## Built-in rule reference

These rules ship with the default profiles. Each is a declarative definition internally — you can override any of them by defining a `rule_definitions` entry with the same `id`.

### Sequence rules

| ID | Trigger | When | Action | Description |
|----|---------|------|--------|-------------|
| `read_before_edit` | edit | pre | warn | File must be in `read_files` set before editing |
| `read_before_write_existing` | write | pre | warn | Existing file must be read before overwriting |
| `search_before_read` | read | pre | warn | After N blind reads, suggest Grep/Glob first |
| `verify_after_edit` | edit | post | remind | Re-read modified section after editing |
| `test_after_changes` | edit, write | post | remind | Run tests after N changes |

### Prohibition rules

| ID | Trigger | When | Action | Description |
|----|---------|------|--------|-------------|
| `no_bash_for_files` | bash | pre | warn | Detects cat/sed/awk in bash commands |
| `no_blind_exploration` | bash | pre | warn | Detects find/ls -la/tree in bash commands |
| `confirm_destructive` | bash | pre | **block** | Blocks rm -rf, git reset --hard, DROP TABLE |

### Cognitive rules

| ID | Trigger | When | Action | Description |
|----|---------|------|--------|-------------|
| `plan_before_execute` | * | pre | warn | Agent must write text before first tool |
| `web_search_when_unknown` | * | on_text | warn | Detects uncertainty phrases in agent text |
| `delegate_complex` | * | post | remind | After 8+ tool calls, suggest sub-agents |
| `delegate_large_reads` | read | post | remind | After 5+ sequential reads, suggest delegation |
| `max_sequential_same_tool` | * | pre | warn | Same tool N+ times in a row |
| `always_lint_check` | edit, write | post | warn | Checks lint errors in tool result |

### Numeric thresholds

| Parameter | Default | Used by |
|-----------|---------|---------|
| `max_blind_reads` | 3 | `search_before_read` |
| `changes_before_test_reminder` | 3 | `test_after_changes` |
| `max_sequential_same_tool` | 8 | `max_sequential_same_tool` |

Override in `rules:`:

```yaml
behavior:
  profile: dev
  rules:
    max_blind_reads: 1
    changes_before_test_reminder: 1
    max_sequential_same_tool: 5
```
