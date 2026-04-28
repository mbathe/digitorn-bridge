---

id: module-system
title: Module System
sidebar_position: 1
format: md
---



# Module System Fundamentals

Modules are the executable units of LLMOS Bridge. Every action the daemon can perform - reading a file, making an HTTP request, clicking a GUI element, querying a database - is implemented by a module. This document covers the complete module system: what a module is, how it works, and how to build one.

---


## What is a Module?

A module is a Python class that:

1. **Subclasses `BaseModule`** - inheriting the dispatch mechanism, lifecycle hooks, and security integration
2. **Declares its identity** - `MODULE_ID`, `VERSION`, `SUPPORTED_PLATFORMS`
3. **Exposes actions** - via `_action_<name>` methods that accept `params: dict` and return structured results
4. **Publishes a manifest** - a `ModuleManifest` describing all actions, parameters, permissions, and capabilities
5. **Registers with the daemon** - through `ModuleRegistry` at startup

A module is NOT a plugin in the traditional sense. It does not hook into arbitrary extension points. It provides a bounded set of typed actions that the orchestration engine dispatches. The boundary between a module and the system is the `execute()` method.

---


## Module Identity

Every module declares three class-level attributes:

```python
class FilesystemModule(BaseModule):
    MODULE_ID = "filesystem"       # Unique identifier (snake_case)
    VERSION = "1.0.0"             # Semantic version
    SUPPORTED_PLATFORMS = [Platform.ALL]  # Platform compatibility
```

### MODULE_ID

The unique identifier for the module. Used in:
- IML plans: `"module": "filesystem"`
- API paths: `GET /modules/filesystem`
- Configuration: `module.enabled = ["filesystem", ...]`
- Permission scoping: `filesystem.read_file`

**Naming convention**: lowercase `snake_case`. Examples: `filesystem`, `api_http`, `computer_control`, `db_gateway`.

### VERSION

Semantic version string (MAJOR.MINOR.PATCH). Used for:
- Module requirements in IML plans: `"module_requirements": {"filesystem": ">=1.0.0"}`
- Compatibility checking (PEP-440 via `ModuleVersionChecker`)
- Hub versioning and upgrades

### SUPPORTED_PLATFORMS

List of `Platform` enum values declaring where the module can run:

| Platform | Description |
|----------|-------------|
| `Platform.ALL` | Works everywhere |
| `Platform.LINUX` | Linux only |
| `Platform.WINDOWS` | Windows only |
| `Platform.MACOS` | macOS only |
| `Platform.RASPBERRY_PI` | Raspberry Pi (ARM + GPIO) |

The `PlatformGuard` checks these at registration time. If the current platform does not match, the module is excluded from the registry.

### Additional Class Attributes

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `MODULE_TYPE` | string | `"user"` | `"system"` for built-in, `"user"` for community |
| `CONFIG_MODEL` | type | `None` | Pydantic model for module-specific configuration |

---


## Action Dispatch

### The Naming Convention

The core dispatch mechanism uses naming convention: when the executor calls `module.execute("read_file", params)`, the `BaseModule.execute()` method routes to `module._action_read_file(params)`.

```python
class FilesystemModule(BaseModule):
    async def _action_read_file(self, params: dict) -> dict:
        path = params["path"]
        encoding = params.get("encoding", "utf-8")
        content = await asyncio.to_thread(Path(path).read_text, encoding)
        return {"content": content, "path": path}
```

**Rules**:
- Method name: `_action_` prefix + action name in snake_case
- Signature: `async def _action_<name>(self, params: dict) -> Any`
- Must be `async` (use `asyncio.to_thread()` for blocking I/O)
- Return value is stored in `execution_results` under the action ID

### Dynamic Action Registration

For actions not known at class definition time, modules can register handlers dynamically:

```python
def on_start(self):
    self.register_action(
        name="custom_operation",
        handler=self._handle_custom,
        spec=ActionSpec(name="custom_operation", description="Dynamic action"),
    )
```

