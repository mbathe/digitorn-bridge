---

id: creating-modules
title: Creating Modules
sidebar_position: 2
format: md
---



# Creating Custom Modules

This guide walks you through building a custom LLMOS Bridge module from scratch and installing it locally. By the end, you will have a fully operational module that the daemon can discover, execute, and manage.

---


## Prerequisites

- LLMOS Bridge daemon running (standalone mode works fine)
- Python 3.11+
- Understanding of async/await

---


## 1. Package Structure

A module package is a directory with this layout:

```
my_module/                    ← package root (this is what you install)
  llmos-module.toml           ← REQUIRED - package descriptor
  my_module/                  ← Python package (importable)
    __init__.py
    module.py                 ← REQUIRED - BaseModule subclass
    params.py                 ← recommended - Pydantic parameter models
  README.md                   ← required for hub publishing
  CHANGELOG.md                ← recommended
  docs/
    actions.md                ← recommended
    integration.md            ← recommended
```

The `module_class_path` in `llmos-module.toml` links the descriptor to your Python class:

```toml
module_class_path = "my_module.module:MyModule"
#                    ^^^^^^^^^^ ^^^^^^ ^^^^^^^^
#                    package    file   class
```

When installed locally, the package root is added to `PYTHONPATH` so the worker subprocess can import `my_module.module`.

---


## 2. The `llmos-module.toml` Descriptor

This is the single source of truth for module identity. It must be at the root of your package directory.

```toml
[module]
# --- Identity ---
module_id = "my_module"           # snake_case, globally unique
version   = "1.0.0"              # semantic version
description = "What this module does in one line."
author    = "Name <email>"
license   = "MIT"
homepage  = "https://github.com/you/my-module"

# --- Entry point ---
module_class_path = "my_module.module:MyModule"

# --- Platform support ---
platforms = ["all"]
# or: ["linux", "windows", "macos", "raspberry_pi"]

# --- Python dependencies (pip format) ---
requirements = [
    "httpx>=0.27.0",
    "beautifulsoup4>=4.12.0",
]

# --- Module-to-module dependencies ---
[module.module_dependencies]
# filesystem = ">=1.0.0"   # only if you need another module to be installed

# --- Hub metadata ---
tags = ["search", "network", "web"]
sandbox_level = "basic"      # "none" | "basic" | "strict" | "isolated"
module_type = "user"         # "user" | "daemon" | "system"
min_bridge_version = ""      # e.g. "1.2.0"
icon = "🔍"

# --- Action declarations (for hub listing - not the full spec) ---
[[module.actions]]
name = "search_web"
description = "Search the web and return structured results."
risk_level = "medium"
permission = "local_worker"
category = "network"

[[module.actions]]
name = "fetch_page"
description = "Fetch a URL and extract readable text."
risk_level = "low"
permission = "readonly"
category = "network"

# --- Capabilities summary ---
[module.capabilities]
permissions = ["network.read", "network.external"]
side_effects = ["network_request"]
events_emitted = []
events_subscribed = []
services_provided = []
services_consumed = []

# --- Documentation paths ---
[module.docs]
readme     = "README.md"
changelog  = "CHANGELOG.md"
actions    = "docs/actions.md"
integration = "docs/integration.md"
```

### Key fields

| Field | Required | Notes |
|-------|----------|-------|
| `module_id` | ✓ | Unique across all modules. Used as registry key. |
| `version` | ✓ | Semantic versioning (`major.minor.patch`). |
| `module_class_path` | ✓ | `"package.module:ClassName"` notation. |
| `requirements` | - | Installed in an isolated venv before the worker starts. |
| `module_dependencies` | - | Other modules that must be installed first. |
| `sandbox_level` | - | Isolation level for the subprocess. |

---


## 3. Parameter Models (`params.py`)

Define a Pydantic v2 model for each action. This makes your action self-documenting and validates input automatically.

