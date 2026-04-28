---

id: security
title: Security Architecture
sidebar_position: 2
format: md
---



# Security Architecture

LLMOS Bridge implements defense-in-depth security with multiple independent layers. Every plan submission passes through fast heuristic screening, optional ML-based analysis, LLM-driven intent verification, profile-based permission enforcement, and per-action decorator checks - all before any module code executes.

---


## Security Layers

```
Plan submission arrives
    |
    v
[Layer 1.3] Scanner Pipeline ──── <1ms
    |   HeuristicScanner (35 patterns, 9 categories)
    |   LLMGuardScanner (DeBERTa, optional)
    |   PromptGuardScanner (Meta 86M, optional)
    |
    +--→ REJECT → 403 with scan details
    |
    v
[Layer 1.5] IntentVerifier ──── ~200ms (background)
    |   LLM-based semantic analysis
    |   ThreatCategoryRegistry (7 built-in categories)
    |   PromptComposer (dynamic prompt assembly)
    |   LLM providers: Anthropic, OpenAI, Ollama, custom
    |
    +--→ REJECT → SecurityError
    |
    v
[Layer 2.0] PermissionGuard ──── <1ms
    |   Profile check (readonly/local_worker/power_user/unrestricted)
    |   Approval gate check
    |   Sandbox path enforcement (symlink-safe)
    |
    +--→ DENY → PermissionDeniedError / ApprovalRequiredError
    |
    v
[Layer 3.0] Action Decorators ──── per-action, runtime
    |   @requires_permission → PermissionManager.check_or_raise()
    |   @rate_limited → ActionRateLimiter.check_or_raise()
    |   @intent_verified → IntentVerifier.verify_action()
    |   @sensitive_action → emit audit event
    |   @audit_trail → before/after logging
    |   @data_classification → tag output
    |
    v
Module action executes
    |
    v
[Layer 4.0] OutputSanitizer
    |   Scrub module output before LLM injection
    |   8 injection pattern detections
    |   String truncation, depth limiting
    |
    v
Clean result returned to agent
```

---


## Scanner Pipeline

The scanner pipeline provides ultra-fast pre-LLM screening. It runs synchronously before any expensive operations.

### Architecture

```
SecurityPipeline
    |
    +--→ ScannerRegistry (lifecycle: register/enable/disable)
    |
    +--→ For each enabled scanner (ordered by priority):
    |      |
    |      +--→ scanner.scan(plan_text, context) → ScanResult
    |      |
    |      +--→ If fail_fast and result.verdict == REJECT → short-circuit
    |
    +--→ Aggregate: PipelineResult
             |
             +--→ max_risk_score vs reject_threshold (0.7) → REJECT
             +--→ max_risk_score vs warn_threshold (0.3) → WARN
             +--→ Otherwise → ALLOW
```

### ScanVerdict

| Verdict | Meaning |
|---------|---------|
| `ALLOW` | Input is safe |
| `WARN` | Suspicious but not blocked |
| `REJECT` | Input blocked |

### ScanResult

| Field | Type | Description |
|-------|------|-------------|
| `scanner_id` | string | Scanner that produced this result |
| `verdict` | ScanVerdict | ALLOW, WARN, or REJECT |
| `risk_score` | float | 0.0 to 1.0 |
| `threat_types` | list | Detected threat categories |
| `details` | string | Human-readable explanation |
| `matched_patterns` | list | Pattern IDs that matched |
| `scan_duration_ms` | float | Scanner execution time |

### PipelineResult

| Field | Type | Description |
|-------|------|-------------|
| `allowed` | bool | Final decision |
| `aggregate_verdict` | ScanVerdict | Worst verdict across all scanners |
| `max_risk_score` | float | Highest risk score |
| `scanner_results` | list | Individual ScanResult per scanner |
| `short_circuited` | bool | Whether fail_fast terminated early |
| `total_duration_ms` | float | Total pipeline execution time |

### Built-in Scanners

#### HeuristicScanner

Ultra-fast regex-based pattern matching. 35 default patterns across 9 threat categories. Typical execution: <1ms.

