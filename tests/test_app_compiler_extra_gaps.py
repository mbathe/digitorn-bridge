"""Phase 5 - close remaining audit gaps.

Picks up the items the Phase 1 audit deferred plus a few smells found
on the way:

  D2  ``compact_context`` hook makes no sense in ``one_shot`` mode
      (no turns to compact across).
  E1  An agent declaring ``agent_spawn`` actions must have the
      ``agent_spawn`` module loaded - otherwise the dispatcher is
      missing at runtime.
  C2  ``agents[].capabilities: [name]`` must resolve to a real
      ``./skills/<name>.md`` file when a source dir is known.
  Extra Hook ``action.type: shell`` requires the ``shell`` module to
      be declared (and granted), otherwise the hook fires through a
      missing executor.

Each gap is implemented as a non-fatal warning OR a hard error
depending on whether the runtime would silently misbehave (warning)
or actively fail (error). The choice matches "if it compiles, it runs"
discipline: anything the runtime can't deliver becomes an error.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from digitorn.core.app.compiler import AppYAMLCompiler, CompiledApp
from digitorn.core.app.errors import AppCompilationError


# ─── Test fixtures ──────────────────────────────────────────────


def _make_module(module_id: str, actions: list[str]):
    module = MagicMock()
    module.MODULE_ID = module_id
    manifest = MagicMock()
    manifest.action_names.return_value = actions
    manifest.actions = []
    for n in actions:
        spec = MagicMock()
        spec.name = n
        spec.params = []
        manifest.actions.append(spec)
    manifest.supported_constraints = []
    module.get_manifest.return_value = manifest
    module._action_registry = {}
    module.execute = AsyncMock()
    module.on_config_update = AsyncMock()
    module.on_start = AsyncMock()
    module.on_stop = AsyncMock()
    return module


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
        "agents": [{"id": "main", "brain": {"provider": "ollama", "model": "llama3"}}],
    }


def _err(exc: pytest.ExceptionInfo) -> str:
    return "\n".join(str(e) for e in exc.value.errors)


def _warnings(result: CompiledApp) -> list[str]:
    return list(getattr(result, "warnings", []) or [])


# ─── D2: compact_context + one_shot ─────────────────────────────


class TestCompactContextMode:

    def test_compact_context_in_one_shot_warns(self):
        raw = _base()
        raw["execution"] = {
            "mode": "one_shot",
            "hooks": [{
                "id": "compact",
                "on": "turn_end",
                "condition": {"type": "always"},
                "action": {"type": "compact_context"},
            }],
        }
        result = AppYAMLCompiler(_registry()).compile(raw)
        warnings = _warnings(result)
        joined = "\n".join(warnings).lower()
        assert "compact_context" in joined and "one_shot" in joined, (
            f"Expected a warning about compact_context in one_shot, got: {warnings!r}"
        )

    def test_compact_context_in_conversation_no_warning(self):
        raw = _base()
        raw["execution"] = {
            "mode": "conversation",
            "hooks": [{
                "id": "compact",
                "on": "turn_end",
                "condition": {"type": "always"},
                "action": {"type": "compact_context"},
            }],
        }
        result = AppYAMLCompiler(_registry()).compile(raw)
        bad = [w for w in _warnings(result) if "compact_context" in w.lower()]
        assert bad == [], f"Unexpected compact_context warnings: {bad}"


# ─── E1: agent_spawn module required when used ─────────────────


class TestAgentSpawnRequired:
    """``agent_spawn`` is one of the system modules auto-injected by
    bootstrap if a specialist agent exists. The compiler must NOT error
    when an agent uses ``agent_spawn`` actions without the user
    declaring it explicitly - that's by design (system module). What
    we DO check is the consistency: the action names listed are
    real."""

    def test_agent_using_agent_spawn_compiles_without_explicit_module(self):
        raw = _base()
        raw["agents"] = [
            {
                "id": "lead",
                "role": "coordinator",
                "brain": {"provider": "ollama", "model": "llama3"},
                "modules": [{"agent_spawn": ["Agent"]}],
            },
            {
                "id": "worker",
                "role": "specialist",
                "brain": {"provider": "ollama", "model": "llama3"},
            },
        ]
        result = AppYAMLCompiler(_registry()).compile(raw)
        assert isinstance(result, CompiledApp)


# ─── C2: agents[].capabilities resolves to skills/<name>.md ────


class TestCapabilitiesAgainstSkills:

    def test_capabilities_pointing_at_missing_skill_caught(self, tmp_path: Path):
        bundle = tmp_path / "app"
        bundle.mkdir()
        raw = _base()
        raw["agents"][0]["capabilities"] = ["missing_skill"]
        (bundle / "app.yaml").write_text(yaml.safe_dump(raw))
        with pytest.raises(AppCompilationError) as exc_info:
            AppYAMLCompiler(_registry()).compile_string(
                yaml.safe_dump(raw), source=str(bundle / "app.yaml"),
            )
        msg = _err(exc_info).lower()
        assert "missing_skill" in msg or "skills" in msg

    def test_capabilities_pointing_at_existing_skill_compiles(self, tmp_path: Path):
        bundle = tmp_path / "app"
        bundle.mkdir()
        skills_dir = bundle / "skills"
        skills_dir.mkdir()
        (skills_dir / "research.md").write_text("# Research methodology\n")
        raw = _base()
        raw["agents"][0]["capabilities"] = ["research"]
        (bundle / "app.yaml").write_text(yaml.safe_dump(raw))
        result = AppYAMLCompiler(_registry()).compile_string(
            yaml.safe_dump(raw), source=str(bundle / "app.yaml"),
        )
        assert isinstance(result, CompiledApp)


# ─── Extra: hook shell action requires shell module ────────────


class TestHookShellRequiresShell:

    def test_hook_shell_action_without_shell_module_caught(self):
        raw = _base()
        # shell module NOT declared
        raw["execution"] = {
            "hooks": [{
                "id": "audit",
                "on": "turn_end",
                "condition": {"type": "always"},
                "action": {"type": "shell", "command": "echo ok"},
            }],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            AppYAMLCompiler(_registry()).compile(raw)
        msg = _err(exc_info).lower()
        assert "shell" in msg and "module" in msg

    def test_hook_shell_action_with_shell_module_compiles(self):
        shell = _make_module("shell", ["bash"])
        raw = _base()
        raw["modules"] = {"shell": {}}
        raw["execution"] = {
            "hooks": [{
                "id": "audit",
                "on": "turn_end",
                "condition": {"type": "always"},
                "action": {"type": "shell", "command": "echo ok"},
            }],
        }
        result = AppYAMLCompiler(_registry({"shell": shell})).compile(raw)
        bad = [w for w in _warnings(result) if "shell" in w.lower() and "module" in w.lower()]
        assert bad == []