```python
# my_module/params.py
from pydantic import BaseModel, Field


class SearchWebParams(BaseModel):
    query: str = Field(description="Search query string.")
    max_results: int = Field(default=5, ge=1, le=20, description="Number of results to return.")
    safe_search: bool = Field(default=True, description="Enable safe search filtering.")


class FetchPageParams(BaseModel):
    url: str = Field(description="URL to fetch.")
    extract_text_only: bool = Field(default=True, description="Strip HTML tags and return plain text.")
    max_length: int = Field(default=5000, ge=100, le=50000, description="Max characters to return.")
```

**In your action**, validate with `Model.model_validate(params)`:

```python
async def _action_search_web(self, params: dict) -> dict:
    p = SearchWebParams.model_validate(params)
    # Use p.query, p.max_results, p.safe_search
```

---


## 4. The Module Class (`module.py`)

### 4.1 Minimal structure

```python
from __future__ import annotations
from typing import Any

from llmos_bridge.modules.base import BaseModule, ModulePolicy, Platform, ResourceEstimate
from llmos_bridge.modules.manifest import ActionSpec, ModuleManifest, ParamSpec

from my_module.params import SearchWebParams, FetchPageParams


class MyModule(BaseModule):
    # ----------------------------------------------------------------
    # Identity - REQUIRED
    # ----------------------------------------------------------------
    MODULE_ID = "my_module"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]

    # ----------------------------------------------------------------
    # Dependency check - called in __init__
    # ----------------------------------------------------------------
    def _check_dependencies(self) -> None:
        """Raise ModuleLoadError if a required package is missing."""
        try:
            import httpx
            import bs4
        except ImportError as e:
            from llmos_bridge.exceptions import ModuleLoadError
            raise ModuleLoadError(
                module_id=self.MODULE_ID,
                reason=f"Missing required package: {e}. Install with: pip install httpx beautifulsoup4",
            )

    # ----------------------------------------------------------------
    # Actions - one async method per action, named _action_<name>
    # ----------------------------------------------------------------
    async def _action_search_web(self, params: dict[str, Any]) -> dict[str, Any]:
        p = SearchWebParams.model_validate(params)
        # ... implementation ...
        return {"results": [], "query": p.query}

    async def _action_fetch_page(self, params: dict[str, Any]) -> dict[str, Any]:
        p = FetchPageParams.model_validate(params)
        # ... implementation ...
        return {"url": p.url, "content": ""}

    # ----------------------------------------------------------------
    # Manifest - REQUIRED
    # ----------------------------------------------------------------
    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=self.MODULE_ID,
            version=self.VERSION,
            description="...",
            platforms=["all"],
            actions=[
                ActionSpec(
                    name="search_web",
                    description="Search the web.",
                    params=[ParamSpec("query", "string", "Search query.")],
                    returns="object",
                ),
                ActionSpec(
                    name="fetch_page",
                    description="Fetch a URL.",
                    params=[ParamSpec("url", "string", "URL to fetch.")],
                    returns="object",
                ),
            ],
        )
```

### 4.2 Adding security decorators

Import from `llmos_bridge.security`:

```python
from llmos_bridge.security.decorators import (
    requires_permission,
    sensitive_action,
    rate_limited,
    audit_trail,
    data_classification,
)
from llmos_bridge.security.models import Permission, RiskLevel, DataClassification
```

**Stack decorators outermost → innermost:**

```python
@requires_permission(Permission.NETWORK_EXTERNAL, reason="Makes outbound HTTP requests")
@rate_limited(calls_per_minute=30)
@audit_trail("standard")
async def _action_search_web(self, params: dict[str, Any]) -> dict[str, Any]:
    ...
```

**Decorator reference:**

| Decorator | Purpose | Metadata added to manifest |
|-----------|---------|--------------------------|
| `@requires_permission(*perms)` | Check OS-level permission at runtime | `permissions`, `permission_reason` |
| `@sensitive_action(risk_level)` | Emit audit event, flag risk | `risk_level`, `irreversible` |
| `@rate_limited(calls_per_minute)` | Throttle calls | `rate_limit` |
| `@audit_trail("standard")` | Before/after log to event bus | `audit_level` |
| `@data_classification(level)` | Tag data sensitivity | `data_classification` |
| `@intent_verified(strict)` | LLM intent verification | `intent_verified` |

