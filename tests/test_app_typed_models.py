"""Phase 2 tests for the typed replacements of ``dict[str, Any]`` fields.

Four fields get strict Pydantic models so the IDE auto-completes and
typos surface as validation errors:

  - ``AppMeta.quick_prompts`` -> ``list[QuickPrompt]``
  - ``AppDefinition.skills`` -> ``list[SkillEntry]``
  - ``AppDefinition.slash_commands`` -> ``list[SlashCommand]``
  - ``AgentDefinition.pool`` -> ``AgentPoolConfig``
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from digitorn.core.app.compiler import AppYAMLCompiler, CompiledApp
from digitorn.core.app.errors import AppCompilationError


def _registry(modules: dict[str, Any] | None = None):
    r = MagicMock()
    modules = modules or {}
    r.list_available.return_value = list(modules.keys())
    r.get = MagicMock(side_effect=lambda mid: modules[mid] if mid in modules else (_ for _ in ()).throw(Exception(f"no {mid}")))
    r.create = r.get
    r.is_available = MagicMock(side_effect=lambda mid: mid in modules)
    return r


def _base() -> dict:
    return {
        "app": {"app_id": "test", "name": "Test"},
        "agents": [
            {"id": "main", "brain": {"provider": "ollama", "model": "llama3"}},
        ],
    }


def _err(exc: pytest.ExceptionInfo) -> str:
    return "\n".join(str(e) for e in exc.value.errors)


def _compile(raw: dict) -> CompiledApp:
    return AppYAMLCompiler(_registry()).compile(raw)


# ─── QuickPrompt (AppMeta.quick_prompts) ───────────────────────


class TestQuickPrompts:

    def test_valid_quick_prompt_compiles(self):
        raw = _base()
        raw["app"]["quick_prompts"] = [
            {"label": "Counter", "icon": "🔢", "message": "Build a counter"},
        ]
        result = _compile(raw)
        assert result.meta.quick_prompts[0].label == "Counter"

    def test_quick_prompt_missing_message_caught(self):
        raw = _base()
        raw["app"]["quick_prompts"] = [{"label": "X", "icon": "Y"}]
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        assert "message" in _err(exc_info).lower()

    def test_quick_prompt_extra_field_allowed(self):
        """Future-proof: extra keys pass through (extra: allow)."""
        raw = _base()
        raw["app"]["quick_prompts"] = [{
            "label": "X", "message": "Y", "tooltip": "future field",
        }]
        result = _compile(raw)
        assert result.meta.quick_prompts[0].label == "X"


# ─── SkillEntry (AppDefinition.skills) ─────────────────────────


class TestSkillEntries:

    def test_valid_skill_entry_compiles(self, tmp_path):
        skill_path = tmp_path / "commit.md"
        skill_path.write_text("# Commit skill\nWrite a clean message.\n")
        raw = _base()
        raw["skills"] = [
            {"command": "/commit", "description": "Compose commit", "path": str(skill_path)},
        ]
        result = _compile(raw)
        # CompiledApp.skills is a list[dict] enriched with loaded markdown
        # content - the dict access keeps the legacy contract for callers.
        assert result.skills[0]["command"] == "/commit"

    def test_skill_missing_command_caught(self):
        raw = _base()
        raw["skills"] = [{"description": "x", "path": "x.md"}]
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        assert "command" in _err(exc_info).lower()

    def test_skill_missing_path_caught(self):
        raw = _base()
        raw["skills"] = [{"command": "/x", "description": "x"}]
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        assert "path" in _err(exc_info).lower()


# ─── SlashCommand (AppDefinition.slash_commands) ───────────────


class TestSlashCommands:

    def test_valid_slash_command_compiles(self):
        raw = _base()
        raw["slash_commands"] = [
            {"command": "/deploy", "description": "Deploy app", "template": "Deploy to {{env}}"},
        ]
        result = _compile(raw)
        # The compiled output stores slash_commands as plain dicts so
        # the API surface (summary(), client) keeps the historical
        # mapping shape. Pydantic models are only used for input validation.
        assert result.slash_commands[0]["command"] == "/deploy"

    def test_slash_command_missing_command_caught(self):
        raw = _base()
        raw["slash_commands"] = [{"description": "x"}]
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        assert "command" in _err(exc_info).lower()


# ─── AgentPoolConfig (AgentDefinition.pool) ────────────────────


class TestAgentPool:

    def test_valid_pool_compiles(self):
        raw = _base()
        raw["agents"][0]["pool"] = {"max_workers": 5, "progress": True, "auto_retry": 2}
        result = _compile(raw)
        assert result.agents[0].pool_max_workers == 5

    def test_pool_unknown_key_caught(self):
        raw = _base()
        raw["agents"][0]["pool"] = {"max_workers": 5, "ghost_field": True}
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        assert "ghost_field" in _err(exc_info).lower()

    def test_pool_max_workers_zero_caught(self):
        raw = _base()
        raw["agents"][0]["pool"] = {"max_workers": 0}
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        msg = _err(exc_info).lower()
        assert "max_workers" in msg

    def test_pool_max_workers_negative_caught(self):
        raw = _base()
        raw["agents"][0]["pool"] = {"max_workers": -1}
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        assert "max_workers" in _err(exc_info).lower()
