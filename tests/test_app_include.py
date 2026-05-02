"""Phase 2 tests for the ``include:`` block (fragmentation).

A real Digitorn app is rarely a single 1000-line YAML. Teams want to
split the agents, the prompts, the hooks, the behavior rules into
their own files in a conventional layout. The ``include:`` block makes
the composition explicit and predictable.

Hybrid pattern (validated with the user 2026-05-01):

  - **Convention auto-load**: when ``./agents/*.yaml``, ``./hooks/*.yaml``,
    ``./behavior/rules.yaml`` exist next to ``app.yaml`` they are
    automatically merged into the relevant top-level fields.
  - **Explicit override**: an ``include:`` block in ``app.yaml`` can
    pin specific files instead of (or in addition to) the conventions.

Composition rules:

  - List fields (agents, hooks, skills) are CONCATENATED. Inline entries
    in app.yaml come first, then fragments in alphabetical filename order.
  - Dict fields (modules, behavior config) are MERGED, with inline
    winning on conflict.
  - Duplicate ids (e.g. two agents with id 'foo') fail at compile.
  - Fragments are validated as a piece of an AppDefinition - they don't
    repeat the ``app:`` block, they only carry their own slice.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from digitorn.core.app.compiler import AppYAMLCompiler, CompiledApp
from digitorn.core.app.errors import AppCompilationError


# ─── Fixtures ───────────────────────────────────────────────────


def _registry(modules: dict[str, Any] | None = None):
    r = MagicMock()
    modules = modules or {}
    r.list_available.return_value = list(modules.keys())
    r.get = MagicMock(side_effect=lambda mid: modules[mid] if mid in modules else (_ for _ in ()).throw(Exception(f"no {mid}")))
    r.create = r.get
    r.is_available = MagicMock(side_effect=lambda mid: mid in modules)
    return r


def _write(path: Path, body: dict | list | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, str):
        path.write_text(body, encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def _compiler(modules: dict[str, Any] | None = None) -> AppYAMLCompiler:
    return AppYAMLCompiler(_registry(modules or {}))


# ─── 1. Convention auto-load ───────────────────────────────────


class TestConventionAutoLoad:
    """Sub-files in standard locations are merged automatically."""

    def test_agents_directory_auto_loaded(self, tmp_path: Path):
        app_dir = tmp_path / "app"
        _write(app_dir / "app.yaml", {
            "app": {"app_id": "test", "name": "T"},
            # Note: NO agents in app.yaml - they live entirely in agents/
        })
        _write(app_dir / "agents" / "main.yaml", {
            "id": "main",
            "brain": {"provider": "ollama", "model": "llama3"},
        })
        _write(app_dir / "agents" / "helper.yaml", {
            "id": "helper",
            "brain": {"provider": "ollama", "model": "llama3"},
        })
        result = _compiler().compile_file(app_dir / "app.yaml")
        ids = {a.agent_id for a in result.agents}
        assert ids == {"main", "helper"}

    def test_hooks_directory_auto_loaded(self, tmp_path: Path):
        app_dir = tmp_path / "app"
        _write(app_dir / "app.yaml", {
            "app": {"app_id": "test", "name": "T"},
            "agents": [{
                "id": "main",
                "brain": {"provider": "ollama", "model": "llama3"},
            }],
        })
        _write(app_dir / "hooks" / "audit.yaml", [
            {
                "id": "audit_all",
                "on": "tool_end",
                "condition": {"type": "always"},
                "action": {"type": "log", "message": "x"},
            },
        ])
        result = _compiler().compile_file(app_dir / "app.yaml")
        hook_ids = [h.id for h in result.execution.hooks]
        assert "audit_all" in hook_ids

    def test_inline_plus_fragments_concatenated(self, tmp_path: Path):
        app_dir = tmp_path / "app"
        _write(app_dir / "app.yaml", {
            "app": {"app_id": "test", "name": "T"},
            "agents": [
                {"id": "inline", "brain": {"provider": "ollama", "model": "llama3"}},
            ],
        })
        _write(app_dir / "agents" / "fragment.yaml", {
            "id": "from_file",
            "brain": {"provider": "ollama", "model": "llama3"},
        })
        result = _compiler().compile_file(app_dir / "app.yaml")
        ids = [a.agent_id for a in result.agents]
        # inline wins on order. Fragment loaded after.
        assert "inline" in ids and "from_file" in ids

    def test_no_subdir_compiles_as_before(self, tmp_path: Path):
        """No agents/ dir => behaviour unchanged from Phase 1."""
        app_dir = tmp_path / "app"
        _write(app_dir / "app.yaml", {
            "app": {"app_id": "test", "name": "T"},
            "agents": [{
                "id": "main",
                "brain": {"provider": "ollama", "model": "llama3"},
            }],
        })
        result = _compiler().compile_file(app_dir / "app.yaml")
        assert {a.agent_id for a in result.agents} == {"main"}


# ─── 2. Explicit include override ──────────────────────────────


class TestExplicitInclude:
    """The ``include:`` block points at specific files or dirs."""

    def test_include_specific_file(self, tmp_path: Path):
        app_dir = tmp_path / "app"
        _write(app_dir / "app.yaml", {
            "app": {"app_id": "test", "name": "T"},
            "include": {"agents": ["./roster/triage.yaml"]},
        })
        _write(app_dir / "roster" / "triage.yaml", {
            "id": "triage",
            "brain": {"provider": "ollama", "model": "llama3"},
        })
        result = _compiler().compile_file(app_dir / "app.yaml")
        assert {a.agent_id for a in result.agents} == {"triage"}

    def test_include_directory_explicitly(self, tmp_path: Path):
        app_dir = tmp_path / "app"
        _write(app_dir / "app.yaml", {
            "app": {"app_id": "test", "name": "T"},
            "include": {"agents": "./pool/"},
        })
        _write(app_dir / "pool" / "a.yaml", {
            "id": "a",
            "brain": {"provider": "ollama", "model": "llama3"},
        })
        _write(app_dir / "pool" / "b.yaml", {
            "id": "b",
            "brain": {"provider": "ollama", "model": "llama3"},
        })
        result = _compiler().compile_file(app_dir / "app.yaml")
        assert {a.agent_id for a in result.agents} == {"a", "b"}

    def test_include_unknown_path_caught(self, tmp_path: Path):
        app_dir = tmp_path / "app"
        _write(app_dir / "app.yaml", {
            "app": {"app_id": "test", "name": "T"},
            "agents": [{"id": "main", "brain": {"provider": "ollama", "model": "llama3"}}],
            "include": {"agents": "./missing/"},
        })
        with pytest.raises(AppCompilationError) as exc_info:
            _compiler().compile_file(app_dir / "app.yaml")
        msg = "\n".join(str(e) for e in exc_info.value.errors)
        assert "missing" in msg.lower() or "include" in msg.lower()


# ─── 3. Conflict detection ─────────────────────────────────────


class TestConflictDetection:

    def test_duplicate_agent_id_across_fragments_caught(self, tmp_path: Path):
        app_dir = tmp_path / "app"
        _write(app_dir / "app.yaml", {
            "app": {"app_id": "test", "name": "T"},
        })
        _write(app_dir / "agents" / "a.yaml", {
            "id": "shared",
            "brain": {"provider": "ollama", "model": "llama3"},
        })
        _write(app_dir / "agents" / "b.yaml", {
            "id": "shared",
            "brain": {"provider": "ollama", "model": "llama3"},
        })
        with pytest.raises(AppCompilationError) as exc_info:
            _compiler().compile_file(app_dir / "app.yaml")
        msg = "\n".join(str(e) for e in exc_info.value.errors).lower()
        assert "shared" in msg and ("duplicate" in msg or "unique" in msg)


# ─── 4. Prompt files referenced by agents ──────────────────────


class TestBundleReload:
    """Fragments must round-trip through a bundle: a fragmented app
    compiled from disk, then re-compiled via asset_loader (no source
    dir), must yield the same set of agents. Otherwise the daemon's
    reload-from-DB path silently loses every fragment-declared agent."""

    def test_fragments_recompile_via_asset_loader(self, tmp_path: Path):
        from digitorn.core.app.compiler import AppYAMLCompiler

        app_dir = tmp_path / "app"
        _write(app_dir / "app.yaml", {"app": {"app_id": "test", "name": "T"}})
        _write(app_dir / "agents" / "main.yaml", {
            "id": "main",
            "brain": {"provider": "ollama", "model": "llama3"},
        })
        _write(app_dir / "agents" / "helper.yaml", {
            "id": "helper",
            "brain": {"provider": "ollama", "model": "llama3"},
        })

        # 1. Source-tree compile: should pick up both fragments and
        # collect them into the bundle assets.
        compiler = _compiler()
        first = compiler.compile_file(app_dir / "app.yaml")
        assert {a.agent_id for a in first.agents} == {"main", "helper"}
        assert "agents/main.yaml" in first.collected_assets
        assert "agents/helper.yaml" in first.collected_assets

        # 2. Bundle-reload compile: pretend we're recompiling from the
        # bundle by feeding the collected assets back through an
        # asset_loader that exposes list_dir. The recompile must see
        # the same agents.
        assets = dict(first.collected_assets)

        def _loader(rel: str) -> str | None:
            return assets.get(rel)

        def _list_dir(rel_dir: str) -> list[str]:
            prefix = rel_dir.rstrip("/") + "/"
            return sorted(
                k for k in assets
                if k.startswith(prefix) and "/" not in k[len(prefix):]
                and (k.endswith(".yaml") or k.endswith(".yml"))
            )

        _loader.list_dir = _list_dir  # type: ignore[attr-defined]

        compiler2 = _compiler()
        second = compiler2.compile_string(
            first.raw_yaml,
            source="bundle://test/abc",
            asset_loader=_loader,
        )
        assert {a.agent_id for a in second.agents} == {"main", "helper"}


class TestPromptFiles:
    """An agent fragment can reference ``./prompts/<name>.md`` via
    the ``skills:`` field, the loader resolves it relative to app dir."""

    def test_agent_with_skill_path_loads_prompt(self, tmp_path: Path):
        app_dir = tmp_path / "app"
        _write(app_dir / "app.yaml", {
            "app": {"app_id": "test", "name": "T"},
        })
        _write(app_dir / "agents" / "main.yaml", {
            "id": "main",
            "brain": {"provider": "ollama", "model": "llama3"},
            "skills": "./prompts/main.md",
        })
        _write(app_dir / "prompts" / "main.md", "# Main agent\n\nDo the thing.\n")
        result = _compiler().compile_file(app_dir / "app.yaml")
        agent = result.agents[0]
        assert "Do the thing" in agent.skills_content
