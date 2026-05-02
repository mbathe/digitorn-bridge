"""Phase 1 schema/compiler hardening tests.

Each test corresponds to one of the 9 gaps closed in Phase 1
(see ``.logs/schema-migration/phase-1-audit.md``).

Test class layout mirrors the audit categories:
    A. Brain / provider validation
    B. Tool reference validation
    C. Cross-references
    D. Mode coherence
    F. Resource bounds
    Regression. Real builtin apps + web YAMLs still compile.

These tests are written FIRST (TDD red phase). They drive the schema
and compiler changes that follow.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from digitorn.core.app.compiler import AppYAMLCompiler, CompiledApp
from digitorn.core.app.errors import AppCompilationError


# ═══════════════════════════════════════════════════════════════════════
# Fixtures - copied from tests/test_app_compiler.py because they are
# inline there and not exported. Keeping the duplication minimal so we
# do not create a circular dependency between the two files.
# ═══════════════════════════════════════════════════════════════════════


def _make_action_entry(name: str, params_model: type | None = None):
    entry = MagicMock()
    entry.params_model = params_model
    entry.spec = MagicMock()
    entry.spec.name = name
    entry.spec.params = []
    return entry


def _make_manifest(action_names: list[str], constraints: list | None = None):
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
    module = MagicMock()
    module.MODULE_ID = module_id
    manifest = _make_manifest(action_names, constraints)
    module.get_manifest.return_value = manifest
    registry = {n: _make_action_entry(n) for n in action_names}
    module._action_registry = registry
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


def _base_app(extra: dict | None = None) -> dict:
    """Minimal valid app, override-able with `extra`."""
    raw = {
        "app": {"app_id": "test-app", "name": "Test"},
        "agents": [
            {
                "id": "main",
                "brain": {"provider": "ollama", "model": "llama3.2"},
            }
        ],
    }
    if extra:
        raw.update(extra)
    return raw


def _compiler(modules: dict[str, Any] | None = None) -> AppYAMLCompiler:
    return AppYAMLCompiler(_make_registry(modules or {}))


def _err_text(exc_info: pytest.ExceptionInfo[AppCompilationError]) -> str:
    return "\n".join(str(e) for e in exc_info.value.errors)


# ═══════════════════════════════════════════════════════════════════════
# Category A - Brain / provider validation
# ═══════════════════════════════════════════════════════════════════════


class TestBrainCredentialRequired:
    """Gap A2 - cloud providers must have a credential source.

    Local providers (ollama, lm_studio, vllm) are exempt.
    The 'claude-code' sentinel for anthropic is exempt.
    A localhost base_url override is exempt.
    """

    def test_brain_cloud_provider_without_credential_caught(self):
        """Anthropic without api_key, credential, or provider_id must fail."""
        raw = _base_app()
        raw["agents"] = [{
            "id": "main",
            "brain": {"provider": "anthropic", "model": "claude-haiku-4-5"},
        }]
        with pytest.raises(AppCompilationError) as exc_info:
            _compiler().compile(raw)
        msg = _err_text(exc_info)
        assert "anthropic" in msg.lower()
        assert "credential" in msg.lower() or "api_key" in msg.lower()

    def test_brain_local_provider_ollama_compiles_without_credential(self):
        """Ollama needs no auth - just compile."""
        raw = _base_app()
        raw["agents"] = [{
            "id": "main",
            "brain": {"provider": "ollama", "model": "llama3.2"},
        }]
        result = _compiler().compile(raw)
        assert isinstance(result, CompiledApp)

    def test_brain_local_provider_lm_studio_compiles_without_credential(self):
        raw = _base_app()
        raw["agents"] = [{
            "id": "main",
            "brain": {"provider": "lm_studio", "model": "qwen2.5-coder"},
        }]
        result = _compiler().compile(raw)
        assert isinstance(result, CompiledApp)

    def test_brain_local_provider_vllm_compiles_without_credential(self):
        raw = _base_app()
        raw["agents"] = [{
            "id": "main",
            "brain": {"provider": "vllm", "model": "meta-llama/Llama-3-8B"},
        }]
        result = _compiler().compile(raw)
        assert isinstance(result, CompiledApp)

    def test_brain_anthropic_claude_code_marker_compiles(self):
        """Special sentinel for the Claude Code OAuth token bypass."""
        raw = _base_app()
        raw["agents"] = [{
            "id": "main",
            "brain": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "config": {"api_key": "claude-code"},
            },
        }]
        result = _compiler().compile(raw)
        assert isinstance(result, CompiledApp)

    def test_brain_localhost_base_url_override_compiles(self):
        """OpenAI-compat targeting localhost (e.g. Ollama via OpenAI client)."""
        raw = _base_app()
        raw["agents"] = [{
            "id": "main",
            "brain": {
                "provider": "openai",
                "model": "qwen2.5-coder",
                "config": {"base_url": "http://localhost:11434/v1"},
            },
        }]
        result = _compiler().compile(raw)
        assert isinstance(result, CompiledApp)

    def test_brain_inline_api_key_compiles(self):
        """Inline api_key in config (dev-mode YAML) is acceptable."""
        raw = _base_app()
        raw["agents"] = [{
            "id": "main",
            "brain": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "config": {"api_key": "{{env.OPENAI_API_KEY}}"},
            },
        }]
        result = _compiler().compile(raw)
        assert isinstance(result, CompiledApp)

    def test_brain_credential_ref_compiles(self):
        """Recommended path: credential ref."""
        raw = _base_app()
        raw["agents"] = [{
            "id": "main",
            "brain": {
                "provider": "anthropic",
                "model": "claude-haiku-4-5",
                "credential": "anthropic_main",
            },
        }]
        result = _compiler().compile(raw)
        assert isinstance(result, CompiledApp)


class TestBrainVisionMismatch:
    """Gap A3 - vision: true on a non-vision model must be flagged."""

    def test_brain_vision_on_deepseek_chat_caught(self):
        raw = _base_app()
        raw["agents"] = [{
            "id": "main",
            "brain": {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "credential": "deepseek_main",
                "vision": True,
            },
        }]
        with pytest.raises(AppCompilationError) as exc_info:
            _compiler().compile(raw)
        msg = _err_text(exc_info)
        assert "vision" in msg.lower()
        assert "deepseek-chat" in msg.lower() or "model" in msg.lower()

    def test_brain_vision_auto_compiles(self):
        """vision: null (auto-detect) must remain accepted."""
        raw = _base_app()
        raw["agents"] = [{
            "id": "main",
            "brain": {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "credential": "deepseek_main",
                # vision unset - auto-detect from model name
            },
        }]
        result = _compiler().compile(raw)
        assert isinstance(result, CompiledApp)

    def test_brain_vision_on_vision_model_compiles(self):
        raw = _base_app()
        raw["agents"] = [{
            "id": "main",
            "brain": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "credential": "anthropic_main",
                "vision": True,
            },
        }]
        result = _compiler().compile(raw)
        assert isinstance(result, CompiledApp)


# ═══════════════════════════════════════════════════════════════════════
# Category B - Tool reference validation
# ═══════════════════════════════════════════════════════════════════════


class TestHookToolNameValidation:
    """Gap B1 - hook condition tool_name.match must reference a real tool."""

    def test_hook_tool_name_typo_caught(self):
        web = _make_module("web", ["search", "fetch"])
        raw = _base_app({"modules": {"web": {}}})
        raw["execution"] = {
            "hooks": [{
                "id": "retry",
                "on": "tool_end",
                "condition": {"type": "tool_name", "match": "web.serch"},  # typo
                "action": {"type": "log", "message": "x"},
            }],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compiler({"web": web}).compile(raw)
        msg = _err_text(exc_info)
        assert "web.serch" in msg or "tool_name" in msg.lower()

    def test_hook_tool_name_valid_regex_compiles(self):
        web = _make_module("web", ["search", "fetch"])
        raw = _base_app({"modules": {"web": {}}})
        raw["execution"] = {
            "hooks": [{
                "id": "retry",
                "on": "tool_end",
                "condition": {"type": "tool_name", "match": "web.search"},
                "action": {"type": "log", "message": "x"},
            }],
        }
        result = _compiler({"web": web}).compile(raw)
        assert isinstance(result, CompiledApp)

    def test_hook_tool_name_regex_alternation_compiles(self):
        """match: 'web.search|web.fetch' should match either."""
        web = _make_module("web", ["search", "fetch"])
        raw = _base_app({"modules": {"web": {}}})
        raw["execution"] = {
            "hooks": [{
                "id": "retry",
                "on": "tool_end",
                "condition": {"type": "tool_name", "match": "web.search|web.fetch"},
                "action": {"type": "log", "message": "x"},
            }],
        }
        result = _compiler({"web": web}).compile(raw)
        assert isinstance(result, CompiledApp)


class TestBehaviorRuleTriggerValidation:
    """Gap B2 - behavior custom rule trigger must reference a real tool."""

    def test_behavior_rule_trigger_typo_caught(self):
        fs = _make_module("filesystem", ["read", "write", "edit"])
        raw = _base_app({"modules": {"filesystem": {}}})
        raw["behavior"] = {
            "custom": [{
                "id": "no_blind_write",
                "trigger": "filesytem.write",  # typo
                "rule": "Read before write",
                "action": "block",
            }],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compiler({"filesystem": fs}).compile(raw)
        msg = _err_text(exc_info)
        assert "filesytem.write" in msg or "trigger" in msg.lower()

    def test_behavior_rule_trigger_valid_compiles(self):
        fs = _make_module("filesystem", ["read", "write", "edit"])
        raw = _base_app({"modules": {"filesystem": {}}})
        raw["behavior"] = {
            "custom": [{
                "id": "no_blind_write",
                "trigger": "filesystem.write",
                "rule": "Read before write",
                "action": "block",
            }],
        }
        result = _compiler({"filesystem": fs}).compile(raw)
        assert isinstance(result, CompiledApp)


# ═══════════════════════════════════════════════════════════════════════
# Category C - Cross-references
# ═══════════════════════════════════════════════════════════════════════


class TestAgentModulesScope:
    """Gap C1 - agents[].modules must reference real modules and actions."""

    def test_agent_modules_unknown_module_caught(self):
        fs = _make_module("filesystem", ["read", "write"])
        raw = _base_app({"modules": {"filesystem": {}}})
        raw["agents"][0]["modules"] = ["filsystem"]  # typo
        with pytest.raises(AppCompilationError) as exc_info:
            _compiler({"filesystem": fs}).compile(raw)
        msg = _err_text(exc_info)
        assert "filsystem" in msg or "module" in msg.lower()

    def test_agent_modules_unknown_action_caught(self):
        fs = _make_module("filesystem", ["read", "write"])
        raw = _base_app({"modules": {"filesystem": {}}})
        raw["agents"][0]["modules"] = [{"filesystem": ["read", "delte"]}]  # typo
        with pytest.raises(AppCompilationError) as exc_info:
            _compiler({"filesystem": fs}).compile(raw)
        msg = _err_text(exc_info)
        assert "delte" in msg or "action" in msg.lower()

    def test_agent_modules_simple_form_compiles(self):
        fs = _make_module("filesystem", ["read", "write"])
        raw = _base_app({"modules": {"filesystem": {}}})
        raw["agents"][0]["modules"] = ["filesystem"]
        result = _compiler({"filesystem": fs}).compile(raw)
        assert isinstance(result, CompiledApp)

    def test_agent_modules_granular_form_compiles(self):
        fs = _make_module("filesystem", ["read", "write", "edit"])
        raw = _base_app({"modules": {"filesystem": {}}})
        raw["agents"][0]["modules"] = [{"filesystem": ["read", "write"]}]
        result = _compiler({"filesystem": fs}).compile(raw)
        assert isinstance(result, CompiledApp)

    def test_agent_modules_mixed_form_compiles(self):
        fs = _make_module("filesystem", ["read"])
        sh = _make_module("shell", ["bash"])
        raw = _base_app({"modules": {"filesystem": {}, "shell": {}}})
        raw["agents"][0]["modules"] = ["filesystem", {"shell": ["bash"]}]
        result = _compiler({"filesystem": fs, "shell": sh}).compile(raw)
        assert isinstance(result, CompiledApp)


class TestSkillFileExists:
    """Gap C3 - agents[].skills (file path) must point to a real file."""

    def test_skill_file_missing_caught(self, tmp_path: Path):
        bundle = tmp_path / "app"
        bundle.mkdir()
        (bundle / "app.yaml").write_text("# stub")
        raw = _base_app()
        raw["agents"][0]["skills"] = "./skills/missing.md"
        with pytest.raises(AppCompilationError) as exc_info:
            _compiler().compile_string(yaml.safe_dump(raw), source=str(bundle / "app.yaml"))
        msg = _err_text(exc_info)
        assert "missing.md" in msg or "skills" in msg.lower()

    def test_skill_file_present_compiles(self, tmp_path: Path):
        bundle = tmp_path / "app"
        bundle.mkdir()
        (bundle / "app.yaml").write_text("# stub")
        skills = bundle / "skills"
        skills.mkdir()
        (skills / "research.md").write_text("# Research methodology\n...")
        raw = _base_app()
        raw["agents"][0]["skills"] = "./skills/research.md"
        result = _compiler().compile_string(
            yaml.safe_dump(raw), source=str(bundle / "app.yaml"),
        )
        assert isinstance(result, CompiledApp)


class TestCredentialRefAgainstSchema:
    """Gap C4 - credential refs validated against credentials_schema."""

    def test_credential_ref_undeclared_caught(self):
        """When credentials_schema is declared, references must match."""
        raw = _base_app()
        raw["agents"][0]["brain"] = {
            "provider": "anthropic",
            "model": "claude-haiku-4-5",
            "credential": "wrong_name",
        }
        raw["execution"] = {
            "credentials_schema": {
                "providers": [
                    {
                        "name": "anthropic_main",
                        "type": "api_key",
                        "scope": "per_user",
                        "fields": [{"name": "api_key", "type": "string", "required": True}],
                    }
                ],
            },
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compiler().compile(raw)
        msg = _err_text(exc_info)
        assert "wrong_name" in msg or "credential" in msg.lower()

    def test_credential_ref_no_schema_compiles(self):
        """Without credentials_schema, ref is opaque - accept anything."""
        raw = _base_app()
        raw["agents"][0]["brain"] = {
            "provider": "anthropic",
            "model": "claude-haiku-4-5",
            "credential": "anything_user_typed",
        }
        result = _compiler().compile(raw)
        assert isinstance(result, CompiledApp)

    def test_credential_ref_matches_schema_compiles(self):
        raw = _base_app()
        raw["agents"][0]["brain"] = {
            "provider": "anthropic",
            "model": "claude-haiku-4-5",
            "credential": "anthropic_main",
        }
        raw["execution"] = {
            "credentials_schema": {
                "providers": [
                    {
                        "name": "anthropic_main",
                        "type": "api_key",
                        "scope": "per_user",
                        "fields": [{"name": "api_key", "type": "string", "required": True}],
                    }
                ],
            },
        }
        result = _compiler().compile(raw)
        assert isinstance(result, CompiledApp)


# ═══════════════════════════════════════════════════════════════════════
# Category D - Mode coherence
# ═══════════════════════════════════════════════════════════════════════


class TestTriggersWithoutBackground:
    """Gap D1 - declaring triggers in non-background mode must be visible.

    Currently the compiler has a code path commented as 'warning emitted in
    compile()' but no warning is actually emitted. We close the gap by
    adding the missing emission. The test asserts the warning is reachable
    via the compile output (collected_warnings or a similar surface).
    """

    def test_triggers_in_conversation_mode_rejected(self):
        """Triggers in non-background mode → hard compile error.

        Stricter than the audit's original 'warning' plan because the
        runtime silently ignores these triggers, which violates the
        'if it compiles, it runs' contract. We refuse outright.
        """
        raw = _base_app()
        raw["execution"] = {
            "mode": "conversation",
            "triggers": [{"id": "ghost", "type": "cron", "schedule": "0 9 * * *"}],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compiler().compile(raw)
        msg = _err_text(exc_info).lower()
        assert "trigger" in msg
        assert "background" in msg or "conversation" in msg

    def test_triggers_in_background_mode_no_warning(self):
        raw = _base_app()
        raw["execution"] = {
            "mode": "background",
            "entry_agent": "main",
            "triggers": [{"id": "live", "type": "cron", "schedule": "0 9 * * *"}],
        }
        result = _compiler().compile(raw)
        warnings = getattr(result, "warnings", []) or []
        bad = [w for w in warnings if "background" in w.lower() and "trigger" in w.lower()]
        assert bad == [], f"Unexpected trigger-mode warnings: {bad}"


# ═══════════════════════════════════════════════════════════════════════
# Category F - Resource bounds
# ═══════════════════════════════════════════════════════════════════════


class TestResourceBounds:
    """Gap F1 - max_turns must be ≥ 1, timeout must be > 0."""

    def test_max_turns_zero_rejected(self):
        raw = _base_app()
        raw["execution"] = {"max_turns": 0}
        with pytest.raises(AppCompilationError) as exc_info:
            _compiler().compile(raw)
        msg = _err_text(exc_info)
        assert "max_turns" in msg

    def test_max_turns_negative_rejected(self):
        raw = _base_app()
        raw["execution"] = {"max_turns": -5}
        with pytest.raises(AppCompilationError) as exc_info:
            _compiler().compile(raw)
        msg = _err_text(exc_info)
        assert "max_turns" in msg

    def test_max_turns_one_accepted(self):
        raw = _base_app()
        raw["execution"] = {"max_turns": 1}
        result = _compiler().compile(raw)
        assert isinstance(result, CompiledApp)

    def test_timeout_zero_rejected(self):
        raw = _base_app()
        raw["execution"] = {"timeout": 0}
        with pytest.raises(AppCompilationError) as exc_info:
            _compiler().compile(raw)
        msg = _err_text(exc_info)
        assert "timeout" in msg

    def test_timeout_negative_rejected(self):
        raw = _base_app()
        raw["execution"] = {"timeout": -1.0}
        with pytest.raises(AppCompilationError) as exc_info:
            _compiler().compile(raw)
        msg = _err_text(exc_info)
        assert "timeout" in msg

    def test_timeout_small_positive_accepted(self):
        raw = _base_app()
        raw["execution"] = {"timeout": 0.5}
        result = _compiler().compile(raw)
        assert isinstance(result, CompiledApp)


# ═══════════════════════════════════════════════════════════════════════
# Regression - real builtin apps + web YAMLs still compile
# ═══════════════════════════════════════════════════════════════════════


REPO_ROOT = Path(__file__).resolve().parent.parent
BUILTINS_DIR = REPO_ROOT / "packages" / "digitorn" / "builtins"


def _list_builtin_yamls() -> list[Path]:
    return sorted(BUILTINS_DIR.glob("*/app.yaml"))


@pytest.mark.parametrize("yaml_path", _list_builtin_yamls(), ids=lambda p: p.parent.name)
def test_existing_builtin_apps_still_pydantic_validate(yaml_path: Path):
    """Every shipped builtin must still pass Pydantic validation.

    We do NOT run the full compile because the registry is empty in unit
    tests (modules are not loaded). The assertion is the strongest one
    that's safe to run in isolation: AppDefinition.model_validate must
    accept the YAML AFTER the alias pass (schema_aliases.apply_schema_aliases),
    which mirrors what the real compile pipeline does and is what the
    canonical schema expects.
    """
    from digitorn.core.app.schema import AppDefinition
    from digitorn.core.app.schema_aliases import apply_schema_aliases

    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    raw = apply_schema_aliases(raw)
    AppDefinition.model_validate(raw)
