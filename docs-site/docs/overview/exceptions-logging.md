---

id: exceptions-logging
title: Exceptions & Logging
sidebar_position: 9
format: md
---



# Exceptions & Logging

---


## Exception Hierarchy

All exceptions inherit from `LLMOSError`, which stores both a human-readable message and optional contextual metadata.

```
LLMOSError(message, context=None)
    |
    +--→ ProtocolError
    |       +--→ IMLParseError(message, raw_payload=None)
    |       +--→ IMLValidationError(message, errors=None)
    |       +--→ TemplateResolutionError(template, reason)
    |
    +--→ SecurityError
    |       +--→ PermissionDeniedError(action, module, profile)
    |       +--→ ApprovalRequiredError(action_id, plan_id)
    |       +--→ PermissionNotGrantedError(permission, module_id, action, risk_level)
    |       +--→ RateLimitExceededError(action_key, limit, window)
    |       +--→ SanitizationError
    |       +--→ IntentVerificationError(plan_id, reason)
    |       +--→ SuspiciousIntentError(plan_id, reasoning, threats, risk_level)
    |       +--→ InputScanRejectedError(plan_id, verdict, risk_score, scanners)
    |
    +--→ OrchestrationError
    |       +--→ DAGCycleError(cycle)
    |       +--→ DependencyError(action_id, dep_id, reason)
    |       +--→ ExecutionTimeoutError(action_id, timeout_seconds)
    |
    +--→ ModuleError
    |       +--→ ModuleNotFoundError(module_id)
    |       +--→ ActionNotFoundError(module_id, action)
    |       +--→ ModuleLoadError(module_id, reason)
    |       +--→ ActionExecutionError(module_id, action, cause)
    |       +--→ ModuleLifecycleError(module_id, current_state, target_state)
    |       +--→ ServiceNotFoundError(service)
    |       +--→ ActionDisabledError(module_id, action, reason)
    |       +--→ PolicyViolationError(module_id, violation)
    |       +--→ WorkerError
    |               +--→ WorkerStartError(module_id, reason)
    |               +--→ WorkerCommunicationError(module_id, reason)
    |               +--→ WorkerCrashedError(module_id, exit_code)
    |               +--→ VenvCreationError(module_id, reason)
    |
    +--→ PerceptionError
    |       +--→ ScreenCaptureError
    |       +--→ OCRError
    |
    +--→ MemoryError
            +--→ StateStoreError
            +--→ VectorStoreError
```

### Exception Categories

#### Protocol (Layer 1)

| Exception | Arguments | When |
|-----------|-----------|------|
| `IMLParseError` | message, raw_payload | Malformed JSON or IML structure |
| `IMLValidationError` | message, errors | Pydantic validation fails |
| `TemplateResolutionError` | template, reason | `{{result.X.Y}}` unresolvable |

#### Security (Layer 2)

| Exception | Arguments | When |
|-----------|-----------|------|
| `PermissionDeniedError` | action, module, profile | Profile disallows module.action |
| `ApprovalRequiredError` | action_id, plan_id | Action needs user approval |
| `PermissionNotGrantedError` | permission, module_id, action, risk_level | OS permission not granted |
| `RateLimitExceededError` | action_key, limit, window | Rate limit exceeded |
| `SanitizationError` | (message) | Output contains injection patterns |
| `IntentVerificationError` | plan_id, reason | LLM verification call failed |
| `SuspiciousIntentError` | plan_id, reasoning, threats, risk_level | Security threat detected |
| `InputScanRejectedError` | plan_id, verdict, risk_score, scanners | Scanner pipeline rejected |

#### Orchestration (Layer 3)

| Exception | Arguments | When |
|-----------|-----------|------|
| `DAGCycleError` | cycle | Circular dependency in action graph |
| `DependencyError` | action_id, dep_id, reason | Dependency not satisfied |
| `ExecutionTimeoutError` | action_id, timeout_seconds | Action exceeded timeout |

#### Module (Layer 4)

| Exception | Arguments | When |
|-----------|-----------|------|
| `ModuleNotFoundError` | module_id | Module not registered |
| `ActionNotFoundError` | module_id, action | Module lacks action |
| `ModuleLoadError` | module_id, reason | Module init failed |
| `ActionExecutionError` | module_id, action, cause | Unexpected error in action |
| `ModuleLifecycleError` | module_id, current_state, target_state | Invalid lifecycle transition |
| `ServiceNotFoundError` | service | ServiceBus lookup failed |
| `ActionDisabledError` | module_id, action, reason | Action administratively disabled |
| `PolicyViolationError` | module_id, violation | Module policy constraint violated |

