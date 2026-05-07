---
id: security
title: Security Architecture
sidebar_position: 11
---

# Security

Digitorn's security model has three independent surfaces, each
configured by its own block in the YAML and enforced at a different
layer:

| Block | Source | Layer | What it controls |
|-------|--------|-------|------------------|
| `tools.capabilities` | `schema.py` `CapabilitiesConfig` | Application - runs in-process before every tool call | Which actions the agent can call, with grant / approve / deny / risk gates. |
| `security.behavior` | `schema.py` `BehaviorConfig` | Behavioural - declarative rules + classifier injected into the agent loop | Pattern-based behaviour rules (read before edit, test after change, ...). |
| `security.sandbox` | `schema.py` `SandboxConfig` | OS kernel - Landlock / seccomp / namespaces / Job Objects | Process-level isolation (filesystem, network, process spawning). |

Every behaviour and field on this page maps to real code; entries
are cited with file + line.

## `tools.capabilities` - application security

`schema.py` `CapabilitiesConfig` (`extra: forbid`). Optional -
absence means dev/test mode (no enforcement). Production apps should
declare it explicitly.

```yaml
tools:
  capabilities:
    default_policy: auto              # auto | approve | block
    max_risk_level: medium            # low | medium | high
    grant:
      - { module: filesystem, actions: [read, glob, grep] }
      - { module: web,        actions: [search] }
    approve:
      - { module: shell,      actions: [bash] }
      - { module: filesystem, actions: [write, edit] }
    deny:
      - { module: workspace,  actions: [delete] }
      - { module: web,        actions: [download] }
    approval_timeout: 300             # seconds, [30, 3600]
    hidden_modules: []                # ids hidden from the agent index
    hidden_actions: []                # specific actions hidden
```

The compiler validates each `(module, action)` pair against the
loaded action registry - mistyped or non-existent actions raise a
compile error.

### Fields

`schema.py`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `default_policy` | `auto \| approve \| block` | `approve` | Action when no explicit grant matches. |
| `max_risk_level` | `low \| medium \| high` | `medium` | Cap on the risk level an action may declare. |
| `grant` | list[CapabilityGrant] | `[]` | Explicit allows. |
| `approve` | list[CapabilityGrant] | `[]` | Each call pauses for user approval. |
| `deny` | list[CapabilityGrant] | `[]` | Hard block. |
| `approval_timeout` | int [30, 3600] | `300` | Seconds before an unanswered approval auto-denies. |
| `hidden_modules` | list[string] | `[]` | Modules hidden from the agent index but still callable from setup steps / hooks / channels. |
| `hidden_actions` | list[CapabilityGrant] | `[]` | Specific actions hidden but executable internally. |

`CapabilityGrant` (`schema.py`) is `{module: str, actions:
list[str], reason: str}`. Empty `actions` means "all actions on the
module". `reason` is human-readable, surfaced on `deny` events.

### How a tool call is gated - the seven gates

`security.py` `security_gate`. Every tool call passes
through the same in-order gate sequence; the first violation raises
`PermissionDeniedError` (or `ApprovalRequiredError` at gate 4) and
the audit log records the decision with the gate name.

| Gate | Code label | Triggers when... |
|------|------------|-------------------|
| 0 | `gate0_inactive` | The app is deployed but not active. Admin profiles bypass. |
| 1a | `gate1_module` | The agent's profile can't access the module (`hidden_modules` or per-agent `modules` restriction filters it out). |
| 1b | `gate1_hidden` | The action is in `hidden_actions` for this module. |
| 2 | `gate2_risk` | Action's declared risk exceeds `max_risk_level`, with no explicit grant or per-action policy. |
| 3 | `gate3_permissions` | Action declares `required_permissions` (symbolic, e.g. `fs.write`, `net.http`) and the profile lacks them. |
| 4 | `gate4_policy` | Resolved action policy is `block` (denied) or `approve` (paused for HITL). |
| 5 | `gate5_classification` | Per-tool data-classification rule rejects the params (e.g. PII detected in a non-PII-allowed channel). |
| 6 | `gate6_rate_limit` | Per-action rate limit window exceeded. |

System modules (`context_builder`, `llm_provider`, `index`) bypass
the gates entirely - they're internal infrastructure, not
user-facing tools (`security.py`).

