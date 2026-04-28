"""Tests for the CLI layer: app commands and config loading.

Covers:
    - ``digitorn app validate`` (success, errors, missing file, output table)
    - ``digitorn app schema`` (module found, module not found)
    - Config: Settings defaults, RuntimeConfig, ServerConfig, YAML loading
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from digitorn.core.cli.app import app_cli
from digitorn.core.config import (
    AppConfig,
    DatabaseConfig,
    LoggingConfig,
    ModulesConfig,
    RuntimeConfig,
    ServerConfig,
    Settings,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers - lightweight fakes for CompiledApp and friends
# ---------------------------------------------------------------------------


@dataclass
class _FakeAppMeta:
    app_id: str = "test-app"
    name: str = "Test App"
    version: str = "1.0"


@dataclass
class _FakeModuleConfig:
    module_id: str = "hello"
    config: dict[str, Any] = field(default_factory=dict)
    setup_steps: list[Any] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    middleware: list[Any] = field(default_factory=list)


@dataclass
class _FakeSecurityProfile:
    default_policy: str = "auto"
    max_risk_level: str = "medium"


@dataclass
class _FakeCompiledApp:
    meta: _FakeAppMeta = field(default_factory=_FakeAppMeta)
    modules: dict[str, _FakeModuleConfig] = field(default_factory=dict)
    security_profile: _FakeSecurityProfile | None = None

    @property
    def module_ids(self) -> list[str]:
        return list(self.modules.keys())


@dataclass
class _FakeParamSpec:
    name: str = "name"
    description: str = "A name"


@dataclass
class _FakeActionSpec:
    name: str = "greet"
    risk_level: str = "low"
    params: list[_FakeParamSpec] = field(default_factory=list)


@dataclass
class _FakeConstraintSpec:
    name: str = "paths"
    type: str = "list[str]"
    scope: str = "universal"
    description: str = "Allowed paths"


@dataclass
class _FakeManifest:
    actions: list[_FakeActionSpec] = field(default_factory=list)
    supported_constraints: list[_FakeConstraintSpec] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------


def _mock_settings():
    s = MagicMock()
    s.modules.enabled = []
    s.modules.disabled = []
    s.modules.load_all = True
    return s


def _run_validate(compiled, tmp_path, filename="app.yaml"):
    """Invoke ``app validate <path>`` with a mocked compiler."""
    yaml_file = tmp_path / filename
    yaml_file.write_text("app:\n  app_id: test\n  name: Test\n")

    mock_compiler_cls = MagicMock()
    mock_compiler_cls.return_value.compile_file.return_value = compiled

    with (
        patch("digitorn.core.config.Settings.load", return_value=_mock_settings()),
        patch("digitorn.modules.registry.ModuleRegistry", return_value=MagicMock()),
        patch("digitorn.core.loader.load_modules"),
        patch("digitorn.core.app.AppYAMLCompiler", mock_compiler_cls),
        patch("digitorn.core.app.compiler.AppYAMLCompiler", mock_compiler_cls),
    ):
        return runner.invoke(app_cli, ["validate", str(yaml_file)])


def _run_validate_error(errors: list[str], tmp_path):
    """Invoke ``app validate`` where the compiler raises."""
    from digitorn.core.app.errors import AppCompilationError

    yaml_file = tmp_path / "bad.yaml"
    yaml_file.write_text("app:\n  app_id: x\n  name: X\n")

    mock_compiler_cls = MagicMock()
    mock_compiler_cls.return_value.compile_file.side_effect = AppCompilationError(
        errors, source=str(yaml_file),
    )

    with (
        patch("digitorn.core.config.Settings.load", return_value=_mock_settings()),
        patch("digitorn.modules.registry.ModuleRegistry", return_value=MagicMock()),
        patch("digitorn.core.loader.load_modules"),
        patch("digitorn.core.app.AppYAMLCompiler", mock_compiler_cls),
        patch("digitorn.core.app.compiler.AppYAMLCompiler", mock_compiler_cls),
    ):
        return runner.invoke(app_cli, ["validate", str(yaml_file)])


def _run_schema(mock_registry, module_id="hello"):
    """Invoke ``app schema <module_id>`` with a mocked registry."""
    with (
        patch("digitorn.core.config.Settings.load", return_value=_mock_settings()),
        patch("digitorn.modules.registry.ModuleRegistry", return_value=mock_registry),
        patch("digitorn.core.loader.load_modules"),
        patch("digitorn.core.loader.discover_modules", return_value={}),
    ):
        return runner.invoke(app_cli, ["schema", module_id])


# ---------------------------------------------------------------------------
# Tests: validate command - happy path
# ---------------------------------------------------------------------------


class TestValidateSuccess:
    """validate command: successful compilation."""

    def test_exit_code_zero(self, tmp_path):
        compiled = _FakeCompiledApp(modules={"hello": _FakeModuleConfig()})
        result = _run_validate(compiled, tmp_path)
        assert result.exit_code == 0

    def test_output_contains_validation_ok(self, tmp_path):
        compiled = _FakeCompiledApp(modules={"hello": _FakeModuleConfig()})
        result = _run_validate(compiled, tmp_path)
        assert "Validation OK" in result.output

    def test_output_contains_app_id(self, tmp_path):
        compiled = _FakeCompiledApp(
            meta=_FakeAppMeta(app_id="my-app", name="My App"),
            modules={"hello": _FakeModuleConfig()},
        )
        result = _run_validate(compiled, tmp_path)
        assert "my-app" in result.output

    def test_output_contains_app_name_in_title(self, tmp_path):
        compiled = _FakeCompiledApp(
            meta=_FakeAppMeta(name="My Cool App"),
            modules={"hello": _FakeModuleConfig()},
        )
        result = _run_validate(compiled, tmp_path)
        assert "My Cool App" in result.output

    def test_output_contains_module_name(self, tmp_path):
        compiled = _FakeCompiledApp(
            modules={"database": _FakeModuleConfig(module_id="database")},
        )
        result = _run_validate(compiled, tmp_path)
        assert "database" in result.output

    def test_security_profile_shown(self, tmp_path):
        compiled = _FakeCompiledApp(
            modules={"hello": _FakeModuleConfig()},
            security_profile=_FakeSecurityProfile(
                default_policy="block", max_risk_level="low",
            ),
        )
        result = _run_validate(compiled, tmp_path)
        assert "block" in result.output
        assert "low" in result.output

    def test_no_security_profile(self, tmp_path):
        compiled = _FakeCompiledApp(
            modules={"hello": _FakeModuleConfig()},
            security_profile=None,
        )
        result = _run_validate(compiled, tmp_path)
        assert "no capabilities block" in result.output

    def test_multiple_modules(self, tmp_path):
        compiled = _FakeCompiledApp(
            modules={
                "hello": _FakeModuleConfig(module_id="hello"),
                "database": _FakeModuleConfig(module_id="database"),
            },
        )
        result = _run_validate(compiled, tmp_path)
        assert "hello" in result.output
        assert "database" in result.output

    def test_constrained_modules_listed(self, tmp_path):
        compiled = _FakeCompiledApp(
            modules={
                "filesystem": _FakeModuleConfig(
                    module_id="filesystem",
                    constraints={"paths": ["/tmp"]},
                ),
                "db": _FakeModuleConfig(module_id="db"),
            },
        )
        result = _run_validate(compiled, tmp_path)
        assert "filesystem" in result.output

    def test_setup_steps_counted(self, tmp_path):
        compiled = _FakeCompiledApp(
            modules={
                "hello": _FakeModuleConfig(setup_steps=["s1", "s2", "s3"]),
            },
        )
        result = _run_validate(compiled, tmp_path)
        assert "3" in result.output

    def test_version_shown(self, tmp_path):
        compiled = _FakeCompiledApp(
            meta=_FakeAppMeta(version="2.5"),
            modules={"hello": _FakeModuleConfig()},
        )
        result = _run_validate(compiled, tmp_path)
        assert "2.5" in result.output


# ---------------------------------------------------------------------------
# Tests: validate command - error cases
# ---------------------------------------------------------------------------


class TestValidateErrors:
    """validate command: compilation failures."""

    def test_compilation_error_exit_code(self, tmp_path):
        result = _run_validate_error(["err"], tmp_path)
        assert result.exit_code == 1

    def test_compilation_error_shows_count(self, tmp_path):
        result = _run_validate_error(
            ["Unknown module 'foo'", "Missing param 'bar'"], tmp_path,
        )
        assert "2 error" in result.output

    def test_compilation_error_shows_messages(self, tmp_path):
        result = _run_validate_error(
            ["Module 'ghost' not found", "Invalid param type"], tmp_path,
        )
        assert "Module 'ghost' not found" in result.output
        assert "Invalid param type" in result.output

    def test_single_error_count(self, tmp_path):
        result = _run_validate_error(["One error"], tmp_path)
        assert "1 error" in result.output

    def test_missing_path_argument(self):
        """No path argument -> typer shows usage error."""
        result = runner.invoke(app_cli, ["validate"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Tests: schema command
# ---------------------------------------------------------------------------


class TestSchemaCommand:
    """schema command: show module YAML schema."""

    def _make_registry(self, module=None, available=True, available_list=None):
        mock = MagicMock()
        mock.is_available.return_value = available
        mock.list_available.return_value = available_list or []
        if module is not None:
            mock.get.return_value = module
        return mock

    def _make_module(self, manifest):
        m = MagicMock()
        m.get_manifest.return_value = manifest
        m.CONFIG_MODEL = None
        return m

    def test_module_found_exit_zero(self):
        module = self._make_module(_FakeManifest(
            actions=[_FakeActionSpec(name="greet")],
        ))
        result = _run_schema(self._make_registry(module=module), "hello")
        assert result.exit_code == 0

    def test_action_name_displayed(self):
        module = self._make_module(_FakeManifest(
            actions=[_FakeActionSpec(name="greet")],
        ))
        result = _run_schema(self._make_registry(module=module), "hello")
        assert "greet" in result.output

    def test_module_not_found_exit_one(self):
        reg = self._make_registry(available=False, available_list=["hello"])
        result = _run_schema(reg, "nonexistent")
        assert result.exit_code == 1

    def test_module_not_found_message(self):
        reg = self._make_registry(available=False, available_list=["hello"])
        result = _run_schema(reg, "nonexistent")
        assert "not available" in result.output

    def test_available_modules_listed_on_not_found(self):
        reg = self._make_registry(
            available=False, available_list=["hello", "database"],
        )
        result = _run_schema(reg, "nonexistent")
        assert "hello" in result.output
        assert "database" in result.output

    def test_constraints_displayed(self):
        module = self._make_module(_FakeManifest(
            actions=[_FakeActionSpec(name="read")],
            supported_constraints=[
                _FakeConstraintSpec(name="paths", description="Allowed paths"),
            ],
        ))
        result = _run_schema(self._make_registry(module=module), "filesystem")
        assert "paths" in result.output

    def test_risk_level_displayed(self):
        module = self._make_module(_FakeManifest(
            actions=[_FakeActionSpec(name="drop", risk_level="high")],
        ))
        result = _run_schema(self._make_registry(module=module), "db")
        assert "high" in result.output

    def test_no_argument_shows_error(self):
        result = runner.invoke(app_cli, ["schema"])
        assert result.exit_code != 0

    def test_multiple_actions_all_shown(self):
        module = self._make_module(_FakeManifest(
            actions=[
                _FakeActionSpec(name="alpha", risk_level="low"),
                _FakeActionSpec(name="beta", risk_level="medium"),
                _FakeActionSpec(name="gamma", risk_level="high"),
            ],
        ))
        result = _run_schema(self._make_registry(module=module), "mymod")
        assert "alpha" in result.output
        assert "beta" in result.output
        assert "gamma" in result.output

    def test_param_names_shown(self):
        module = self._make_module(_FakeManifest(
            actions=[
                _FakeActionSpec(
                    name="connect",
                    params=[_FakeParamSpec(name="host"), _FakeParamSpec(name="port")],
                ),
            ],
        ))
        result = _run_schema(self._make_registry(module=module), "db")
        assert "host" in result.output
        assert "port" in result.output


# ---------------------------------------------------------------------------
# Tests: Settings / Config defaults
# ---------------------------------------------------------------------------


class TestSettingsDefaults:
    """Default values for Settings and sub-configs."""

    def test_server_host(self):
        assert ServerConfig().host == "127.0.0.1"

    def test_server_port(self):
        assert ServerConfig().port == 8000

    def test_server_workers(self):
        assert ServerConfig().workers == 1

    def test_server_reload_off(self):
        assert ServerConfig().reload is False

    def test_server_rate_limit(self):
        assert ServerConfig().rate_limit_rpm == 60

    def test_server_kv_backend_none(self):
        assert ServerConfig().kv_backend is None

    def test_server_cors_origins(self):
        origins = ServerConfig().cors_origins
        assert "http://localhost" in origins
        assert "http://127.0.0.1" in origins

    def test_database_url_sqlite(self):
        assert "sqlite" in DatabaseConfig().url

    def test_database_echo_off(self):
        assert DatabaseConfig().echo is False

    def test_database_pool_size(self):
        assert DatabaseConfig().pool_size == 5

    def test_modules_paths(self):
        assert ModulesConfig().paths == ["modules"]

    def test_modules_enabled_empty(self):
        assert ModulesConfig().enabled == []

    def test_modules_load_all(self):
        assert ModulesConfig().load_all is True

    def test_app_config_no_yaml(self):
        assert AppConfig().yaml_path is None

    def test_app_config_stop_on_error(self):
        assert AppConfig().stop_on_error is False

    def test_logging_level(self):
        assert LoggingConfig().level == "info"

    def test_logging_format(self):
        assert LoggingConfig().format == "console"

    def test_settings_sections_present(self):
        s = Settings()
        assert isinstance(s.server, ServerConfig)
        assert isinstance(s.database, DatabaseConfig)
        assert isinstance(s.modules, ModulesConfig)
        assert isinstance(s.logging, LoggingConfig)
        assert isinstance(s.app, AppConfig)
        assert isinstance(s.runtime, RuntimeConfig)


class TestRuntimeConfig:
    """RuntimeConfig field defaults and validation."""

    def test_max_consecutive_failures(self):
        assert RuntimeConfig().max_consecutive_failures == 2

    def test_max_repeat_window(self):
        assert RuntimeConfig().max_repeat_window == 6

    def test_tool_timeout(self):
        assert RuntimeConfig().tool_timeout == 120.0

    def test_context_pressure_threshold(self):
        assert RuntimeConfig().context_pressure_threshold == 0.75

    def test_specialist_context_window(self):
        assert RuntimeConfig().specialist_context_window == 50_000

    def test_watch_poll_interval(self):
        assert RuntimeConfig().watch_poll_interval == 5

    def test_port_below_minimum_rejected(self):
        with pytest.raises(Exception):
            ServerConfig(port=80)

    def test_port_above_maximum_rejected(self):
        with pytest.raises(Exception):
            ServerConfig(port=70000)

    def test_workers_zero_rejected(self):
        with pytest.raises(Exception):
            ServerConfig(workers=0)

    def test_workers_above_max_rejected(self):
        with pytest.raises(Exception):
            ServerConfig(workers=100)

    def test_rate_limit_zero_rejected(self):
        with pytest.raises(Exception):
            ServerConfig(rate_limit_rpm=0)

    def test_custom_runtime_values(self):
        cfg = RuntimeConfig(
            max_consecutive_failures=5,
            tool_timeout=60.0,
            context_pressure_threshold=0.5,
        )
        assert cfg.max_consecutive_failures == 5
        assert cfg.tool_timeout == 60.0
        assert cfg.context_pressure_threshold == 0.5


class TestSettingsLoad:
    """Settings.load() with YAML config files."""

    def test_load_from_yaml(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "server": {"port": 9090, "host": "0.0.0.0"},
            "logging": {"level": "debug"},
        }))
        settings = Settings.load(config_file=config_file)
        assert settings.server.port == 9090
        assert settings.server.host == "0.0.0.0"
        assert settings.logging.level == "debug"

    def test_load_nonexistent_file_uses_defaults(self, tmp_path):
        settings = Settings.load(config_file=tmp_path / "nope.yaml")
        assert settings.server.port == 8000

    def test_load_empty_yaml_uses_defaults(self, tmp_path):
        cfg = tmp_path / "empty.yaml"
        cfg.write_text("")
        settings = Settings.load(config_file=cfg)
        assert settings.server.port == 8000

    def test_load_partial_yaml_merges(self, tmp_path):
        cfg = tmp_path / "partial.yaml"
        cfg.write_text(yaml.dump({"server": {"port": 3000}}))
        settings = Settings.load(config_file=cfg)
        assert settings.server.port == 3000
        assert settings.server.host == "127.0.0.1"

    def test_override_settings_singleton(self):
        from digitorn.core.config import get_settings, override_settings

        custom = Settings(server=ServerConfig(port=1234))
        override_settings(custom)
        try:
            assert get_settings().server.port == 1234
        finally:
            override_settings(None)