Dynamic actions are checked first, before `_action_` method lookup.

### The execute() Method

`BaseModule.execute()` is **not abstract** - it provides the standard dispatch logic:

```
execute(action, params, context)
    |
    +--→ Look up handler (dynamic registry first, then _action_ method)
    |
    +--→ Security decorator enforcement (if SecurityManager injected)
    |
    +--→ Call handler(params)
    |
    +--→ Catch exceptions → ActionExecutionError
    |
    +--→ Return result
```

Modules can override `execute()` for custom dispatch logic, but this is rarely needed.

---


## Module Manifest

Every module must implement `get_manifest()` returning a `ModuleManifest`:

```python
def get_manifest(self) -> ModuleManifest:
    return ModuleManifest(
        module_id=self.MODULE_ID,
        version=self.VERSION,
        description="File and directory operations",
        actions=[
            ActionSpec(
                name="read_file",
                description="Read file content",
                params=[
                    ParamSpec(name="path", type="string", description="File path", required=True),
                    ParamSpec(name="encoding", type="string", description="Encoding", default="utf-8"),
                ],
                returns="object",
                returns_description="File content and metadata",
                permission_required="local_worker",
                permissions=["filesystem.read"],
                tags=["io", "read"],
            ),
            # ... more actions
        ],
        declared_permissions=["filesystem.read", "filesystem.write", "filesystem.delete"],
        tags=["core", "io"],
    )
```

### ModuleManifest Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `module_id` | string | Yes | Must match `MODULE_ID` |
| `version` | string | Yes | Must match `VERSION` |
| `description` | string | Yes | Human-readable module description |
| `author` | string | No | Module author |
| `homepage` | string | No | Documentation URL |
| `platforms` | list | No | Supported platforms (default: `["all"]`) |
| `actions` | list | Yes | List of `ActionSpec` |
| `dependencies` | list | No | pip dependencies (e.g., `["PIL>=9.0"]`) |
| `tags` | list | No | Searchable tags |
| `declared_permissions` | list | No | OS-level permissions the module needs |

**Module Spec v2 fields** (inter-module communication):

| Field | Type | Description |
|-------|------|-------------|
| `module_type` | string | `"system"` or `"user"` |
| `provides_services` | list | Services this module provides |
| `consumes_services` | list | Services this module consumes |
| `emits_events` | list | Event topics this module emits |
| `subscribes_events` | list | Event topics this module subscribes to |
| `config_schema` | dict | JSON Schema for module configuration |

**Module Spec v3 fields** (hub, isolation, signing):

| Field | Type | Description |
|-------|------|-------------|
| `resource_limits` | ResourceLimits | CPU, memory, execution time budgets |
| `sandbox_level` | string | `"none"`, `"basic"`, `"strict"`, `"isolated"` |
| `license` | string | SPDX license identifier |
| `optional_dependencies` | list | Optional pip dependencies |
| `module_dependencies` | dict | Required module versions (PEP-440) |
| `signing` | ModuleSignature | Ed25519 cryptographic signature |
| `declared_capabilities` | list | Structured permission with scope and constraints |

### ActionSpec

Each action in the manifest is described by an `ActionSpec`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | - | Action name (matches `_action_<name>`) |
| `description` | string | - | Human-readable description |
| `params` | list | `[]` | Parameter specifications |
| `returns` | string | `"object"` | Return type |
| `returns_description` | string | `""` | Description of return value |
| `permission_required` | string | `"local_worker"` | Minimum profile required |
| `permissions` | list | `[]` | OS-level permissions required |
| `risk_level` | string | `"low"` | Risk classification |
| `irreversible` | bool | `false` | Whether action can be undone |
| `streams_progress` | bool | `false` | Whether action supports progress streaming |
| `tags` | list | `[]` | Searchable tags |

### ParamSpec