**Declare permissions in the manifest:**

```python
def get_manifest(self) -> ModuleManifest:
    return ModuleManifest(
        ...
        declared_permissions=[
            Permission.NETWORK_READ,
            Permission.NETWORK_EXTERNAL,
        ],
    )
```

### 4.3 Available Permission constants

```python
from llmos_bridge.security.models import Permission

# Filesystem
Permission.FILESYSTEM_READ        # "filesystem.read"
Permission.FILESYSTEM_WRITE       # "filesystem.write"
Permission.FILESYSTEM_DELETE      # "filesystem.delete"
Permission.FILESYSTEM_SENSITIVE   # "filesystem.sensitive"

# Network
Permission.NETWORK_READ           # "network.read"
Permission.NETWORK_SEND           # "network.send"
Permission.NETWORK_EXTERNAL       # "network.external"

# Device
Permission.CAMERA                 # "device.camera"
Permission.MICROPHONE             # "device.microphone"
Permission.SCREEN_CAPTURE         # "device.screen"
Permission.KEYBOARD               # "device.keyboard"

# Data
Permission.DATABASE_READ          # "data.database.read"
Permission.DATABASE_WRITE         # "data.database.write"
Permission.CREDENTIALS            # "data.credentials"
Permission.PERSONAL_DATA          # "data.personal"

# OS
Permission.PROCESS_EXECUTE        # "os.process.execute"
Permission.PROCESS_KILL           # "os.process.kill"
Permission.ENV_READ               # "os.environment.read"
Permission.ADMIN                  # "os.admin"

# Apps
Permission.BROWSER                # "app.browser"
Permission.EMAIL_SEND             # "app.email.send"

# Module management
Permission.MODULE_READ            # "module.read"
Permission.MODULE_MANAGE          # "module.manage"
Permission.MODULE_INSTALL         # "module.install"

# Community modules - use any dotted string
"my_module.custom_resource"
```

### 4.4 Lifecycle hooks

```python
async def on_start(self) -> None:
    """Called when module transitions to ACTIVE. Initialize connections."""
    self._client = httpx.AsyncClient(timeout=30.0)

async def on_stop(self) -> None:
    """Called when module is disabled. Release resources."""
    if hasattr(self, "_client"):
        await self._client.aclose()

async def on_config_update(self, config: dict[str, Any]) -> None:
    """Called when runtime config is updated via API."""
    await super().on_config_update(config)  # Validates against CONFIG_MODEL
    # Re-initialize with new config if needed

async def on_install(self) -> None:
    """Called once when module is first installed."""
    # One-time setup: create directories, download assets, etc.

async def on_update(self, old_version: str) -> None:
    """Called when module is upgraded."""
    if old_version < "2.0.0":
        # Migration logic
        pass
```

**Important:** Never override `__init__()`. Use `on_start()` to initialize resources.

### 4.5 Introspection overrides

```python
async def health_check(self) -> dict[str, Any]:
    """Custom health check."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get("https://example.com", timeout=5.0)
        return {"status": "ok", "connectivity": True}
    except Exception as e:
        return {"status": "degraded", "connectivity": False, "error": str(e)}

def metrics(self) -> dict[str, Any]:
    """Expose operational metrics."""
    return {
        "requests_made": self._request_count,
        "cache_hits": self._cache_hits,
        "errors": self._error_count,
    }

def get_context_snippet(self) -> str | None:
    """Inject live context into LLM system prompt."""
    return (
        "Available web search: use web_search module for current information. "
        "Rate limit: 30 searches/minute."
    )

def policy_rules(self) -> ModulePolicy:
    """Declare runtime constraints."""
    return ModulePolicy(
        max_parallel_calls=5,
        cooldown_seconds=1.0,
        allow_remote_invocation=True,
    )

async def estimate_cost(self, action: str, params: dict) -> ResourceEstimate:
    """Pre-execution cost estimate for the scheduler."""
    if action == "search_web":
        return ResourceEstimate(
            estimated_duration_seconds=2.0,
            estimated_memory_mb=20.0,
            confidence=0.7,
        )
    return ResourceEstimate(estimated_duration_seconds=1.0, confidence=0.5)
```

