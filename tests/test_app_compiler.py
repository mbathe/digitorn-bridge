"""Tests for the App YAML compiler, variable resolver, and bootstrapper.

Tests are organized by component:
    - TestVariableResolver       - {{env.X}}, {{var}}, {{?? fallback}}
    - TestSchemaModels           - Pydantic parsing of YAML structure
    - TestCompiler               - Full compilation pipeline
    - TestCompilerErrors         - Error collection and reporting
    - TestSecurityProfileBuild   - Capabilities → SecurityProfile mapping
    - TestBootstrapper           - Setup action execution
    - TestEndToEnd               - YAML file → compile → bootstrap
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from digitorn.core.app.variables import collect_unresolved, resolve_variables
from digitorn.core.app.schema import (
    AppDefinition,
    AppMeta,
    CapabilitiesConfig,
    CapabilityGrant,
    ModuleBlock,
    SetupStep,
)
from digitorn.core.app.errors import (
    ActionNotFoundError,
    AppBootstrapError,
    AppCompilationError,
    ConstraintValidationError,
    ModuleNotFoundError,
    ParamsValidationError,
    VariableResolutionError,
)
from digitorn.core.app.compiler import (
    AppYAMLCompiler,
    CompiledApp,
    CompiledModuleConfig,
    CompiledSetupStep,
)
from digitorn.core.app.bootstrapper import AppBootstrapper, BootstrapResult, SetupStepResult


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _make_action_entry(name: str, params_model: type | None = None):
    """Create a mock ActionEntry for the registry."""
    entry = MagicMock()
    entry.params_model = params_model
    entry.spec = MagicMock()
    entry.spec.name = name
    entry.spec.params = []
    return entry


def _make_manifest(action_names: list[str], constraints: list | None = None):
    """Create a mock ModuleManifest."""
    manifest = MagicMock()
    manifest.action_names.return_value = action_names
    manifest.actions = []
    for name in action_names:
        spec = MagicMock()
        spec.name = name
        spec.params = []
        manifest.actions.append(spec)
    manifest.supported_constraints = constraints or []
    return manifest


def _make_module(module_id: str, action_names: list[str], constraints: list | None = None):
    """Create a mock BaseModule with the given actions."""
    module = MagicMock()
    module.MODULE_ID = module_id
    manifest = _make_manifest(action_names, constraints)
    module.get_manifest.return_value = manifest

    # Build _action_registry
    registry = {}
    for name in action_names:
        registry[name] = _make_action_entry(name)
    module._action_registry = registry

    # execute returns ActionResult-like
    async def mock_execute(action, params):
        result = MagicMock()
        result.success = True
        result.data = {"action": action, "params": params}
        result.error = None
        return result

    module.execute = AsyncMock(side_effect=mock_execute)
    module.on_config_update = AsyncMock()
    module.on_start = AsyncMock()
    module.on_stop = AsyncMock()
    return module


def _make_registry(modules: dict[str, Any] | None = None):
    """Create a mock ModuleRegistry."""
    registry = MagicMock()
    modules = modules or {}
    registry.list_available.return_value = list(modules.keys())

    def get(mid):
        if mid not in modules:
            raise Exception(f"Module '{mid}' not found")
        return modules[mid]

    registry.get = MagicMock(side_effect=get)
    registry.create = MagicMock(side_effect=get)
    registry.is_available = MagicMock(side_effect=lambda mid: mid in modules)
    return registry


def _with_agent(raw: dict) -> dict:
    """Inject a minimal agent into a raw YAML dict (compiler requires >= 1 agent)."""
    if "agents" not in raw:
        raw["agents"] = [{"id": "default", "brain": {"model": "test-model"}}]
    return raw


# ═══════════════════════════════════════════════════════════════════════
# Variable Resolver
# ═══════════════════════════════════════════════════════════════════════


class TestVariableResolver:
    """Tests for variables.py - template resolution."""

    def test_simple_variable(self):
        result = resolve_variables("hello {{name}}", {"name": "world"})
        assert result == "hello world"

    def test_env_variable(self):
        result = resolve_variables(
            "path: {{env.HOME}}", {}, env={"HOME": "/home/user"}
        )
        assert result == "path: /home/user"

    def test_secret_variable(self):
        result = resolve_variables(
            "key: {{secret.API_KEY}}", {}, env={"API_KEY": "sk-123"}
        )
        assert result == "key: sk-123"

    def test_nested_dict(self):
        data = {"host": "{{db_host}}", "port": 5432, "options": {"ssl": "{{use_ssl}}"}}
        result = resolve_variables(data, {"db_host": "localhost", "use_ssl": "true"})
        assert result == {"host": "localhost", "port": 5432, "options": {"ssl": "true"}}

    def test_list_resolution(self):
        data = ["{{a}}", "{{b}}", "literal"]
        result = resolve_variables(data, {"a": "x", "b": "y"})
        assert result == ["x", "y", "literal"]

    def test_null_coalescing(self):
        result = resolve_variables(
            "{{env.MISSING ?? 'default_val'}}", {}, env={}
        )
        assert result == "default_val"

    def test_null_coalescing_chain(self):
        result = resolve_variables(
            "{{env.A ?? env.B ?? 'fallback'}}", {}, env={"B": "from_b"}
        )
        assert result == "from_b"

    def test_missing_variable_raises(self):
        with pytest.raises(ValueError, match="not defined"):
            resolve_variables("{{missing}}", {})

    def test_missing_env_passthrough(self):
        # env.X is lenient at compile (symmetric with secret.X) - unresolved
        # templates pass through so the runtime credential resolver can
        # substitute them per-user at activation time.
        result = resolve_variables("{{env.NOPE}}", {}, env={})
        assert result == "{{env.NOPE}}"

    def test_recursive_resolution(self):
        result = resolve_variables(
            "{{base}}/data",
            {"base": "{{env.HOME}}/project"},
            env={"HOME": "/home/user"},
        )
        assert result == "/home/user/project/data"

    def test_circular_detection(self):
        with pytest.raises(ValueError, match="max depth"):
            resolve_variables(
                "{{a}}", {"a": "{{b}}", "b": "{{a}}"}, env={}
            )

    def test_scalars_pass_through(self):
        assert resolve_variables(42, {}) == 42
        assert resolve_variables(True, {}) is True
        assert resolve_variables(None, {}) is None
        assert resolve_variables(3.14, {}) == 3.14

    def test_collect_unresolved(self):
        data = {"host": "{{db_host}}", "port": 5432, "items": ["{{env.X}}"]}
        found = collect_unresolved(data)
        assert set(found) == {"db_host", "env.X"}

    def test_collect_unresolved_empty(self):
        assert collect_unresolved({"host": "localhost"}) == []

    def test_multiple_vars_in_string(self):
        result = resolve_variables("{{a}}-{{b}}", {"a": "hello", "b": "world"})
        assert result == "hello-world"

    def test_system_shell_family_variables_exist(self):
        shell_family = resolve_variables("{{sys.shell_family}}", {})
        shell = resolve_variables("{{sys.shell}}", {})
        path_sep = resolve_variables("{{sys.path_sep}}", {})

        assert shell_family in {"powershell", "cmd", "bash", "sh", "zsh", "fish", "dash", "unknown"}
        assert shell
        assert path_sep in {"/", "\\"}

    def test_system_temp_dir_uses_platform_tempdir(self):
        import tempfile

        result = resolve_variables("{{sys.temp_dir}}", {})
        legacy = resolve_variables("{{sys.tmpdir}}", {})

        assert result == tempfile.gettempdir()
        assert legacy == tempfile.gettempdir()


# ═══════════════════════════════════════════════════════════════════════
# Schema Models
# ═══════════════════════════════════════════════════════════════════════


class TestSchemaModels:
    """Tests for schema.py - Pydantic parsing."""

    def test_minimal_app(self):
        # Canonical shape (v2): 7 nested top-level blocks. Legacy flat
        # shape still works via apply_schema_aliases at compile time;
        # this test asserts the canonical form directly.
        raw = {"app": {"app_id": "test", "name": "Test App"}}
        defn = AppDefinition.model_validate(raw)
        assert defn.app.app_id == "test"
        assert defn.tools.modules == {}
        assert defn.dev.variables == {}

    def test_full_app(self):
        # Canonical nested shape (v2). Modules + capabilities live
        # under `tools:`, variables under `dev:`.
        raw = {
            "app": {
                "app_id": "my-agent",
                "name": "My Agent",
                "version": "2.0",
                "tags": ["coding"],
            },
            "dev": {"variables": {"workspace": "/tmp"}},
            "tools": {
                "modules": {
                    "database": {
                        "setup": [
                            {"action": "connect", "params": {"driver": "sqlite"}},
                        ],
                        "constraints": {
                            "allowed_actions": ["fetch_results"],
                            "blocked_actions": ["execute_query"],
                        },
                    }
                },
                "capabilities": {
                    "default_policy": "auto",
                    "max_risk_level": "high",
                    "grant": [{"module": "database", "actions": ["fetch_results"]}],
                    "deny": [
                        {
                            "module": "database",
                            "actions": ["execute_query"],
                            "reason": "Read-only",
                        }
                    ],
                },
            },
        }
        defn = AppDefinition.model_validate(raw)
        assert defn.app.version == "2.0"
        assert len(defn.tools.modules["database"].setup) == 1
        assert defn.tools.capabilities.default_policy == "auto"
        assert len(defn.tools.capabilities.deny) == 1

    def test_setup_step_defaults(self):
        step = SetupStep(action="connect")
        assert step.params == {}

    def test_module_block_defaults(self):
        block = ModuleBlock()
        assert block.setup == []
        assert block.constraints == {}

    def test_invalid_app_missing_id(self):
        with pytest.raises(Exception):
            AppDefinition.model_validate({"app": {"name": "No ID"}})


# ═══════════════════════════════════════════════════════════════════════
# Compiler
# ═══════════════════════════════════════════════════════════════════════


class TestCompiler:
    """Tests for compiler.py - full compilation pipeline."""

    def test_compile_minimal(self):
        """Minimal YAML with no modules compiles successfully."""
        db_mod = _make_module("database", ["connect", "fetch_results"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {"app": {"app_id": "test", "name": "Test"}}
        result = compiler.compile(_with_agent(raw))

        assert isinstance(result, CompiledApp)
        assert result.app_id == "test"
        # Only llm_provider (auto-created for the inline agent brain)
        assert "database" not in result.modules

    def test_compile_with_setup(self):
        """Setup steps are validated and compiled."""
        db_mod = _make_module("database", ["connect", "fetch_results"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "database": {
                    "setup": [
                        {
                            "action": "connect",
                            "params": {"driver": "sqlite", "database": "test.db"},
                        }
                    ]
                }
            },
        }
        result = compiler.compile(_with_agent(raw))
        assert len(result.modules["database"].setup_steps) == 1
        step = result.modules["database"].setup_steps[0]
        assert step.action == "connect"
        assert step.resolved_params["driver"] == "sqlite"

    def test_compile_with_variables(self):
        """Variables in params are resolved at compile time."""
        db_mod = _make_module("database", ["connect"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "variables": {"db_path": "/data/app.db"},
            "modules": {
                "database": {
                    "setup": [
                        {
                            "action": "connect",
                            "params": {"database": "{{db_path}}"},
                        }
                    ]
                }
            },
        }
        result = compiler.compile(_with_agent(raw))
        step = result.modules["database"].setup_steps[0]
        assert step.resolved_params["database"] == "/data/app.db"

    def test_compile_with_env_variables(self):
        """Environment variables in params are resolved."""
        db_mod = _make_module("database", ["connect"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "database": {
                    "setup": [
                        {
                            "action": "connect",
                            "params": {"host": "{{env.DB_HOST}}"},
                        }
                    ]
                }
            },
        }
        with patch.dict("os.environ", {"DB_HOST": "db.prod.internal"}):
            result = compiler.compile(_with_agent(raw))
        step = result.modules["database"].setup_steps[0]
        assert step.resolved_params["host"] == "db.prod.internal"

    def test_compile_with_constraints(self):
        """Module constraints are stored in the compiled output."""
        db_mod = _make_module("database", ["connect", "fetch_results"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "database": {
                    "constraints": {
                        "allowed_actions": ["fetch_results"],
                        "blocked_actions": ["connect"],
                    }
                }
            },
        }
        result = compiler.compile(_with_agent(raw))
        constraints = result.modules["database"].constraints
        assert constraints["allowed_actions"] == ["fetch_results"]
        assert constraints["blocked_actions"] == ["connect"]

    def test_compile_multiple_modules(self):
        """Multiple modules compile independently."""
        db_mod = _make_module("database", ["connect"])
        fs_mod = _make_module("filesystem", ["read_file", "write_file"])
        registry = _make_registry({"database": db_mod, "filesystem": fs_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "database": {
                    "setup": [{"action": "connect", "params": {"driver": "sqlite"}}],
                },
                "filesystem": {
                    "constraints": {"allowed_actions": ["read_file"]},
                },
            },
        }
        result = compiler.compile(_with_agent(raw))
        assert "database" in result.modules
        assert "filesystem" in result.modules
        assert len(result.modules["database"].setup_steps) == 1
        assert result.modules["filesystem"].constraints["allowed_actions"] == ["read_file"]

    def test_compile_with_config(self):
        """Static config block is resolved and stored."""
        perception_mod = _make_module("perception", ["capture_screen"])
        perception_mod.CONFIG_MODEL = None  # No validation in this test
        registry = _make_registry({"perception": perception_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "perception": {
                    "config": {
                        "enabled": False,
                        "capture_after": True,
                        "ocr_enabled": False,
                        "timeout_seconds": 10,
                        "actions": {
                            "browser.take_screenshot": {
                                "capture_after": True,
                                "ocr_enabled": True,
                            }
                        },
                    }
                }
            },
        }
        result = compiler.compile(_with_agent(raw))
        cfg = result.modules["perception"].config
        assert cfg["enabled"] is False
        assert cfg["timeout_seconds"] == 10
        assert cfg["actions"]["browser.take_screenshot"]["ocr_enabled"] is True

    def test_compile_config_with_variables(self):
        """Variables in config block are resolved."""
        mod = _make_module("llm", ["complete"])
        mod.CONFIG_MODEL = None
        registry = _make_registry({"llm": mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "variables": {"model_name": "claude-sonnet-4-20250514"},
            "modules": {
                "llm": {
                    "config": {
                        "provider": "anthropic",
                        "model": "{{model_name}}",
                        "max_tokens": 8192,
                    }
                }
            },
        }
        result = compiler.compile(_with_agent(raw))
        assert result.modules["llm"].config["model"] == "claude-sonnet-4-20250514"

    def test_compile_config_validated_against_config_model(self):
        """Config is validated against CONFIG_MODEL if present."""
        from pydantic import BaseModel as PydanticBaseModel

        class PerceptionConfig(PydanticBaseModel):
            enabled: bool = True
            timeout_seconds: int = 10

        mod = _make_module("perception", ["capture"])
        mod.CONFIG_MODEL = PerceptionConfig
        registry = _make_registry({"perception": mod})
        compiler = AppYAMLCompiler(registry)

        # Valid config
        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "perception": {
                    "config": {"enabled": False, "timeout_seconds": 5},
                }
            },
        }
        result = compiler.compile(_with_agent(raw))
        assert result.modules["perception"].config["enabled"] is False

        # Invalid config - bad type
        raw_bad = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "perception": {
                    "config": {"enabled": "not_a_bool_but_truthy", "timeout_seconds": "abc"},
                }
            },
        }
        with pytest.raises(AppCompilationError, match="config"):
            compiler.compile(raw_bad)


# ═══════════════════════════════════════════════════════════════════════
# Compiler Errors
# ═══════════════════════════════════════════════════════════════════════


class TestCompilerErrors:
    """Tests for error collection and reporting."""

    def test_unknown_module(self):
        """Referencing a non-loaded module raises ModuleNotFoundError."""
        registry = _make_registry({})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {"nonexistent": {"setup": []}},
        }
        with pytest.raises(ModuleNotFoundError, match="nonexistent"):
            compiler.compile(_with_agent(raw))

    def test_unknown_action(self):
        """Referencing a non-existent action is caught."""
        db_mod = _make_module("database", ["connect"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "database": {
                    "setup": [
                        {"action": "nonexistent_action", "params": {}},
                    ]
                }
            },
        }
        with pytest.raises(AppCompilationError, match="nonexistent_action"):
            compiler.compile(_with_agent(raw))

    def test_unresolved_variable(self):
        """Unresolved variables in params raise VariableResolutionError."""
        db_mod = _make_module("database", ["connect"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "database": {
                    "setup": [
                        {"action": "connect", "params": {"host": "{{undefined_var}}"}},
                    ]
                }
            },
        }
        with pytest.raises(VariableResolutionError):
            compiler.compile(_with_agent(raw))

    def test_invalid_yaml_schema(self):
        """Invalid YAML structure raises AppCompilationError."""
        registry = _make_registry({})
        compiler = AppYAMLCompiler(registry)

        with pytest.raises(AppCompilationError, match="schema"):
            compiler.compile({"not_app": "bad"})

    def test_multiple_errors_collected(self):
        """Multiple errors are collected into a single exception."""
        db_mod = _make_module("database", ["connect"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "database": {
                    "setup": [
                        {"action": "bad_action_1", "params": {}},
                        {"action": "bad_action_2", "params": {}},
                    ]
                }
            },
        }
        with pytest.raises(AppCompilationError) as exc_info:
            compiler.compile(_with_agent(raw))
        assert len(exc_info.value.errors) == 2

    def test_constraint_not_list_error(self):
        """Non-list value for allowed_actions raises error."""
        db_mod = _make_module("database", ["connect"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "database": {
                    "constraints": {"allowed_actions": "not_a_list"},
                }
            },
        }
        with pytest.raises(AppCompilationError, match="Expected a list"):
            compiler.compile(_with_agent(raw))

    def test_compile_file_not_found(self):
        """compile_file raises on missing file."""
        registry = _make_registry({})
        compiler = AppYAMLCompiler(registry)
        with pytest.raises(AppCompilationError, match="File not found"):
            compiler.compile_file(Path("/nonexistent/app.yaml"))


# ═══════════════════════════════════════════════════════════════════════
# Security Profile Build
# ═══════════════════════════════════════════════════════════════════════


class TestSecurityProfileBuild:
    """Tests for capabilities → SecurityProfile conversion."""

    def test_grants_set_auto(self):
        """Grant entries set action policy to 'auto'."""
        db_mod = _make_module("database", ["connect", "fetch_results"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        # Grant validation now requires the module to be declared in
        # the modules block - the alias pass lifts both `modules:` and
        # `capabilities:` into `tools:`.
        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {"database": {}},
            "capabilities": {
                "grant": [{"module": "database", "actions": ["fetch_results"]}],
            },
        }
        result = compiler.compile(_with_agent(raw))
        profile = result.security_profile
        grant = profile.module_grants["database"]
        assert grant.action_overrides["fetch_results"] == "auto"

    def test_denies_set_block(self):
        """Deny entries set action policy to 'block'."""
        db_mod = _make_module("database", ["connect", "execute_query"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "capabilities": {
                "deny": [
                    {
                        "module": "database",
                        "actions": ["execute_query"],
                        "reason": "Read-only",
                    }
                ],
            },
        }
        result = compiler.compile(_with_agent(raw))
        profile = result.security_profile
        grant = profile.module_grants["database"]
        assert grant.action_overrides["execute_query"] == "block"

    def test_deny_overrides_grant(self):
        """Deny takes priority over grant for the same action."""
        db_mod = _make_module("database", ["execute_query"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {"database": {}},
            "capabilities": {
                "grant": [{"module": "database", "actions": ["execute_query"]}],
                "deny": [{"module": "database", "actions": ["execute_query"]}],
            },
        }
        result = compiler.compile(_with_agent(raw))
        profile = result.security_profile
        # Deny is processed after grant, so it wins
        assert profile.module_grants["database"].action_overrides["execute_query"] == "block"

    def test_allowed_actions_constraint_blocks_others(self):
        """allowed_actions constraint blocks all actions not in the list."""
        db_mod = _make_module("database", ["connect", "fetch_results", "execute_query"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "database": {
                    "constraints": {"allowed_actions": ["fetch_results"]},
                }
            },
        }
        result = compiler.compile(_with_agent(raw))
        profile = result.security_profile
        overrides = profile.module_grants["database"].action_overrides
        assert overrides["fetch_results"] == "auto"
        assert overrides["connect"] == "block"
        assert overrides["execute_query"] == "block"

    def test_blocked_actions_constraint(self):
        """blocked_actions constraint blocks specific actions."""
        db_mod = _make_module("database", ["connect", "execute_query"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "database": {
                    "constraints": {"blocked_actions": ["execute_query"]},
                }
            },
        }
        result = compiler.compile(_with_agent(raw))
        overrides = result.security_profile.module_grants["database"].action_overrides
        assert overrides["execute_query"] == "block"

    def test_profile_metadata(self):
        """Security profile carries app metadata."""
        db_mod = _make_module("database", ["connect"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "my-agent", "name": "Agent"},
            "capabilities": {
                "default_policy": "auto",
                "max_risk_level": "high",
            },
        }
        result = compiler.compile(_with_agent(raw))
        assert result.security_profile.app_id == "my-agent"
        assert result.security_profile.default_policy == "auto"
        assert result.security_profile.max_risk_level == "high"

    def test_granted_permissions(self):
        """Grants produce permission strings in module:action format."""
        db_mod = _make_module("database", ["connect", "fetch_results"])
        registry = _make_registry({"database": db_mod})
        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "modules": {"database": {}},
            "capabilities": {
                "grant": [
                    {"module": "database", "actions": ["connect", "fetch_results"]},
                ],
            },
        }
        result = compiler.compile(_with_agent(raw))
        perms = result.security_profile.granted_permissions
        assert "database:connect" in perms
        assert "database:fetch_results" in perms


# ═══════════════════════════════════════════════════════════════════════
# Bootstrapper
# ═══════════════════════════════════════════════════════════════════════


class TestBootstrapper:
    """Tests for bootstrapper.py - setup action execution."""

    @pytest.mark.asyncio
    async def test_bootstrap_executes_steps(self):
        """Setup steps are executed via module.execute()."""
        db_mod = _make_module("database", ["connect"])
        registry = _make_registry({"database": db_mod})

        compiled = CompiledApp(
            meta=AppMeta(app_id="test", name="Test"),
            modules={
                "database": CompiledModuleConfig(
                    module_id="database",
                    setup_steps=[
                        CompiledSetupStep(
                            module_id="database",
                            action="connect",
                            resolved_params={"driver": "sqlite"},
                        )
                    ],
                )
            },
        )

        bootstrapper = AppBootstrapper(registry)
        result = await bootstrapper.bootstrap(compiled)

        assert result.success
        assert result.step_count == 1
        assert result.setup_results[0].success
        db_mod.execute.assert_called_once_with("connect", {"driver": "sqlite"})

    @pytest.mark.asyncio
    async def test_bootstrap_multiple_steps(self):
        """Multiple setup steps are executed in order."""
        db_mod = _make_module("database", ["connect", "set_policy"])
        registry = _make_registry({"database": db_mod})

        compiled = CompiledApp(
            meta=AppMeta(app_id="test", name="Test"),
            modules={
                "database": CompiledModuleConfig(
                    module_id="database",
                    setup_steps=[
                        CompiledSetupStep(
                            module_id="database",
                            action="connect",
                            resolved_params={"driver": "sqlite"},
                        ),
                        CompiledSetupStep(
                            module_id="database",
                            action="set_policy",
                            resolved_params={"policy": "readonly"},
                        ),
                    ],
                )
            },
        )

        bootstrapper = AppBootstrapper(registry)
        result = await bootstrapper.bootstrap(compiled)

        assert result.success
        assert result.step_count == 2
        assert db_mod.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_bootstrap_failure_continues(self):
        """Failed steps don't stop bootstrap by default."""
        db_mod = _make_module("database", ["connect", "set_policy"])

        call_count = 0

        async def mock_execute(action, params):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if action == "connect":
                result.success = False
                result.error = "Connection refused"
                result.data = None
            else:
                result.success = True
                result.data = {}
                result.error = None
            return result

        db_mod.execute = AsyncMock(side_effect=mock_execute)
        registry = _make_registry({"database": db_mod})

        compiled = CompiledApp(
            meta=AppMeta(app_id="test", name="Test"),
            modules={
                "database": CompiledModuleConfig(
                    module_id="database",
                    setup_steps=[
                        CompiledSetupStep(
                            module_id="database",
                            action="connect",
                            resolved_params={},
                        ),
                        CompiledSetupStep(
                            module_id="database",
                            action="set_policy",
                            resolved_params={"policy": "readonly"},
                        ),
                    ],
                )
            },
        )

        bootstrapper = AppBootstrapper(registry)
        result = await bootstrapper.bootstrap(compiled)

        assert not result.success
        assert call_count == 2  # both steps executed
        assert len(result.failed_steps) == 1
        assert result.failed_steps[0].action == "connect"

    @pytest.mark.asyncio
    async def test_bootstrap_stop_on_error(self):
        """With stop_on_error, bootstrap stops at first failure."""
        db_mod = _make_module("database", ["connect", "set_policy"])

        async def mock_execute(action, params):
            result = MagicMock()
            result.success = False
            result.error = "fail"
            result.data = None
            return result

        db_mod.execute = AsyncMock(side_effect=mock_execute)
        registry = _make_registry({"database": db_mod})

        compiled = CompiledApp(
            meta=AppMeta(app_id="test", name="Test"),
            modules={
                "database": CompiledModuleConfig(
                    module_id="database",
                    setup_steps=[
                        CompiledSetupStep(
                            module_id="database",
                            action="connect",
                            resolved_params={},
                        ),
                        CompiledSetupStep(
                            module_id="database",
                            action="set_policy",
                            resolved_params={},
                        ),
                    ],
                )
            },
        )

        bootstrapper = AppBootstrapper(registry, stop_on_error=True)
        result = await bootstrapper.bootstrap(compiled)

        assert not result.success
        assert result.step_count == 1  # stopped after first failure

    @pytest.mark.asyncio
    async def test_bootstrap_exception_handling(self):
        """Exceptions in execute() are caught and recorded."""
        db_mod = _make_module("database", ["connect"])
        db_mod.execute = AsyncMock(side_effect=RuntimeError("boom"))
        registry = _make_registry({"database": db_mod})

        compiled = CompiledApp(
            meta=AppMeta(app_id="test", name="Test"),
            modules={
                "database": CompiledModuleConfig(
                    module_id="database",
                    setup_steps=[
                        CompiledSetupStep(
                            module_id="database",
                            action="connect",
                            resolved_params={},
                        )
                    ],
                )
            },
        )

        bootstrapper = AppBootstrapper(registry)
        result = await bootstrapper.bootstrap(compiled)

        assert not result.success
        assert result.failed_steps[0].error == "boom"

    @pytest.mark.asyncio
    async def test_bootstrap_stores_constraints(self):
        """Module constraints are stored in the result."""
        db_mod = _make_module("database", ["connect"])
        registry = _make_registry({"database": db_mod})

        compiled = CompiledApp(
            meta=AppMeta(app_id="test", name="Test"),
            modules={
                "database": CompiledModuleConfig(
                    module_id="database",
                    constraints={"allowed_actions": ["fetch_results"], "max_rows": 5000},
                )
            },
        )

        bootstrapper = AppBootstrapper(registry)
        result = await bootstrapper.bootstrap(compiled)

        assert result.constraints["database"]["max_rows"] == 5000

    @pytest.mark.asyncio
    async def test_bootstrap_applies_config(self):
        """Config block is pushed via on_config_update() before setup steps."""
        perception_mod = _make_module("perception", ["capture"])
        perception_mod.on_config_update = AsyncMock()
        registry = _make_registry({"perception": perception_mod})

        compiled = CompiledApp(
            meta=AppMeta(app_id="test", name="Test"),
            modules={
                "perception": CompiledModuleConfig(
                    module_id="perception",
                    config={
                        "enabled": False,
                        "capture_after": True,
                        "timeout_seconds": 10,
                    },
                    setup_steps=[
                        CompiledSetupStep(
                            module_id="perception",
                            action="capture",
                            resolved_params={},
                        )
                    ],
                )
            },
        )

        bootstrapper = AppBootstrapper(registry)
        result = await bootstrapper.bootstrap(compiled)

        assert result.success
        # on_config_update called BEFORE execute
        perception_mod.on_config_update.assert_called_once_with({
            "enabled": False,
            "capture_after": True,
            "timeout_seconds": 10,
        })
        perception_mod.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_bootstrap_config_error_recorded(self):
        """Config application failure is recorded in errors."""
        mod = _make_module("broken", ["something"])
        mod.on_config_update = AsyncMock(side_effect=ValueError("bad config"))
        registry = _make_registry({"broken": mod})

        compiled = CompiledApp(
            meta=AppMeta(app_id="test", name="Test"),
            modules={
                "broken": CompiledModuleConfig(
                    module_id="broken",
                    config={"bad_key": "bad_value"},
                )
            },
        )

        bootstrapper = AppBootstrapper(registry)
        result = await bootstrapper.bootstrap(compiled)

        assert not result.success
        assert any("config" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_bootstrap_empty_config_skips_update(self):
        """Empty config dict does NOT call on_config_update."""
        mod = _make_module("simple", ["act"])
        mod.on_config_update = AsyncMock()
        registry = _make_registry({"simple": mod})

        compiled = CompiledApp(
            meta=AppMeta(app_id="test", name="Test"),
            modules={
                "simple": CompiledModuleConfig(
                    module_id="simple",
                    config={},  # empty
                )
            },
        )

        bootstrapper = AppBootstrapper(registry)
        await bootstrapper.bootstrap(compiled)

        mod.on_config_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_bootstrap_summary(self):
        """summary() returns a compact dict."""
        db_mod = _make_module("database", ["connect"])
        registry = _make_registry({"database": db_mod})

        compiled = CompiledApp(
            meta=AppMeta(app_id="my-app", name="Test"),
            modules={
                "database": CompiledModuleConfig(
                    module_id="database",
                    setup_steps=[
                        CompiledSetupStep(
                            module_id="database",
                            action="connect",
                            resolved_params={},
                        )
                    ],
                    constraints={"max_rows": 100},
                )
            },
        )

        bootstrapper = AppBootstrapper(registry)
        result = await bootstrapper.bootstrap(compiled)
        summary = result.summary()

        assert summary["app_id"] == "my-app"
        assert summary["success"] is True
        assert summary["total_steps"] == 1


# ═══════════════════════════════════════════════════════════════════════
# End-to-End (YAML file → compile → bootstrap)
# ═══════════════════════════════════════════════════════════════════════


class TestEndToEnd:
    """Full pipeline: YAML file → compile → bootstrap."""

    @pytest.mark.asyncio
    async def test_yaml_file_roundtrip(self, tmp_path):
        """Write a YAML file, compile it, and bootstrap it."""
        yaml_content = textwrap.dedent("""\
            app:
              app_id: e2e-test
              name: "E2E Test App"
              version: "1.0"

            variables:
              db_driver: sqlite

            modules:
              database:
                setup:
                  - action: connect
                    params:
                      connection_id: main
                      driver: "{{db_driver}}"
                      database: ":memory:"
                  - action: connect
                    params:
                      connection_id: analytics
                      driver: sqlite
                      database: "/data/analytics.db"
                constraints:
                  allowed_actions:
                    - connect
                    - fetch_results
                    - list_tables
                  blocked_actions:
                    - execute_query

            agents:
              - id: default
                brain:
                  model: test-model

            capabilities:
              default_policy: approve
              max_risk_level: medium
              grant:
                - module: database
                  actions:
                    - fetch_results
                    - list_tables
              deny:
                - module: database
                  actions:
                    - execute_query
                  reason: "Read-only for this app"
        """)

        yaml_file = tmp_path / "app.yaml"
        yaml_file.write_text(yaml_content)

        # Setup mock modules
        db_mod = _make_module(
            "database",
            ["connect", "fetch_results", "list_tables", "execute_query", "disconnect"],
        )
        llm_mod = _make_module("llm_provider", [])
        registry = _make_registry({"database": db_mod, "llm_provider": llm_mod})

        # Compile
        compiler = AppYAMLCompiler(registry)
        compiled = compiler.compile_file(yaml_file)

        assert compiled.app_id == "e2e-test"
        assert compiled.source_path == yaml_file
        assert len(compiled.modules["database"].setup_steps) == 2

        # Verify variable resolution
        step0 = compiled.modules["database"].setup_steps[0]
        assert step0.resolved_params["driver"] == "sqlite"

        # Verify security profile
        profile = compiled.security_profile
        assert profile.app_id == "e2e-test"
        overrides = profile.module_grants["database"].action_overrides
        assert overrides["execute_query"] == "block"

        # Bootstrap
        bootstrapper = AppBootstrapper(registry)
        result = await bootstrapper.bootstrap(compiled)

        assert result.success
        assert result.step_count == 2
        assert db_mod.execute.call_count == 2

        # Verify execution order
        calls = db_mod.execute.call_args_list
        assert calls[0].args == ("connect", {"connection_id": "main", "driver": "sqlite", "database": ":memory:"})
        assert calls[1].args == ("connect", {"connection_id": "analytics", "driver": "sqlite", "database": "/data/analytics.db"})

    @pytest.mark.asyncio
    async def test_empty_modules_section(self, tmp_path):
        """An app with no modules compiles and bootstraps successfully."""
        yaml_content = textwrap.dedent("""\
            app:
              app_id: empty-app
              name: "Empty App"

            agents:
              - id: default
                brain:
                  model: test-model
        """)
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text(yaml_content)

        llm_mod = _make_module("llm_provider", [])
        registry = _make_registry({"llm_provider": llm_mod})
        compiler = AppYAMLCompiler(registry)
        compiled = compiler.compile_file(yaml_file)

        assert "database" not in compiled.modules

        bootstrapper = AppBootstrapper(registry)
        result = await bootstrapper.bootstrap(compiled)
        assert result.success
        assert result.step_count == 0

    @pytest.mark.asyncio
    async def test_multi_module_yaml(self, tmp_path):
        """YAML with multiple modules compiles and bootstraps."""
        yaml_content = textwrap.dedent("""\
            app:
              app_id: multi
              name: "Multi Module App"

            agents:
              - id: default
                brain:
                  model: test-model

            modules:
              database:
                setup:
                  - action: connect
                    params:
                      driver: sqlite
              filesystem:
                setup:
                  - action: set_root
                    params:
                      path: /data
                constraints:
                  allowed_actions:
                    - read_file
                    - list_directory
        """)
        yaml_file = tmp_path / "multi.yaml"
        yaml_file.write_text(yaml_content)

        db_mod = _make_module("database", ["connect", "fetch_results"])
        fs_mod = _make_module("filesystem", ["set_root", "read_file", "write_file", "list_directory"])
        llm_mod = _make_module("llm_provider", [])
        registry = _make_registry({"database": db_mod, "filesystem": fs_mod, "llm_provider": llm_mod})

        compiler = AppYAMLCompiler(registry)
        compiled = compiler.compile_file(yaml_file)

        assert {"database", "filesystem"}.issubset(set(compiled.module_ids))

        # Filesystem: write_file should be blocked (not in allowed_actions)
        fs_overrides = compiled.security_profile.module_grants["filesystem"].action_overrides
        assert fs_overrides.get("write_file") == "block"
        assert fs_overrides.get("read_file") == "auto"

        # Bootstrap
        bootstrapper = AppBootstrapper(registry)
        result = await bootstrapper.bootstrap(compiled)
        assert result.success
        assert result.step_count == 2  # 1 db + 1 fs


# ═══════════════════════════════════════════════════════════════════════
# Error types
# ═══════════════════════════════════════════════════════════════════════


class TestErrors:
    """Test error formatting and hierarchy."""

    def test_compilation_error_message(self):
        exc = AppCompilationError(["error 1", "error 2", "error 3"])
        assert "3 error(s)" in str(exc)
        assert "error 1" in str(exc)

    def test_compilation_error_truncation(self):
        errors = [f"error {i}" for i in range(10)]
        exc = AppCompilationError(errors)
        assert "and 5 more" in str(exc)

    def test_bootstrap_error(self):
        exc = AppBootstrapError("step failed", step_errors=[{"step": "connect"}])
        assert exc.step_errors == [{"step": "connect"}]

    def test_error_inheritance(self):
        assert issubclass(VariableResolutionError, AppCompilationError)
        assert issubclass(ModuleNotFoundError, AppCompilationError)
        assert issubclass(ActionNotFoundError, AppCompilationError)
        assert issubclass(ParamsValidationError, AppCompilationError)
        assert issubclass(ConstraintValidationError, AppCompilationError)