Each action parameter is described by a `ParamSpec`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | - | Parameter name |
| `type` | string | - | JSON Schema type (`string`, `integer`, `number`, `boolean`, `object`, `array`) |
| `description` | string | - | Human-readable description |
| `required` | bool | `true` | Whether parameter is required |
| `default` | any | `None` | Default value |
| `enum` | list | `None` | Allowed values |
| `example` | any | `None` | Example value |

---


## Module Lifecycle

Modules progress through a defined lifecycle managed by the `ModuleLifecycleManager`:

```
                    ┌─── on_start() ────→ ACTIVE
                    |                       |
REGISTERED ─────────┤                    on_pause()
                    |                       |
                    └─── (load error) ──→ FAILED
                                           v
                                        PAUSED
                                           |
                                        on_resume()
                                           |
                                           v
                                        ACTIVE
                                           |
                                        on_stop()
                                           |
                                           v
                                        DISABLED
```

### Lifecycle Hooks

All hooks are async and default to no-op. Override only what you need:

| Hook | When Called | Purpose |
|------|------------|---------|
| `on_start()` | Module transitions to ACTIVE | Initialize connections, load resources |
| `on_stop()` | Module being disabled | Clean up connections, release resources |
| `on_pause()` | Module temporarily suspended | Pause background tasks |
| `on_resume()` | Module resuming from pause | Resume background tasks |
| `on_config_update(config)` | Configuration changed at runtime | Hot-reload configuration |
| `health_check()` | Periodic health probe | Return connectivity/status dict |
| `metrics()` | Metrics collection | Return operational metrics dict |
| `state_snapshot()` | State persistence | Return serializable state dict |
| `on_event(topic, event)` | Event subscription (v3) | Handle subscribed events |
| `restore_state(state)` | Crash recovery (v3) | Restore from snapshot |
| `on_install()` | First installation (v3) | One-time setup |
| `on_update(old_version)` | Version upgrade (v3) | Migration logic |
| `on_resource_pressure(level)` | System under pressure (v3) | Shed load, release caches |

### Dependency Checking

Override `_check_dependencies()` to verify runtime prerequisites:

```python
def _check_dependencies(self) -> None:
    try:
        import openpyxl
    except ImportError:
        raise ModuleLoadError(
            "excel",
            "openpyxl >= 3.1 is required. Install with: pip install openpyxl"
        )
```

This runs in the constructor. If it raises, the module is not registered.

---


## Security Integration

### Security Manager Injection

When `security_advanced.enable_decorators = true`, the daemon injects a `SecurityManager` into each module:

```python
module.set_security(security_manager)
```

This enables runtime enforcement of security decorators on `_action_*` methods.

### Security Metadata Introspection

`BaseModule._collect_security_metadata()` scans all `_action_*` methods for decorator metadata:

```python
meta = module._collect_security_metadata()
# {
#     "read_file": {"required_permissions": ["filesystem.read"]},
#     "delete_file": {
#         "required_permissions": ["filesystem.delete"],
#         "risk_level": "HIGH",
#         "irreversible": True,
#         "audit_level": "detailed",
#     },
# }
```

This metadata enriches the manifest and is exposed through the API for dashboard and LLM agent visibility.

### Decorator Application

Security decorators are applied directly to `_action_*` methods:

```python
from llmos_bridge.security.decorators import (
    requires_permission,
    sensitive_action,
    rate_limited,
    audit_trail,
    data_classification,
)
from llmos_bridge.security.models import RiskLevel, DataClassification
from llmos_bridge.security.permissions import Permission

class FilesystemModule(BaseModule):
    @requires_permission(Permission.FILESYSTEM_WRITE, reason="Writes to disk")
    @sensitive_action(risk_level=RiskLevel.HIGH, irreversible=True)
    @rate_limited(calls_per_minute=60)
    @audit_trail("detailed")
    async def _action_delete_file(self, params: dict) -> dict:
        ...
```

See the [Annotators documentation](../annotators/) for complete decorator reference.

---


## Streaming Integration

### @streams_progress

Actions that perform long-running operations can emit progress updates:

```python
from llmos_bridge.orchestration.streaming_decorators import streams_progress
from llmos_bridge.orchestration.stream import _STREAM_KEY, ActionStream

class ApiHttpModule(BaseModule):
    @streams_progress
    async def _action_download_file(self, params: dict) -> dict:
        stream: ActionStream | None = params.pop(_STREAM_KEY, None)
        total_bytes = 0
        async for chunk in download(url):
            total_bytes += len(chunk)
            if stream:
                await stream.emit_progress(
                    percent=(total_bytes / expected) * 100,
                    message=f"Downloaded {total_bytes} bytes"
                )
        return {"path": output_path, "bytes": total_bytes}
```

The executor detects `@streams_progress` and injects an `ActionStream` into `params["_stream"]`. The stream emits events via the EventBus, which are delivered to clients through SSE at `GET /plans/{id}/stream`.

### Streaming Metadata Collection

`BaseModule._collect_streaming_metadata()` scans for `@streams_progress`:

```python
meta = module._collect_streaming_metadata()
# {"download_file": {"streams_progress": True}}
```

This metadata is included in the `ActionSpec.streams_progress` field of the manifest.

---


## Configuration

### Module-Specific Configuration

Modules can declare a configuration model:

```python
from llmos_bridge.modules.config import ModuleConfigBase, ConfigField

class VisionConfig(ModuleConfigBase):
    cache_max_entries: int = ConfigField(5, description="Max cached parse results")
    cache_ttl_seconds: float = ConfigField(2.0, description="Cache TTL in seconds")
    speculative_prefetch: bool = ConfigField(True, description="Background prefetch")

class VisionModule(BaseModule):
    CONFIG_MODEL = VisionConfig
```

The `CONFIG_MODEL` is used to:
1. Generate JSON Schema for the API endpoint
2. Validate configuration updates at runtime
3. Provide typed access via `self.config`

### Runtime Configuration Updates

```python
async def on_config_update(self, config: dict) -> None:
    """Called when configuration changes at runtime."""
    self._cache.max_entries = config.get("cache_max_entries", 5)
    self._cache.ttl = config.get("cache_ttl_seconds", 2.0)
```

---


## Context Snippet

Modules can contribute dynamic content to the LLM agent's system prompt:

```python
def get_context_snippet(self) -> str | None:
    """Return dynamic system prompt content."""
    return (
        "The filesystem module provides access to the local filesystem. "
        f"Sandbox paths: {', '.join(self._sandbox_paths)}. "
        f"Current working directory: {os.getcwd()}."
    )
```

This is included in the response to `GET /context` and is injected into the LLM's system prompt by the SDK.

---


## Resource Estimation

Modules can provide pre-execution cost hints for the scheduler:

```python
def estimate_cost(self, action: str, params: dict) -> ResourceEstimate:
    if action == "download_file":
        return ResourceEstimate(
            estimated_duration_seconds=30.0,
            estimated_memory_mb=50,
            estimated_io_operations=100,
            confidence=0.3,
        )
    return ResourceEstimate()  # defaults
```

The `ResourceEstimate` fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `estimated_duration_seconds` | float | 1.0 | Expected execution time |
| `estimated_memory_mb` | float | 10.0 | Expected memory usage |
| `estimated_cpu_percent` | float | 10.0 | Expected CPU utilization |
| `estimated_io_operations` | int | 0 | Expected I/O operations |
| `confidence` | float | 0.5 | Confidence in estimate (0.0 to 1.0) |

---


## Module Policy

Declare runtime constraints:

```python
def policy_rules(self) -> ModulePolicy:
    return ModulePolicy(
        max_parallel_calls=3,       # Max concurrent actions (0 = unlimited)
        cooldown_seconds=0.0,       # Min delay between calls
        allow_remote_invocation=True,
        execution_timeout=300,      # Default timeout
        max_memory_mb=512,
        retry_on_failure=True,
    )
```

---


## Inter-Module Communication

Modules should never import each other directly. LLMOS Bridge provides two patterns for cross-module interaction:

### Pattern 1: ServiceBus (recommended)

Modules expose services that other modules can discover and call through the central `ServiceBus`. All calls route through the provider's `execute()` method, preserving security decorators, audit trails, and rate limiting.

**Declaring a service** (provider module):

```python
from llmos_bridge.modules.manifest import ServiceDescriptor

def register_services(self) -> list[ServiceDescriptor]:
    return [
        ServiceDescriptor(
            name="filesystem.path_resolver",
            methods=["resolve", "validate", "normalize"],
            description="Path resolution and validation service",
        )
    ]
```

**Calling a service** (consumer module):

```python
async def _action_process_image(self, params: dict) -> dict:
    # Call the vision module's parse_screen action through ServiceBus
    result = await self._ctx.service_bus.call(
        "vision", "parse_screen", {"capture": True}
    )
    # result contains the VisionParseResult from the vision module
    elements = result.get("elements", [])
    return {"element_count": len(elements)}
```

**Checking availability**:

```python
if self._ctx.service_bus.is_available("vision"):
    result = await self._ctx.service_bus.call("vision", "parse_screen", {...})
```

### Pattern 2: Registry Access (for tight coupling)

When a module fundamentally depends on another (e.g., `computer_control` requires `vision` + `gui`), it can access the registry directly. This pattern should be rare - prefer ServiceBus for loose coupling.

```python
class ComputerControlModule(BaseModule):
    def set_registry(self, registry: ModuleRegistry) -> None:
        self._registry = registry

    def _get_vision_module(self) -> BaseModule:
        if not self._registry.is_available("vision"):
            raise ActionExecutionError("vision module not available")
        return self._registry.get("vision")

    async def _action_click_element(self, params: dict) -> dict:
        vision = self._get_vision_module()
        parse_result = await vision.execute("capture_and_parse", {})
        # ... resolve element and click
```

**Note**: Registry access is wired in `server.py` via `module.set_registry(registry)`. Declare the dependency in your manifest's `module_dependencies`:

```python
def get_manifest(self) -> ModuleManifest:
    return ModuleManifest(
        ...,
        module_dependencies={"vision": ">=1.0.0", "gui": ">=1.0.0"},
        consumes_services=["vision", "gui"],
    )
```

---


## Registration and Discovery

### Built-in Module Registration

Built-in modules are registered in `create_app()`:

```python
# In api/server.py
from llmos_bridge.modules.filesystem.module import FilesystemModule

registry = ModuleRegistry()
registry.register(FilesystemModule())
```

### Platform Guard

The `PlatformGuard` automatically excludes modules incompatible with the current platform:

```python
# Module with SUPPORTED_PLATFORMS = [Platform.RASPBERRY_PI]
# Running on x86_64 Linux → excluded from registry
```

### Module Discovery

The `ModuleRegistry` supports dynamic discovery:

```python
# List all registered modules
modules = registry.list_modules()

# Get specific module
fs = registry.get("filesystem")

# Check availability
available = registry.is_available("browser")
```

---


## Complete Module Example

A minimal but complete module implementation:

```python
"""Example: System information module."""

from __future__ import annotations

import platform
from typing import Any

from llmos_bridge.modules.base import BaseModule, Platform
from llmos_bridge.modules.manifest import (
    ActionSpec,
    ModuleManifest,
    ParamSpec,
)
from llmos_bridge.security.decorators import (
    audit_trail,
    requires_permission,
)
from llmos_bridge.security.permissions import Permission


class SystemInfoModule(BaseModule):
    MODULE_ID = "system_info"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]

    async def _action_get_info(self, params: dict) -> dict:
        """Return basic system information."""
        return {
            "os": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        }

    @requires_permission(Permission.PROCESS_EXECUTE)
    @audit_trail("standard")
    async def _action_get_uptime(self, params: dict) -> dict:
        """Return system uptime."""
        import psutil
        boot_time = psutil.boot_time()
        uptime = time.time() - boot_time
        return {"uptime_seconds": uptime}

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=self.MODULE_ID,
            version=self.VERSION,
            description="System information and diagnostics",
            actions=[
                ActionSpec(
                    name="get_info",
                    description="Get basic system information",
                    returns="object",
                ),
                ActionSpec(
                    name="get_uptime",
                    description="Get system uptime in seconds",
                    returns="object",
                    permissions=["process.execute"],
                ),
            ],
        )
```