The infrastructure meta-actions (`execute_tool`, `search_tools`,
`get_tool`, `list_categories`, `browse_category`, `run_parallel`)
are also bypassed at the dispatcher level (`security.py`); the
gates apply to the **target** tool reached via `execute_tool`, not
to the dispatcher itself.

### Resolving a policy

`security.py::resolve_action_policy` (called at gate 4).
Resolution order:

1. **Explicit deny** in `tools.capabilities.deny` matching this
   `(module, action)` pair → `block`.
2. **Explicit approve** in `tools.capabilities.approve` → `approve`
   (wait for user OK).
3. **Explicit grant** in `tools.capabilities.grant` →
   `auto` (allowed, no friction).
4. **Per-grant default action policy** - when a grant matches the
   module without an explicit action policy, falls back to the
   grant's own `default_action_policy`.
5. **App-level `default_policy`** - final fallback (`approve` by
   default).

When the resolved policy is `approve`, the gate raises
`ApprovalRequiredError` and the runtime enqueues an entry in the
`ApprovalQueue`. The user picks `approve` / `deny` (or the app's
custom choices); the runtime resumes the turn with the choice
threaded back into the agent's context. If no answer comes within
`approval_timeout` seconds, the call auto-denies.

### Risk levels

Every `@action` declares a `risk_level` (`low`, `medium`, `high`)
in its decorator. `max_risk_level` (`schema.py`) caps what an
agent can call without an explicit grant - useful when you want to
allow most things but block destructive operations everywhere
without listing them one by one.

```yaml
tools:
  capabilities:
    max_risk_level: low               # only "low" actions auto-allowed
    grant:
      - { module: shell, actions: [bash] }   # explicit grant bypasses the cap
```

A `high`-risk action (e.g. `workspace.delete`, `shell.bash` in some
configurations) without an explicit grant is denied at gate 2.

### Hidden vs denied

| | `hidden_modules` / `hidden_actions` | `deny` |
|-|------------------------------------|--------|
| Visible to the agent | No (not in the tool index) | Yes (the agent can try) |
| Callable from setup steps / hooks / channels | Yes | No (gate 4 blocks) |
| Audit log entry on attempt | Filtered before reaching the audit | `denied` event with `gate1` or `gate4_policy` |

Use `hidden_*` to declutter the agent's toolset without breaking
internal automation. Use `deny` when the action must NEVER fire,
internal or not.

## `security.behavior` - runtime behavioural rules

`schema.py` `BehaviorConfig` (`extra: forbid`). The behaviour
engine watches every tool call and injects corrections into the
loop. Optional - absence means no behavioural enforcement.

```yaml
security:
  behavior:
    profile: coding                    # preset
    classify_turns: true               # semantic classifier on turn 0
    classifier:
      frequency: every_turn            # every_turn | first_turn | manual
      timeout: 15
      approaches: [direct, plan_and_confirm, delegate]
    brain:                             # cheap LLM for classification
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
    rules:
      read_before_edit: true
      test_after_changes: true
      no_bash_for_files: true
    custom:
      - id: protect_migrations
        rule: "Never modify migration files without asking"
        trigger: edit
        condition:
          path_matches: "alembic/versions/*"
        action: block
        message: "Migrations are append-only. Ask before editing."
    rule_definitions: []               # fully declarative rules
    state_tracking: null               # uses defaults from profile when null
```

### Fields

`schema.py`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `profile` | string \| null | `null` | Preset profile: `dev`, `coding`, `research`, `data`, `creative`, `assistant`. Or `{{behavior.X}}` to load from `behavior/X.yaml`. |
| `rules` | dict[str, Any] | `{}` | Override individual rule keys (`read_before_edit`, `test_after_changes`, ...) defined by the profile. |
| `custom` | list[BehaviorCustomRule] | `[]` | Legacy custom rules. Prefer `rule_definitions`. |
| `rule_definitions` | list[BehaviorRuleDefinition] | `[]` | Fully declarative rules - work for any module/action. |
| `state_tracking` | StateTrackingConfig \| null | `null` | What the session state tracks (read_files, edited_files, ...). Profile defaults apply when null. |
| `classify_turns` | bool | `false` | Enable semantic classification - a small LLM analyses each user message before the main agent acts. |
| `classifier` | ClassifierConfig | default-instance | Configuration for the semantic classifier. |
| `brain` | AgentBrain \| null | `null` | LLM dedicated to classification. Falls back to the coordinator's brain. |
| `use_agent_brain` | bool | `true` | When `brain` isn't set, reuse the coordinator's brain for classification. |