**Pattern categories**:
1. Prompt injection attempts
2. Privilege escalation keywords
3. Data exfiltration patterns
4. Suspicious command sequences
5. Intent misalignment indicators
6. Obfuscated payloads (base64 decoding)
7. Resource abuse patterns
8. Shell injection attempts
9. Path traversal patterns

Each `PatternRule` has:
- `id`: unique identifier
- `category`: threat category
- `pattern`: compiled regex
- `severity`: 0.0 to 1.0
- `description`: human-readable
- `enabled`: can be disabled per-pattern

**Base64 detection**: The scanner automatically detects and decodes base64 payloads, checking decoded content for suspicious keywords.

**Extensibility**: Custom patterns can be added via configuration:
```yaml
scanner_pipeline:
  heuristic_extra_patterns:
    - id: "custom_001"
      category: "custom"
      pattern: "dangerous_keyword"
      severity: 0.8
      description: "Custom pattern"
```

#### LLMGuardScanner (Optional)

Wraps the `llm-guard` library (DeBERTa-based models). Provides ML-based injection detection.

Install: `pip install llm-guard`

#### PromptGuardScanner (Optional)

Wraps Meta's Prompt Guard 86M model via HuggingFace transformers.

Install: `pip install transformers torch`

### ScannerRegistry

Manages scanner lifecycle:

| Method | Description |
|--------|-------------|
| `register(scanner)` | Add scanner to registry |
| `unregister(scanner_id)` | Remove scanner |
| `enable(scanner_id)` | Enable scanner |
| `disable(scanner_id)` | Disable scanner |
| `is_enabled(scanner_id)` | Check status |
| `list_all()` | All registered scanners |
| `list_enabled()` | Only enabled scanners |

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/security/scanners` | List all scanners with status |
| POST | `/security/scanners/scan` | Dry-run scan on input |
| POST | `/security/scanners/{id}/enable` | Enable a scanner |
| POST | `/security/scanners/{id}/disable` | Disable a scanner |

---


## Intent Verification

LLM-based semantic analysis of plans before execution. Detects intent misalignment, prompt injection, and suspicious action combinations that heuristic patterns cannot catch.

### Architecture

```
IntentVerifier
    |
    +--→ PromptComposer (assembles dynamic system prompt)
    |      |
    |      +--→ ThreatCategoryRegistry (7 built-in + custom)
    |      +--→ Critical rules, output format, verdict guidelines
    |
    +--→ LLMClient (pluggable provider)
    |      |
    |      +--→ AnthropicLLMClient
    |      +--→ OpenAILLMClient
    |      +--→ OllamaLLMClient
    |      +--→ Custom (via class path)
    |
    +--→ Cache (LRU, plan_hash → VerificationResult)
    |
    +--→ verify_plan(plan) → VerificationResult
```

### VerificationVerdict

| Verdict | Meaning |
|---------|---------|
| `APPROVE` | Plan is safe to execute |
| `REJECT` | Plan is blocked |
| `WARN` | Suspicious but allowed |
| `CLARIFY` | Needs user clarification |

### VerificationResult

| Field | Type | Description |
|-------|------|-------------|
| `verdict` | VerificationVerdict | Final decision |
| `risk_level` | RiskLevel | LOW, MEDIUM, HIGH, CRITICAL |
| `reasoning` | string | LLM's explanation |
| `threats` | list[ThreatDetail] | Detected threats |
| `clarification_needed` | string | Question for user (if CLARIFY) |
| `recommendations` | list | Suggested mitigations |
| `analysis_duration_ms` | float | LLM call duration |
| `llm_model` | string | Model used |
| `cached` | bool | Whether result came from cache |

### ThreatDetail

| Field | Type | Description |
|-------|------|-------------|
| `threat_type` | ThreatType | Category of threat |
| `severity` | float | 0.0 to 1.0 |
| `description` | string | What was detected |
| `affected_action_ids` | list | Which actions are suspicious |
| `evidence` | string | Supporting evidence |

### ThreatType (8 built-in)

| Type | Description |
|------|-------------|
| `PROMPT_INJECTION` | Attempts to override system instructions |
| `PRIVILEGE_ESCALATION` | Attempts to gain unauthorized access |
| `DATA_EXFILTRATION` | Attempts to extract sensitive data |
| `SUSPICIOUS_SEQUENCE` | Unusual action combinations |
| `INTENT_MISALIGNMENT` | Actions don't match stated intent |
| `OBFUSCATED_PAYLOAD` | Encoded or hidden malicious content |
| `RESOURCE_ABUSE` | Excessive resource consumption |
| `CUSTOM` | User-defined threat category |

### Threat Category Registry

7 built-in categories, extensible via configuration:

```yaml
intent_verifier:
  custom_threat_categories:
    - id: "my_custom_threat"
      name: "Custom Threat"
      description: "Detects my specific threat pattern"
      threat_type: "custom"