#### Worker Isolation

| Exception | Arguments | When |
|-----------|-----------|------|
| `WorkerStartError` | module_id, reason | Worker subprocess failed to start |
| `WorkerCommunicationError` | module_id, reason | JSON-RPC pipe broken |
| `WorkerCrashedError` | module_id, exit_code | Worker terminated unexpectedly |
| `VenvCreationError` | module_id, reason | Venv creation or validation failed |

#### Perception (Layer 0)

| Exception | When |
|-----------|------|
| `ScreenCaptureError` | Screenshot capture failed (no display, etc.) |
| `OCRError` | OCR extraction failed (tesseract missing, etc.) |

#### Memory (Layer 6)

| Exception | When |
|-----------|------|
| `StateStoreError` | SQLite key-value store operation failed |
| `VectorStoreError` | ChromaDB vector store operation failed |

---


## Structured Logging

LLMOS Bridge uses `structlog` for structured, context-aware logging throughout the daemon.

### Configuration

```python
from llmos_bridge.logging import configure_logging, get_logger

configure_logging(
    level="info",           # debug, info, warning, error, critical
    format="console",       # "console" (human-readable) or "json" (structured)
    log_file="/var/log/llmos.log"  # Optional additional log file
)

log = get_logger(__name__)
log.info("plan_submitted", plan_id="p-123", action_count=5)
```

### Output Formats

**Console** (default, colors if TTY):
```
2026-03-01 10:30:45 [info     ] plan_submitted    plan_id=p-123 action_count=5
```

**JSON** (structured, for log aggregation):
```json
{"event": "plan_submitted", "plan_id": "p-123", "action_count": 5, "level": "info", "timestamp": "2026-03-01T10:30:45Z"}
```

### Context Variables

Three `ContextVar` instances are automatically injected into every log record:

| Variable | Description |
|----------|-------------|
| `plan_id` | Currently executing plan |
| `action_id` | Currently executing action |
| `session_id` | Current session identifier |

#### Binding Context

```python
from llmos_bridge.logging import bind_plan_context, clear_plan_context

# Bind context for current async task
bind_plan_context(plan_id="p-123", action_id="a-001", session_id="sess-abc")

# All subsequent logs include these fields automatically
log.info("action_started")  # → includes plan_id, action_id, session_id

# Clear when done
clear_plan_context()
```

### Processor Pipeline

```
Log event
    |
    v
TimeStamper (ISO 8601)
    |
    v
_inject_context_vars() ── adds plan_id, action_id, session_id from ContextVars
    |
    v
StackInfoRenderer
    |
    v
_drop_color_message() ── removes uvicorn duplicate field
    |
    v
ConsoleRenderer or JSONRenderer
```

### Silenced Loggers

These noisy loggers are set to WARNING level:
- `uvicorn.access`
- `httpx`
- `asyncio`

### Configuration

```yaml
logging:
  level: info              # Log level
  format: console          # console or json
  file: null               # Additional log file
  audit_file: ~/.llmos/audit.log  # Audit event log
```

---


## Module Helper Systems

### Platform Detection

Detects the current platform and enforces module compatibility.

#### PlatformInfo

| Field | Type | Description |
|-------|------|-------------|
| `os_type` | Platform | LINUX, WINDOWS, MACOS, RASPBERRY_PI |
| `os_name` | str | e.g. "Linux", "Darwin" |
| `os_version` | str | e.g. "6.1.0-debian" |
| `python_version` | str | e.g. "3.11.6" |
| `is_raspberry_pi` | bool | Raspberry Pi detection |
| `architecture` | str | e.g. "x86_64", "aarch64" |

**Detection**: `PlatformInfo.detect()` - cached singleton, checks `/proc/cpuinfo` and `/proc/device-tree/model` for Raspberry Pi.

#### PlatformGuard

| Method | Description |
|--------|-------------|
| `is_compatible(module)` | Check SUPPORTED_PLATFORMS against current OS |
| `assert_compatible(module)` | Raise ModuleLoadError if incompatible |
| `filter_compatible(modules)` | Filter list to compatible modules only |