### 4.6 Configurable modules

```python
from llmos_bridge.modules.config import ConfigField, ModuleConfigBase

class MyModuleConfig(ModuleConfigBase):
    api_key: str = ConfigField(
        default="",
        label="API Key",
        category="Authentication",
        secret=True,
        description="Your API key for external service.",
    )
    timeout: int = ConfigField(
        default=30,
        label="Request Timeout",
        category="Network",
        ge=1, le=120,
        description="HTTP request timeout in seconds.",
    )


class MyModule(BaseModule):
    CONFIG_MODEL = MyModuleConfig  # Enables runtime config via API
    ...

    async def on_config_update(self, config: dict[str, Any]) -> None:
        await super().on_config_update(config)
        # self.config is now a validated MyModuleConfig instance
        self._api_key = self.config.api_key
```

---


## 5. Full ActionSpec Reference

```python
ActionSpec(
    # Required
    name="search_web",
    description="Search the web and return structured results.",

    # Parameters
    params=[
        ParamSpec("query", "string", "Search query.", required=True),
        ParamSpec("max_results", "integer", "Max results.", required=False, default=5),
        ParamSpec("engine", "string", "Search engine.", required=False,
                  enum=["duckduckgo", "google"], default="duckduckgo"),
    ],

    # Return type
    returns="object",
    returns_description='{"results": [{"title": str, "url": str, "snippet": str}], "total": int}',

    # Security
    permission_required="local_worker",  # security profile minimum

    # Platform targeting
    platforms=["all"],

    # Tags for discovery
    tags=["search", "network", "web"],

    # JSON Schema for output validation (optional but recommended)
    output_schema={
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title":   {"type": "string"},
                        "url":     {"type": "string"},
                        "snippet": {"type": "string"},
                    },
                },
            },
            "total": {"type": "integer"},
        },
        "required": ["results", "total"],
    },

    # Side effects declaration
    side_effects=["network_request"],

    # Execution mode
    execution_mode="async",  # "sync" | "async" | "background" | "scheduled"

    # Examples for documentation and testing
    examples=[
        {
            "description": "Search for Python documentation",
            "params": {"query": "Python asyncio tutorial"},
            "expected_output": {
                "results": [{"title": "asyncio docs", "url": "...", "snippet": "..."}],
                "total": 5,
            },
        }
    ],
)
```

---


## 6. Installing a Module Locally

Once your module is ready, install it via the REST API:

```bash
# Install
curl -X POST http://localhost:40000/admin/modules/install \
  -H "Content-Type: application/json" \
  -d '{"path": "/absolute/path/to/my_module"}'

# Expected response
{
  "success": true,
  "module_id": "my_module",
  "version": "1.0.0",
  "installed_deps": ["httpx>=0.27.0", "beautifulsoup4>=4.12.0"],
  "validation_warnings": []
}
```

Or via IML plan:

```json
{
  "plan_id": "install-my-module",
  "protocol_version": "2.0",
  "description": "Install my custom module",
  "actions": [
    {
      "id": "a1",
      "action": "install_module",
      "module": "module_manager",
      "params": {
        "source": "local",
        "path": "/absolute/path/to/my_module"
      }
    }
  ]
}
```

### What happens during installation

1. **Parse** `llmos-module.toml` - extract identity, class path, requirements
2. **Validate** module structure - check all required files, score 0-100
3. **Check module deps** - verify required modules are installed in the registry
4. **Create venv eagerly** - `pip install` (or `uv pip install`) requirements into isolated `~/.llmos/modules/.venvs/my_module/`
5. **Register in index** - persist to `~/.llmos/modules/modules.db`
6. **Register in registry** - create `IsolatedModuleProxy` with `PYTHONPATH` set to package root
7. **Call `on_install()`** - run one-time setup hook

### Validation score

Installation requires **passing all blocking checks** (score ≥ 35 minimum):