### Built-in profiles

Each profile bundles a set of rules and sensible defaults:

| Profile | Targets | Typical rules enabled |
|---------|---------|------------------------|
| `dev` | Permissive baseline for development | Most rules off, audit-only. |
| `coding` | Code-editing apps | `read_before_edit`, `no_bash_for_files`, `test_after_changes`, `verify_after_edit`. |
| `research` | Read-mostly research | `delegate_complex`, `cite_sources`. |
| `data` | Data-pipeline apps | `confirm_destructive` (on writes), `read_before_edit` for SQL, no `kill` on shell. |
| `creative` | Free-form creative | Minimal restrictions, `no_bash_for_files` to keep it sane. |
| `assistant` | General-purpose chat | Balanced default - modest restrictions, encouragement to plan. |

Custom profiles live in `behavior/X.yaml` in the bundle dir;
reference them with `profile: "{{behavior.X}}"`. The compiler
inlines the profile content at compile time.

### Three enforcement levels

Every rule declares `action: block | warn | remind`:

| Level | Effect |
|-------|--------|
| `block` | The tool call is prevented. The runtime injects a system message back into the loop with the rule's `message`. |
| `warn` | The tool call proceeds, but a warning is appended to the agent's next turn ("you violated rule X"). |
| `remind` | A post-tool hint is added to the result (no tool blocking). |

### Custom rules

`BehaviorCustomRule` (`schema.py`):

```yaml
security:
  behavior:
    custom:
      - id: protect_migrations
        rule: "Migrations are immutable - ask before editing."
        enforce: pre_tool                    # pre_tool | post_tool
        trigger: edit                        # tool name (or pattern)
        condition:
          path_matches: "alembic/versions/*"
        action: block                        # block | warn | remind
        message: "Cannot edit migrations without approval."
```

For more flexible matching (multiple triggers, complex conditions),
use `rule_definitions: [BehaviorRuleDefinition]` instead - same
shape but supports compositional conditions (`all_of`, `any_of`,
`not`) and works against any action.

Full rule reference: [Behavior Engine](43-behavior.md).

### Semantic classifier

When `classify_turns: true`, before the main agent acts on turn 0
the daemon sends the user message to a small classifier brain that
emits:

- **complexity** (`trivial` / `simple` / `moderate` / `complex`)
- **approach** (one of `classifier.approaches`)
- **risk** (`low` / `medium` / `high`)