```

Categories can be enabled/disabled individually:
```yaml
intent_verifier:
  disabled_threat_categories:
    - "resource_abuse"
```

### PromptComposer

Dynamically assembles the system prompt for the IntentVerifier LLM:

```
1. Base introduction (security analyst role)
2. Threat category sections (from ThreatCategoryRegistry)
3. Output format specification (JSON)
4. Verdict guidelines (APPROVE/REJECT/WARN/CLARIFY criteria)
5. Critical rules (7 rules: never execute, always analyze, etc.)
6. Custom suffix (user-configurable)
```

The prompt is cached and invalidated when threat categories change.

### LLM Providers

| Provider | Class | Base URL |
|----------|-------|----------|
| Anthropic | `AnthropicLLMClient` | `https://api.anthropic.com/v1` |
| OpenAI | `OpenAILLMClient` | `https://api.openai.com/v1` |
| Ollama | `OllamaLLMClient` | `http://localhost:11434` |
| Custom | User-provided class path | Configurable |
| Null | `NullLLMClient` | N/A (disabled) |

All HTTP providers extend `BaseHTTPLLMClient` which provides:
- Exponential backoff retry on `{429, 500, 502, 503, 504}`
- Configurable timeout
- `httpx.AsyncClient` management

Configuration:
```yaml
intent_verifier:
  enabled: true
  strict: false
  provider: "anthropic"  # or "openai", "ollama", "null", "custom"
  model: "claude-haiku-4-5-20251001"
  api_key: "sk-..."
  timeout_seconds: 30
  cache_size: 1000
  cache_ttl_seconds: 3600
```

---


## Permission System

### Permission Constants (32)

| Category | Permissions |
|----------|------------|
| Filesystem | `FILESYSTEM_READ`, `FILESYSTEM_WRITE`, `FILESYSTEM_DELETE`, `FILESYSTEM_SENSITIVE` |
| Device | `CAMERA`, `MICROPHONE`, `SCREEN_CAPTURE`, `KEYBOARD` |
| Network | `NETWORK_READ`, `NETWORK_SEND`, `NETWORK_EXTERNAL` |
| Data | `DATABASE_READ`, `DATABASE_WRITE`, `DATABASE_DELETE`, `CREDENTIALS`, `PERSONAL_DATA` |
| OS | `PROCESS_EXECUTE`, `PROCESS_KILL`, `PROCESS_READ`, `ENV_READ`, `ENV_WRITE`, `ADMIN` |
| Applications | `BROWSER`, `EMAIL_READ`, `EMAIL_SEND` |
| IoT | `GPIO_READ`, `GPIO_WRITE`, `SENSOR`, `ACTUATOR` |
| Modules | `MODULE_READ`, `MODULE_MANAGE`, `MODULE_INSTALL` |

New OS permissions (added Sprint 12):
- `PROCESS_READ` (`os.process.read`) - risk: low - list running processes
- `ENV_READ` (`os.environment.read`) - risk: low - read environment variables
- `ENV_WRITE` (`os.environment.write`) - risk: medium - set environment variables

Each permission has a default risk level mapping in `PERMISSION_RISK`.

### Per-Action Permission Exposure

The `GET /modules/{id}` endpoint exposes `os_permissions` per action, derived from `@requires_permission` decorators at definition time. All 18 built-in modules have 100% decorator coverage. Example response:

```json
{
  "module_id": "os_exec",
  "actions": {
    "list_processes": {
      "os_permissions": ["os.process.read"]
    },
    "set_env_var": {
      "os_permissions": ["os.environment.write"]
    }
  }
}
```

### PermissionManager

| Method | Description |
|--------|-------------|
| `check(permission, module_id)` | Returns bool |
| `check_or_raise(permission, module_id, action)` | Raises `PermissionNotGrantedError` |
| `check_all(permissions, module_id)` | Check multiple permissions |
| `grant(permission, module_id, scope, reason, risk_level)` | Grant permission |
| `revoke(permission, module_id, scope)` | Revoke permission |
| `revoke_all_for_module(module_id)` | Revoke all for a module |
| `list_grants(module_filter, scope)` | List grants |
| `get_risk_level(permission)` | Get risk level |

### PermissionStore

SQLite-backed persistence with two scopes:

| Scope | Behavior |
|-------|----------|
| `SESSION` | Cleared on daemon restart |
| `PERMANENT` | Persists across restarts |

Lazy expiry cleanup: expired grants are removed during read operations, not on a timer.

### PermissionGuard

Single enforcement point. Checks in order:
1. Plan action count vs profile limit
2. Profile allows `module.action` pattern
3. Explicit approval requirement
4. Sandbox path enforcement

**Sandbox enforcement**: Uses `os.path.realpath()` to resolve symlinks before checking if path is within allowed directories. Prevents symlink-based escapes.

**Path parameter keys monitored**: `path`, `source`, `destination`, `output_path`, `image_path`, `file_path`, `theme_path`, `screenshot_path`, `database`, `archive_path`.

---


## Security Profiles

### Profile Comparison

| Feature | readonly | local_worker | power_user | unrestricted |
|---------|----------|-------------|------------|-------------|
| Max actions | 20 | 50 | 200 | 500 |
| File read | Yes | Yes | Yes | Yes |
| File write | No | Yes | Yes | Yes |
| File delete | No | No | Yes | Yes |
| Commands | No | Yes | Yes | Yes |
| Kill process | No | No | Yes | Yes |
| Browser | No | No | Yes | Yes |
| GUI control | No | No | Yes | Yes |
| Send email | No | No | Yes | Yes |
| Env templates | No | Yes | Yes | Yes |
| Bypass approval | No | No | No | Yes |

Pattern matching uses `fnmatch` wildcards: `filesystem.*`, `*.read_file`, `*.*`.

Denied patterns take precedence over allowed patterns (veto semantics).

---


## Audit System

### AuditEvent (24 events)

| Category | Events |
|----------|--------|
| Plan | `PLAN_SUBMITTED`, `PLAN_STARTED`, `PLAN_COMPLETED`, `PLAN_FAILED`, `PLAN_CANCELLED` |
| Action | `ACTION_STARTED`, `ACTION_COMPLETED`, `ACTION_FAILED`, `ACTION_SKIPPED` |
| Approval | `APPROVAL_REQUESTED`, `APPROVAL_GRANTED`, `APPROVAL_REJECTED`, `PERMISSION_DENIED` |
| Security | `SECURITY_VIOLATION` |
| Permission | `PERMISSION_GRANTED`, `PERMISSION_REVOKED`, `PERMISSION_CHECK_FAILED`, `RATE_LIMIT_EXCEEDED`, `SENSITIVE_ACTION_INVOKED` |
| Intent | `INTENT_VERIFIED`, `INTENT_REJECTED` |
| Scanner | `INPUT_SCAN_PASSED`, `INPUT_SCAN_REJECTED`, `INPUT_SCAN_WARNED` |

### AuditLogger

Delegates to EventBus. Each audit event is routed to the appropriate topic:
- Plan events → `llmos.plans`
- Action events → `llmos.actions`
- Security events → `llmos.security`
- Permission events → `llmos.permissions`

### ActionRateLimiter

In-memory sliding-window rate limiter:
- Per-module, per-action tracking
- Configurable calls_per_minute and calls_per_hour
- Window pruning every 3600 seconds
- Raises `RateLimitExceededError` when exceeded