| Check | Points | Blocking? |
|-------|--------|-----------|
| `llmos-module.toml` exists and is valid | 20 | ✓ |
| `module.py` with BaseModule subclass | 15 | ✓ |
| `module_id` not empty | 5 | ✓ |
| `version` not empty | 5 | ✓ |
| `params.py` exists | 10 | - (warning) |
| `README.md` exists | 10 | ✓ (hub) |
| README has required sections | 10 | - (warning) |
| `CHANGELOG.md` exists | 5 | - (warning) |
| `docs/actions.md` exists | 10 | - (warning) |
| `docs/integration.md` exists | 5 | - (warning) |
| At least one action declared | 5 | - (warning) |

**Hub-ready** = score ≥ 70 and no blocking issues.

### Upgrade and uninstall

```bash
# Upgrade to a new version
curl -X POST http://localhost:40000/admin/modules/my_module/upgrade \
  -H "Content-Type: application/json" \
  -d '{"path": "/path/to/my_module_v2"}'

# Uninstall
curl -X DELETE http://localhost:40000/admin/modules/my_module/uninstall

# List installed community modules
curl http://localhost:40000/admin/modules/installed
```

---


## 7. Configuration

By default, `hub.local_install_enabled=true` means local installation works with zero configuration:

```yaml
# ~/.llmos/config.yaml (or environment variables)
hub:
  local_install_enabled: true   # default - enables local installs
  install_dir: ~/.llmos/modules # where venvs and the index live
  require_signatures: true      # false = skip signature check (local only)
```

Signatures are **not required** for local installs - they are checked only when installing from the hub.

---


## 8. Testing Your Module

### Unit tests (no daemon needed)

```python
import pytest
from unittest.mock import AsyncMock

async def test_search_returns_results():
    from my_module.module import MyModule
    mod = MyModule.__new__(MyModule)  # skip __init__ + _check_dependencies
    mod._security = None
    mod._ctx = None
    mod._dynamic_actions = {}
    mod._dynamic_specs = {}
    mod._config = None

    # Mock external call
    mod._search_duckduckgo = AsyncMock(return_value=[
        {"title": "Test", "url": "https://example.com", "snippet": "A test result"},
    ])

    result = await mod._action_search_web({"query": "test", "max_results": 1})
    assert result["total"] == 1
    assert result["results"][0]["title"] == "Test"
```

### Integration test (real venv + subprocess)

```python
async def test_module_installs_and_runs(tmp_path):
    from llmos_bridge.hub.installer import ModuleInstaller
    # Build installer with real VenvManager...
    result = await installer.install_from_path(Path("/Desktop/my_module"))
    assert result.success
```

### Validate before installing

```python
from llmos_bridge.hub.validator import ModuleValidator

result = ModuleValidator().validate(Path("/Desktop/my_module"))
print(f"Score: {result.score}/100")
print(f"Issues: {result.issues}")
print(f"Warnings: {result.warnings}")
print(f"Hub-ready: {result.hub_ready}")
```

---


## 9. Common Pitfalls

| Problem | Cause | Fix |
|---------|-------|-----|
| `ModuleLoadError: missing required package` | `_check_dependencies` fails | Ensure deps are in `requirements` list in toml |
| Worker can't import module | `PYTHONPATH` not set | Use local install (auto-injects PYTHONPATH) - don't manually `sys.path.insert` |
| `Invalid package: No llmos-module.toml` | Wrong path | Pass the directory containing the toml, not a subdirectory |
| Module already installed | Same `module_id` in index | Use `upgrade` endpoint instead |
| Rate limit exceeded | Too many calls in short window | Adjust `@rate_limited` or caller code |
| `on_start()` fails | Dependency not available at runtime | Check connectivity, handle in try/except, set status in `health_check()` |
| Module excluded on platform | `SUPPORTED_PLATFORMS` too narrow | Add `Platform.ALL` or the target platform |
| Config not applied | `CONFIG_MODEL` not set | Set `CONFIG_MODEL = MyConfig` as class attribute |

---


## 10. Complete Example

See the template module at [packages/llmos-module-template/](../../../packages/llmos-module-template/) for a minimal but complete, production-ready example.

For a real-world example with networking, dependencies, rate limiting, and lifecycle management, see the `web_search` module (installable from `/Desktop/llmos_web_search/`).