This module:
- Declares identity (`system_info`, version `1.0.0`, all platforms)
- Exposes two actions via naming convention
- Applies security decorators to the privileged action
- Publishes a manifest with action specifications
- Uses async methods (blocking I/O wrapped in `asyncio.to_thread()` when needed)

---


## Module Categories

LLMOS Bridge organizes its 18 built-in modules into 8 categories:

### System Modules
Core infrastructure modules that manage the daemon itself.

| Module | Actions | Description |
|--------|---------|-------------|
| `filesystem` | 13 | File and directory operations |
| `os_exec` | 9 | Process execution and system info |
| `module_manager` | 22 | Module lifecycle and hub integration |
| `security` | 6 | Permission and audit management |
| `recording` | 6 | Workflow recording and replay |
| `triggers` | 6 | Reactive automation |

### Network Module
| Module | Actions | Description |
|--------|---------|-------------|
| `api_http` | 17 | HTTP, GraphQL, OAuth2, email, webhooks |

### Automation Modules
GUI and browser automation for computer use.

| Module | Actions | Description |
|--------|---------|-------------|
| `browser` | 14 | Playwright-based web automation |
| `gui` | 13 | Physical GUI interaction (click, type, scroll) |
| `computer_control` | 9 | Semantic GUI automation gateway |
| `window_tracker` | 8 | Window focus and context recovery |

### Database Modules
| Module | Actions | Description |
|--------|---------|-------------|
| `database` | 13 | Direct SQL operations |
| `db_gateway` | 12 | Semantic database access (no SQL) |

### Document Modules
Office document manipulation.

| Module | Actions | Description |
|--------|---------|-------------|
| `excel` | 42 | Excel spreadsheet operations |
| `word` | 30 | Word document operations |
| `powerpoint` | 25 | PowerPoint presentation operations |

### Perception Module
| Module | Actions | Description |
|--------|---------|-------------|
| `vision` | 4 | Screen parsing via OmniParser |

### Hardware Module
| Module | Actions | Description |
|--------|---------|-------------|
| `iot` | 10 | GPIO and IoT device control |

---


## Best Practices

### Action Design

1. **One action, one responsibility** - Each action should do exactly one thing. Prefer `read_file` + `write_file` over `read_and_write_file`.

2. **Return structured data** - Always return a dict with named fields. Avoid returning raw strings or bare lists.

3. **Use async I/O** - For blocking operations (file I/O, subprocess calls), wrap in `asyncio.to_thread()`.

4. **Accept params as dict** - The executor passes a plain dict. Extract parameters with `.get()` for optional fields.

5. **Handle missing params gracefully** - Use defaults for optional parameters. Raise clear errors for missing required params.

### Security

1. **Apply decorators to every action** - Even read-only actions should have `@requires_permission`.

2. **Use the most specific permission** - `Permission.FILESYSTEM_DELETE` rather than `Permission.FILESYSTEM_WRITE` for deletion.

3. **Mark irreversible actions** - `@sensitive_action(irreversible=True)` enables informed approval decisions.

4. **Use audit trails** - `"detailed"` for sensitive operations, `"standard"` for normal operations.

### Testing

1. **Unit test each action independently** - Mock external dependencies, test params in/result out.

2. **Test with `security_advanced={"enable_decorators": False}`** - Unless specifically testing decorator enforcement.

3. **Test error paths** - Missing params, invalid inputs, permission denials, timeout scenarios.

4. **Mark tests appropriately** - `@pytest.mark.unit` for no-I/O, `@pytest.mark.integration` for real filesystem.