---


## Output Sanitizer

Scrubs all module output before it reaches the LLM agent:

| Protection | Description |
|------------|-------------|
| Injection patterns | 8 regex patterns detect prompt injection in output |
| String truncation | Max 50,000 characters per string |
| Depth limiting | Max 10 levels of nesting |
| List truncation | Max 1,000 items per list |
| Binary exclusion | Binary keys (`screenshot_b64`, etc.) are cleaned separately |

**Injection patterns detected**:
- System prompt override attempts
- Role switching (`[SYSTEM]`, `[ASSISTANT]`)
- Instruction injection (`ignore previous`, `new instructions`)
- Delimiter-based injection
- XML/HTML tag injection
- Markdown heading injection

---


## Identity & Authorization

### Design Philosophy

The authorization system is built around five principles that, taken together, make it safe to expose the daemon to untrusted callers without breaking existing single-user deployments.

---


**1. Zero-config by default - never break the happy path**

When `identity.enabled=false` (the default), every incoming request receives a synthetic `IdentityContext(app_id="default", role=ADMIN)` and all checks are no-ops. A developer running the daemon locally does not need to create applications, issue API keys, or think about RBAC. The entire identity layer is transparent until explicitly enabled.

---


**2. Ceilings, not floors - each layer can only restrict**

The authorization model is a *downward restriction chain*:

```
Daemon capabilities (what modules are installed)
    ↓  Application ceiling  (allowed_modules, allowed_actions, quotas)
    ↓  Session ceiling      (allowed_modules subset, permission grants/denials)
    ↓  RBAC role            (what operations the caller can perform)
```

An application defines the maximum set of capabilities its callers can use. A session can only further restrict that set - it can never grant access to a module the application doesn't allow. This means a compromised session cannot escalate: even if an attacker obtains a session token, they are bounded by the application's policy.

This is the *most restrictive wins* rule: if the application allows `["filesystem", "os_exec"]` but the session only allows `["filesystem"]`, the action is denied.

---


**3. Separation of authentication and authorization**

`IdentityResolver` answers *"who are you?"* - it validates API keys and extracts identity from headers. It produces an `IdentityContext` (a data object with no behaviour).

`AuthorizationGuard` answers *"what can you do?"* - it takes an `IdentityContext` and a plan, and decides whether execution is allowed. It never touches keys or tokens.

This separation makes both components independently testable and replaceable. The guard can be used without API keys (identity enabled, keys not required), and the resolver can be used without the guard (identity enabled for logging only).

---


**4. Temporal scoping - sessions are ephemeral by design**

Long-lived credentials (API keys) grant persistent access. Sessions are designed to be *short-lived and scoped*: they carry an expiry (`expires_at`), an idle timeout (`idle_timeout_seconds`), and can be revoked at any time via `DELETE /applications/{id}/sessions/{sid}`.

The intent is that an agent SDK creates a session at the start of a task and destroys it when the task is complete (this is what `ReactivePlanLoop(session_config=...)` does automatically). If the agent crashes, the session expires naturally - no cleanup required.

This limits the blast radius of a compromised agent: the session window is bounded, and the session's `allowed_modules` and `permission_denials` further constrain what damage can be done within that window.

---


**5. Least privilege as the default direction**

New applications and sessions start with maximal restrictions and are explicitly opened up, not the reverse:

- `allowed_modules=[]` means *all modules allowed* (open by default for an established app)
- But `allowed_actions={"os_exec": []}` means *all os_exec actions allowed* while `allowed_actions={"os_exec": ["run_command"]}` restricts to only `run_command`
- Sessions start with no `permission_grants` and can only gain them if explicitly listed

RBAC follows the same direction: the minimum role to submit a plan is `AGENT` (lowest), but creating an application requires `ADMIN` (highest). Callers must be explicitly promoted, not demoted.

---


### Identity Layers

