"""Tests for agent compilation in the app YAML compiler.

Verifies both brain modes:
  1. Inline - full provider config embedded in the agent
  2. Reference - provider_id pointing to a named provider in llm_provider
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from digitorn.core.app.compiler import AppYAMLCompiler, CompiledAgent, CompiledApp, CompiledBrain
from digitorn.core.app.errors import AppCompilationError
from digitorn.core.app.schema import AgentBrain, AgentDefinition, AppDefinition


# ---------------------------------------------------------------------------
# Helpers - mock registry (no real modules needed)
# ---------------------------------------------------------------------------


def _make_action_entry(name: str):
    entry = MagicMock()
    entry.name = name
    entry.params_model = None
    spec = MagicMock()
    spec.name = name
    spec.risk_level = "low"
    spec.permissions = []
    spec.irreversible = False
    entry.spec = spec
    return entry


def _make_module(module_id: str, actions: list[str] | None = None):
    module = MagicMock()
    module.MODULE_ID = module_id
    manifest = MagicMock()
    manifest.action_names.return_value = actions or []
    manifest.supported_constraints = []
    module.get_manifest.return_value = manifest
    module.CONFIG_MODEL = None
    registry = {}
    for a in (actions or []):
        registry[a] = _make_action_entry(a)
    module._action_registry = registry
    return module


def _make_registry(modules: dict[str, MagicMock]):
    registry = MagicMock()
    registry.list_available.return_value = list(modules.keys())

    def get(mid):
        if mid not in modules:
            raise Exception(f"Module '{mid}' not found")
        return modules[mid]

    registry.get = MagicMock(side_effect=get)
    return registry


def _compile(raw: dict, modules: dict | None = None) -> CompiledApp:
    """Compile a raw YAML dict with optional mock modules."""
    if modules is None:
        modules = {}
    # Always include llm_provider if agents reference it
    if "llm_provider" not in modules:
        modules["llm_provider"] = _make_module(
            "llm_provider", ["configure", "chat", "list_providers"]
        )
    registry = _make_registry(modules)
    compiler = AppYAMLCompiler(registry)
    return compiler.compile(raw)


# ---------------------------------------------------------------------------
# Tests - Schema parsing
# ---------------------------------------------------------------------------


class TestAgentSchema:

    def test_parse_inline_brain(self):
        defn = AppDefinition.model_validate({
            "app": {"app_id": "test", "name": "Test"},
            "agents": [{
                "id": "coordinator",
                "role": "coordinator",
                "brain": {
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "temperature": 0.2,
                    "max_tokens": 8192,
                    "timeout": 120.0,
                    "config": {
                        "api_key": "sk-xxx",
                        "base_url": "https://api.deepseek.com/v1",
                    },
                },
                "system_prompt": "You are a coordinator.",
            }],
        })

        assert len(defn.agents) == 1
        agent = defn.agents[0]
        assert agent.id == "coordinator"
        assert agent.brain.provider == "deepseek"
        assert agent.brain.model == "deepseek-chat"
        assert agent.brain.temperature == 0.2
        assert agent.brain.is_reference is False
        assert agent.brain.config["api_key"] == "sk-xxx"

    def test_parse_reference_brain(self):
        defn = AppDefinition.model_validate({
            "app": {"app_id": "test", "name": "Test"},
            "agents": [{
                "id": "worker",
                "brain": {
                    "provider_id": "deepseek_main",
                    "temperature": 0.7,
                },
            }],
        })

        agent = defn.agents[0]
        assert agent.brain.is_reference is True
        assert agent.brain.provider_id == "deepseek_main"
        assert agent.brain.temperature == 0.7

    def test_parse_no_agents(self):
        defn = AppDefinition.model_validate({
            "app": {"app_id": "test", "name": "Test"},
        })
        assert defn.agents == []

    def test_brain_defaults(self):
        brain = AgentBrain.model_validate({"model": "gpt-4o"})
        assert brain.backend == "openai_compat"
        assert brain.temperature is None
        assert brain.is_reference is False


# ---------------------------------------------------------------------------
# Tests - Inline brain compilation
# ---------------------------------------------------------------------------


class TestInlineBrainCompilation:

    def test_inline_brain_creates_provider(self):
        """Inline brain generates a provider config in llm_provider."""
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "agents": [{
                "id": "coordinator",
                "brain": {
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "temperature": 0.2,
                    "config": {
                        "api_key": "sk-xxx",
                        "base_url": "https://api.deepseek.com/v1",
                    },
                },
            }],
        })

        assert len(compiled.agents) == 1
        agent = compiled.agents[0]
        assert agent.agent_id == "coordinator"
        assert agent.brain.provider_id == "coordinator_brain"
        assert agent.brain.is_inline is True
        assert agent.brain.temperature == 0.2

    def test_inline_brain_injected_into_llm_provider(self):
        """Inline brain config is auto-injected into llm_provider module."""
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "agents": [{
                "id": "coder",
                "brain": {
                    "provider": "ollama",
                    "model": "codellama:34b",
                    "temperature": 0.1,
                },
            }],
        })

        # llm_provider should be in compiled modules
        assert "llm_provider" in compiled.modules
        providers = compiled.modules["llm_provider"].config["providers"]
        assert "coder_brain" in providers
        assert providers["coder_brain"]["model"] == "codellama:34b"
        assert providers["coder_brain"]["provider"] == "ollama"
        assert providers["coder_brain"]["temperature"] == 0.1

    def test_multiple_inline_agents(self):
        """Multiple agents each get their own auto-generated provider."""
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "agents": [
                {
                    "id": "coordinator",
                    "role": "coordinator",
                    "brain": {
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                        "config": {"api_key": "sk-test"},
                        "temperature": 0.2,
                    },
                },
                {
                    "id": "worker",
                    "role": "worker",
                    "brain": {"provider": "ollama", "model": "llama3", "temperature": 0.7},
                },
            ],
        })

        assert len(compiled.agents) == 2
        assert compiled.agents[0].brain.provider_id == "coordinator_brain"
        assert compiled.agents[1].brain.provider_id == "worker_brain"

        providers = compiled.modules["llm_provider"].config["providers"]
        assert "coordinator_brain" in providers
        assert "worker_brain" in providers

    def test_inline_brain_requires_model(self):
        """Inline brain without model raises compilation error."""
        with pytest.raises(AppCompilationError, match="model.*required"):
            _compile({
                "app": {"app_id": "test", "name": "Test"},
                "agents": [{
                    "id": "bad",
                    "brain": {"provider": "deepseek"},
                }],
            })

    def test_inline_brain_with_variables(self):
        """Variables in brain config are resolved."""
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "variables": {"ds_key": "my-secret-key"},
            "agents": [{
                "id": "agent1",
                "brain": {
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "config": {"api_key": "{{ds_key}}"},
                },
            }],
        })

        providers = compiled.modules["llm_provider"].config["providers"]
        assert providers["agent1_brain"]["api_key"] == "my-secret-key"

    def test_inline_brain_with_system_prompt(self):
        """System prompt is preserved."""
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "agents": [{
                "id": "helper",
                "brain": {"model": "gpt-4o"},
                "system_prompt": "You are helpful.",
            }],
        })

        assert compiled.agents[0].system_prompt == "You are helpful."

    def test_inline_brain_merges_with_existing_llm_provider(self):
        """Inline providers merge with explicitly declared llm_provider config."""
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "llm_provider": {
                    "config": {
                        "providers": {
                            "shared_brain": {
                                "backend": "anthropic",
                                "model": "claude-sonnet-4-20250514",
                            },
                        },
                    },
                },
            },
            "agents": [{
                "id": "worker",
                "brain": {
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "config": {"api_key": "sk-test"},
                },
            }],
        })

        providers = compiled.modules["llm_provider"].config["providers"]
        assert "shared_brain" in providers  # Explicit
        assert "worker_brain" in providers  # Auto-injected


# ---------------------------------------------------------------------------
# Tests - Reference brain compilation
# ---------------------------------------------------------------------------


class TestReferenceBrainCompilation:

    def test_reference_brain_valid(self):
        """Reference brain with valid provider_id compiles successfully."""
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "llm_provider": {
                    "config": {
                        "providers": {
                            "deepseek_main": {
                                "provider": "deepseek",
                                "model": "deepseek-chat",
                            },
                        },
                    },
                },
            },
            "agents": [{
                "id": "coordinator",
                "brain": {
                    "provider_id": "deepseek_main",
                    "temperature": 0.2,
                },
            }],
        })

        assert len(compiled.agents) == 1
        agent = compiled.agents[0]
        assert agent.brain.provider_id == "deepseek_main"
        assert agent.brain.is_inline is False
        assert agent.brain.temperature == 0.2

    def test_reference_brain_unknown_provider(self):
        """Reference to unknown provider raises compilation error."""
        with pytest.raises(AppCompilationError, match="not found"):
            _compile({
                "app": {"app_id": "test", "name": "Test"},
                "modules": {
                    "llm_provider": {
                        "config": {"providers": {"brain_a": {"model": "x"}}},
                    },
                },
                "agents": [{
                    "id": "agent1",
                    "brain": {"provider_id": "nonexistent"},
                }],
            })

    def test_reference_brain_no_llm_provider(self):
        """Reference brain without llm_provider module raises error."""
        with pytest.raises(AppCompilationError, match="not found"):
            _compile({
                "app": {"app_id": "test", "name": "Test"},
                "agents": [{
                    "id": "agent1",
                    "brain": {"provider_id": "some_provider"},
                }],
            })

    def test_multiple_agents_share_provider(self):
        """Multiple agents can reference the same named provider."""
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "llm_provider": {
                    "config": {
                        "providers": {
                            "shared": {"model": "deepseek-chat"},
                        },
                    },
                },
            },
            "agents": [
                {"id": "a1", "brain": {"provider_id": "shared", "temperature": 0.2}},
                {"id": "a2", "brain": {"provider_id": "shared", "temperature": 0.8}},
            ],
        })

        assert compiled.agents[0].brain.provider_id == "shared"
        assert compiled.agents[1].brain.provider_id == "shared"
        assert compiled.agents[0].brain.temperature == 0.2
        assert compiled.agents[1].brain.temperature == 0.8


# ---------------------------------------------------------------------------
# Tests - Validation errors
# ---------------------------------------------------------------------------


class TestAgentValidation:

    def test_duplicate_agent_id(self):
        """Duplicate agent IDs are rejected."""
        with pytest.raises(AppCompilationError, match="Duplicate"):
            _compile({
                "app": {"app_id": "test", "name": "Test"},
                "agents": [
                    {"id": "agent1", "brain": {"model": "gpt-4o"}},
                    {"id": "agent1", "brain": {"model": "llama3"}},
                ],
            })

    def test_agent_role_default(self):
        """Default role is 'worker'."""
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "agents": [{"id": "a1", "brain": {"model": "gpt-4o"}}],
        })

        assert compiled.agents[0].role == "worker"

    def test_agent_ids_property(self):
        """CompiledApp.agent_ids returns all agent IDs."""
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "agents": [
                {"id": "coordinator", "role": "coordinator", "brain": {"model": "gpt-4o"}},
                {"id": "worker", "brain": {"model": "llama3"}},
            ],
        })

        assert compiled.agent_ids == ["coordinator", "worker"]

    def test_no_agents_rejected(self):
        """App without agents is rejected by the compiler."""
        from digitorn.core.app.errors import AppCompilationError
        with pytest.raises(AppCompilationError, match="At least one agent"):
            _compile({
                "app": {"app_id": "test", "name": "Test"},
            })


# ---------------------------------------------------------------------------
# Tests - Mixed mode (inline + reference)
# ---------------------------------------------------------------------------


class TestMixedMode:

    def test_inline_and_reference_together(self):
        """App can mix inline and reference brains."""
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "modules": {
                "llm_provider": {
                    "config": {
                        "providers": {
                            "shared_claude": {
                                "backend": "anthropic",
                                "model": "claude-sonnet-4-20250514",
                            },
                        },
                    },
                },
            },
            "agents": [
                {
                    "id": "coordinator",
                    "role": "coordinator",
                    "brain": {
                        "provider_id": "shared_claude",
                        "temperature": 0.2,
                    },
                },
                {
                    "id": "coder",
                    "brain": {
                        "provider": "ollama",
                        "model": "codellama:34b",
                        "temperature": 0.1,
                    },
                },
            ],
        })

        assert len(compiled.agents) == 2

        # Coordinator uses reference
        coord = compiled.agents[0]
        assert coord.brain.provider_id == "shared_claude"
        assert coord.brain.is_inline is False

        # Coder uses inline (auto-generated)
        coder = compiled.agents[1]
        assert coder.brain.provider_id == "coder_brain"
        assert coder.brain.is_inline is True

        # Both providers in llm_provider config
        providers = compiled.modules["llm_provider"].config["providers"]
        assert "shared_claude" in providers
        assert "coder_brain" in providers