#### Platform Compatibility Matrix

Built-in mapping from module_id to supported platforms:

| Module | Platforms |
|--------|-----------|
| filesystem, os_exec, api_http | ALL |
| excel, word, browser, gui | LINUX, WINDOWS, MACOS |
| iot | RASPBERRY_PI, LINUX |
| database | LINUX, WINDOWS, MACOS |

---


### Module Lifecycle Manager

Full state machine for module lifecycle with batch operations, action toggles, and event auto-subscription.

#### State Machine

```
LOADED ──→ STARTING ──→ ACTIVE ←──→ PAUSED
                          │
                          └──→ STOPPING ──→ DISABLED
                                   ↓
                                 ERROR
```

#### Methods

| Method | Description |
|--------|-------------|
| `get_state(module_id)` | Returns current state (default LOADED) |
| `async start_module(module_id)` | Transition to ACTIVE |
| `async stop_module(module_id)` | Transition to DISABLED |
| `async pause_module(module_id)` | Transition to PAUSED |
| `async resume_module(module_id)` | Transition back to ACTIVE |
| `async restart_module(module_id)` | Stop then start |
| `async start_all()` | Start all registered modules |
| `async stop_all()` | Stop all in reverse order |
| `disable_action(module_id, action, reason)` | Disable specific action |
| `enable_action(module_id, action)` | Re-enable action |
| `async install_module(module_id)` | Call on_install() hook |
| `async upgrade_module(module_id, old_version)` | Call on_update() hook |
| `async update_config(module_id, config)` | Validate and apply config |
| `get_full_report()` | State, type, disabled_actions per module |

**Auto-subscription**: When a module is started, the lifecycle manager automatically subscribes it to declared EventBus topics. On stop, it unsubscribes.

**State persistence**: Module state snapshots are saved to `ModuleStateStore` on stop and restored on start.

---


### Service Bus

Inter-module communication channel. Modules register services that other modules can call.

#### ServiceRegistration

| Field | Type | Description |
|-------|------|-------------|
| `name` | str | Service name |
| `module_id` | str | Provider module ID |
| `provider` | BaseModule | Module instance |
| `methods` | list[str] | Available action methods |
| `description` | str | Description |

#### ServiceBus Methods

| Method | Description |
|--------|-------------|
| `register_service(name, provider, methods, description)` | Register service |
| `unregister_service(name)` | Remove service |
| `async call(service, method, params)` | Call service method |
| `is_available(service)` | Check registration |
| `list_services()` | List all services |
| `get_provider(service)` | Get provider module |

**Routing**: `call("vision", "parse_screen", params)` routes through the provider module's `execute()` method, preserving security decorators.

---


### Module Signing (Ed25519)

Cryptographic module signing for trust verification.

#### KeyPair

| Field | Type | Description |
|-------|------|-------------|
| `private_key_bytes` | bytes | 32-byte Ed25519 seed |
| `public_key_bytes` | bytes | 32-byte public key |
| `fingerprint` | str | SHA-256 hex of public key |

#### ModuleSigner

| Method | Description |
|--------|-------------|
| `generate_key_pair()` | Generate new Ed25519 pair |
| `save_key_pair(key_pair, path)` | Write .key and .pub files |
| `load_private_key(path)` | Read private key bytes |
| `compute_module_hash(module_dir)` | SHA-256 of module files |
| `sign_content(content_hash)` | Sign hash, return ModuleSignature |
| `sign_module(module_dir)` | Compute hash and sign |

#### SignatureVerifier

| Method | Description |
|--------|-------------|
| `add_trusted_key(fingerprint, public_key_bytes)` | Add trusted key |
| `remove_trusted_key(fingerprint)` | Remove trusted key |
| `load_trust_store(path)` | Load *.pub files from directory |
| `verify(signature, content_hash)` | Full verification |

**Verification checks**: fingerprint in trust store, hash match, Ed25519 signature validity.

**Files hashed**: `*.py`, `pyproject.toml`, `llmos-module.toml` (sorted, stable).

---


### Composite Module

Meta-modules that compose pipelines from existing module actions.

#### PipelineStep

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `module` | str | required | Target module_id |
| `action` | str | required | Target action |
| `param_map` | dict | `{}` | Parameter mapping |
| `condition` | str | `""` | Skip condition |
| `on_error` | str | `"abort"` | `abort`, `continue`, `skip` |