```text
Request arrives with headers:
  Authorization: Bearer llmos_...   → API key validation
  X-LLMOS-App: myapp                → Application scoping
  X-LLMOS-Agent: agent-id           → Agent binding
  X-LLMOS-Session: sess-uuid        → Session restrictions
        |
        v
IdentityResolver.resolve()
        |
        v
IdentityContext { app_id, agent_id, session_id, role }
        |
        v
AuthorizationGuard.check_plan_submission()
  1. Application exists and is enabled
  2. RBAC role ≥ AGENT
  3. max_actions_per_plan quota
  4. max_concurrent_plans quota
  5. Session binding + expiry
  6. Pre-flight: all actions in allowed_modules + allowed_actions + session.allowed_modules
        |
        v
PlanExecutor (per-action): check_action_allowed(app, module, action, session=session)
```

### RBAC Role Hierarchy

Roles are ordered most → least privileged. Each check requires the caller to have **at least** the specified role.

| Role | Level | Typical use |
|------|-------|-------------|
| `ADMIN` | 0 (highest) | Full daemon administration |
| `APP_ADMIN` | 1 | Manage one application and its agents/keys |
| `OPERATOR` | 2 | Submit plans, create sessions |
| `VIEWER` | 3 | Read-only (list plans, get status) |
| `AGENT` | 4 (lowest) | Programmatic plan submission only |

**Scoping**: `APP_ADMIN` can only manage their own application (`identity.app_id == target_app_id`). `ADMIN` bypasses this restriction.

### Application Model

An `Application` defines the permission ceiling for all plans submitted under it:

| Field | Type | Description |
|-------|------|-------------|
| `app_id` | str | Unique identifier |
| `enabled` | bool | Plans rejected when false |
| `allowed_modules` | list[str] | Empty = all modules allowed |
| `allowed_actions` | dict[str, list[str]] | Per-module action whitelist. Empty list = all actions for that module |
| `max_concurrent_plans` | int | In-flight plan limit (default 10) |
| `max_actions_per_plan` | int | Per-plan action count limit (default 100) |

### Session Model

A `Session` can **only restrict** - never expand - the application's grants. The most restrictive level wins.

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | str | UUID |
| `app_id` | str | Must match caller's app |
| `expires_at` | float or None | Absolute UTC expiry timestamp |
| `idle_timeout_seconds` | int or None | Seconds of inactivity before expiry |
| `allowed_modules` | list[str] | Subset of app's allowed_modules (empty = app's list) |
| `permission_grants` | list[str] | Temporary OS permission grants for this session |
| `permission_denials` | list[str] | OS permissions explicitly denied for this session |

### Authorization Enforcement Order

For each action in a plan:

1. **App module whitelist** - `app.allowed_modules` non-empty → module must be listed
2. **App action whitelist** - `app.allowed_actions[module]` non-empty → action must be listed
3. **Session module whitelist** - `session.allowed_modules` non-empty → module must be listed

Errors raised: `ApplicationNotFoundError`, `AuthorizationError`, `QuotaExceededError`.

### API Endpoints (identity.enabled=true)

| Method | Path | Required role | Description |
|--------|------|--------------|-------------|
| GET | `/applications` | VIEWER | List all (ADMIN) or own app (APP_ADMIN) |
| POST | `/applications` | ADMIN | Create application |
| PATCH/DELETE | `/applications/{id}` | APP_ADMIN | Update / delete |
| GET/POST/DELETE | `/applications/{id}/agents` | APP_ADMIN | Manage agents |
| POST | `/applications/{id}/agents/{aid}/api-keys` | APP_ADMIN | Generate API key |
| GET | `/applications/{id}/sessions` | VIEWER | List sessions |
| POST | `/applications/{id}/sessions` | OPERATOR | Create session |
| GET | `/applications/{id}/sessions/{sid}` | VIEWER | Get session |
| DELETE | `/applications/{id}/sessions/{sid}` | OPERATOR | Revoke session |

### Configuration

```yaml
identity:
  enabled: false          # true to activate multi-tenant enforcement
  require_api_keys: false # true to mandate Authorization: Bearer llmos_...
```

When `enabled=false` (default), every request gets `IdentityContext(app_id="default", role=ADMIN)` - identical to pre-distributed behaviour.