These signals are injected into the main agent's prompt as
behavioural directives ("This is a complex task - plan before
acting. Risk: medium - confirm destructive operations.").

The classifier brain accepts the full `AgentBrain` shape - use a
cheap/fast model (`claude-haiku-4-5`, `deepseek-chat`,
`gpt-4o-mini`) to keep latency under a couple of seconds.

## `security.sandbox` - OS-level isolation

`schema.py` `SandboxConfig` (`extra: forbid`). Optional. When
set, the daemon spawns each session in an isolated worker with
kernel-level enforcement (Linux: Landlock + seccomp + cgroups +
namespaces; macOS: Seatbelt; Windows: Job Objects).

```yaml
security:
  sandbox:
    level: strict
    pool_size: 4
    pool_max: 12
    namespaces: [user, pid, net]
    workspace_snapshot: true
    audit: true
    session_timeout: 3600              # seconds, ≥ 60
    idle_timeout: 300                  # seconds, ≥ 30
    allow_paths:
      - "/data/models"
      - "~/datasets:rw"
      - "/etc/myapp"
    resources:
      memory: "2GB"
      cpu: 1.5
      processes: 64
```

### Levels

`schema.py`. Four presets, each adding capabilities to the
previous one:

| Level | What it gives you |
|-------|-------------------|
| `off` | No sandbox. Tools run with the daemon's own process privileges. **Avoid in production.** |
| `standard` (default) | Landlock filesystem restriction + seccomp syscall filter + cgroups resource limits. Single worker per session. |
| `strict` | + warm worker pool, user/PID namespaces, capability drop, MDWE (memory deny-write-execute). |
| `maximum` | + network namespace (sandboxed netns), seccomp-notify audit, CoW workspace snapshots. |

The presets compose with the explicit fields below - declare
`level: strict` and override individual fields if you need finer
control.

### Fields

`schema.py`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `level` | `off \| standard \| strict \| maximum` | `standard` | Preset. |
| `pool_size` | int [1, 32] | `2` | Pre-warmed workers in the pool. |
| `pool_max` | int [1, 64] | `8` | Maximum workers under load. `pool_size ≤ pool_max`. |
| `namespaces` | list[string] | `[]` | Linux namespaces to create: `user`, `pid`, `net`, `mount`. |
| `workspace_snapshot` | bool | `false` | Per-session CoW workspace snapshots - each session gets a private writable view of `runtime.workdir`. |
| `audit` | bool | `false` | Per-session audit trail (security event log). |
| `session_timeout` | int ≥ 60 | `3600` | Maximum session duration in seconds before auto-termination. |
| `idle_timeout` | int ≥ 30 | `300` | Idle timeout before worker recycling. |
| `allow_paths` | list[string] | `[]` | FS paths beyond the workspace. `path` (read-only) or `path:rw` (read-write). Supports `{{variables}}` and `~`. |
| `resources` | dict | `{}` | Per-worker limits: `memory` (e.g. `"512MB"`), `cpu` (cores, fractional ok), `processes` (max PIDs). |

### Platform support

| Backend | Status |
|---------|--------|
| Linux | Full support (Landlock + seccomp + namespaces + cgroups). Levels `standard`, `strict`, `maximum` all functional. |
| macOS | `standard` and `strict` via Seatbelt (sandbox-exec). `maximum` requires extra entitlements. |
| Windows | `standard` via Job Objects (process kill, memory cap, CPU cap). `strict` and `maximum` are advisory only - kernel-level FS / network restrictions aren't available the same way. |

When the daemon runs on a platform that can't honour the requested
level, it logs a warning at boot and degrades to whatever the OS
supports. Use `audit: true` to capture the actual enforcement
decisions per session.

For the kernel-level details (seccomp profile, Landlock rules,
seccomp-notify audit), see [OS Sandbox](35-sandbox.md).

## Audit log

Every gate decision fires `log_security_event(...)` with:

| Field | Description |
|-------|-------------|
| `app_id` | Identifier of the deployed app. |
| `agent_id` | Which agent the call was on. |
| `session_id` | Active session. |
| `module_id` / `action` | The (module, action) pair. |
| `risk_level` | Effective risk level used during gating. |
| `params` | Sanitised parameters (secrets redacted). |
| `decision` | `allowed`, `denied`, `approval_required`. |
| `gate` | The gate that produced the decision (`gate0_inactive`, `gate2_risk`, `gate4_policy`, ...). |
| `reason` | Human-readable explanation. |

The audit log is queryable via the daemon's admin API
(`GET /admin/audit-log?target_app_id=...&event_type=*`) or
directly in the `history_log` table where `kind='audit'`.
Filters compose (AND): `event_type` accepts trailing `*` for
wildcard match, plus `actor_user_id`, `target_user_id`,
`since_ts` / `until_ts` (ISO8601), `success_only`, `limit`,
`offset`. Admin-only.

## Cross-references

- Block-level reference: [App Configuration](02-app-config.md)
  - `tools.capabilities`, `security.behavior`, `security.sandbox`.
- Behavioural rules deep dive: [Behavior Engine](43-behavior.md)
  - every built-in rule, classifier prompt, custom rule format.
- OS sandbox details: [OS Sandbox](35-sandbox.md)
  - Landlock rules, seccomp profile, platform-specific behaviour.
- Credentials vault (separate from the gate engine):
  [credentials.md](../reference/runtime/credentials.md).
- Per-module security knobs (filesystem path sandboxing, web egress
  filtering, MCP server sandboxing, ...):
  [modules/index.md](../reference/modules/) and the per-module
  references under `modules/reference/`.
- Capabilities matrix in the canvas:
  [Multi-Tenant Installs](45-multi-tenant.md) covers the
  `(app_id, scope, owner_user_id)` triple that scopes every audit
  decision.