**Parameter mapping syntax**:
- `"literal_value"` - Static value
- `"{{prev.result}}"` - Previous step's full result
- `"{{prev.result.field}}"` - Previous step's field
- `"{{input.field}}"` - Original action params

#### CompositeModule

Extends BaseModule. Each registered pipeline becomes an action.

```python
composite = CompositeModule.build(
    module_id="my_pipeline",
    version="1.0.0",
    description="Custom workflow",
    pipelines={
        "full_backup": [
            PipelineStep(module="filesystem", action="list_directory", param_map={"path": "{{input.source}}"}),
            PipelineStep(module="filesystem", action="copy_file", param_map={"source": "{{prev.result.path}}", "destination": "{{input.dest}}"}),
        ]
    }
)
```

---


### Virtual Module Factory

Programmatic module creation from callables or tool schemas.

#### VirtualAction

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `handler` | Callable | required | async (params) -> Any |
| `description` | str | `""` | Description |
| `params` | list[ParamSpec] | `[]` | Parameter definitions |
| `permission_required` | str | `"local_worker"` | Minimum profile |
| `side_effects` | list[str] | `[]` | Side effect categories |

#### Factory Methods

| Method | Description |
|--------|-------------|
| `create(module_id, version, description, actions)` | Full module definition |
| `from_callable(module_id, handler, action_name)` | Single-action module from function |
| `from_tool_schema(module_id, tool_schemas, handler_map)` | From LangChain tool schemas |

---


### Module State Store

SQLite-backed state persistence for modules.

| Method | Description |
|--------|-------------|
| `async init()` | Create table |
| `async save(module_id, state)` | Upsert state JSON |
| `async load(module_id)` | Load saved state or None |
| `async delete(module_id)` | Remove state |
| `async list_all()` | Module IDs with saved state |

Used by `ModuleLifecycleManager` to save/restore module state across restarts.

---


### Policy Enforcer

Runtime enforcement of module execution policies (cooldown, concurrency).

| Method | Description |
|--------|-------------|
| `load_policy(module_id)` | Load and cache module policy |
| `async check_and_acquire(module_id, action)` | Enforce cooldown + concurrency |
| `release(module_id)` | Release concurrency slot |
| `reset(module_id)` | Clear cached policy |
| `status()` | Active calls, limits, cooldown per module |

**Cooldown**: Raises `PolicyViolationError` if elapsed time < `cooldown_seconds`.

**Concurrency**: Blocks (up to 30s timeout) if `max_parallel_calls` reached. Uses `asyncio.Semaphore`.

---


### Resource Negotiator

Pre-execution resource estimation and budget checking.

| Method | Description |
|--------|-------------|
| `async negotiate(module_id, action, params)` | Check resource budget |
| `acquire(module_id, estimate)` | Track resource acquisition |
| `release(module_id, estimate)` | Release resources |
| `status()` | Memory (MB) and duration (s) per module |

**Negotiation flow**:
```
1. module.estimate_cost(action, params) → ResourceEstimate
2. Check against manifest.resource_limits (memory, time)
3. Return: granted / deferred (retry_after) / rejected (reason)
```

---


### Module Configuration System

Typed configuration with dashboard UI metadata.

#### ConfigField()

Wrapper around Pydantic `Field` with UI annotations:

| Parameter | Type | Description |
|-----------|------|-------------|
| `default` | Any | Default value |
| `description` | str | Field description |
| `label` | str | UI display label |
| `category` | str | UI category grouping |
| `ui_widget` | str | Widget type hint |
| `ui_order` | int | Display order |
| `restart_required` | bool | Requires module restart |
| `secret` | bool | Mask in UI |
| `ge`, `le` | float | Numeric constraints |
| `min_length`, `max_length` | int | String constraints |

#### ModuleConfigBase

Base class for module configuration models. Stores UI metadata in `json_schema_extra`.

```python
class MyModuleConfig(ModuleConfigBase):
    api_key: str = ConfigField("", secret=True, label="API Key", category="auth")
    timeout: int = ConfigField(30, ge=1, le=300, label="Timeout", restart_required=True)
```

#### @configurable Decorator

```python
@configurable(MyModuleConfig)
class MyModule(BaseModule):
    ...
    # CONFIG_MODEL is set to MyModuleConfig
```

Module configuration schema is exposed via `get_manifest()` and `to_config_schema()`.
