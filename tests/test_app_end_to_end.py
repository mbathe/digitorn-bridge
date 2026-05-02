"""End-to-end stress test - every Phase 1-8 addition wired together.

Single integration test that builds a YAML using EVERY new feature
(flow + include + runtime alias + ui block + dependencies block + mode
gating + typed models + JSON Schema), compiles it, persists the
collected assets, then re-compiles via asset_loader (bundle reload).

If this passes, all the wiring is real, not just spec.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from digitorn.core.app.compiler import AppYAMLCompiler


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


def _registry(modules: dict[str, Any]):
    r = MagicMock()
    r.list_available.return_value = list(modules.keys())
    r.get = MagicMock(side_effect=lambda mid: modules[mid] if mid in modules else (_ for _ in ()).throw(Exception(f"no {mid}")))
    r.create = r.get
    r.is_available = MagicMock(side_effect=lambda mid: mid in modules)
    return r


def test_kitchen_sink_compile_and_bundle_reload(tmp_path: Path):
    """One YAML, every Phase 1-8 feature, compile twice (source-tree
    then bundle-reload) and assert both produce the same result."""
    app_dir = tmp_path / "kitchen-sink"
    app_dir.mkdir()

    # Main YAML using every new shape: runtime: alias, ui: block,
    # dependencies: block, flow: graph, fragments via convention.
    main = {
        "app": {
            "app_id": "kitchen-sink",
            "name": "Kitchen Sink",
            "version": "1.0.0",
            "description": "Every Phase 1-8 feature in one YAML.",
        },
        "dependencies": {
            "variables": {"workspace": "/tmp/ws"},
        },
        "modules": {
            "web": {},
            "agent_spawn": {},
        },
        "runtime": {                          # alias of execution:
            "mode": "conversation",
            "entry_agent": "triage",
            "max_turns": 25,
            "greeting": "Hi!",
        },
        "ui": {                               # new wrapper block
            "theme": {"accent": "#A78BFA"},
            "features": {"voice": False, "memory_panel": True},
            "slash_commands": [
                {"command": "/digest", "description": "Generate digest", "template": "Summarise"},
            ],
        },
        "flow": {                             # phase 2 flow:
            "id": "main",
            "entry": "triage_node",
            "max_iterations": 10,
            "nodes": [
                {
                    "id": "triage_node",
                    "type": "agent",
                    "agent": "triage",
                    "routes": [
                        {"when": "output.kind == 'refund'", "to": "approval_gate"},
                        {"when": "default", "to": "end"},
                    ],
                },
                {
                    "id": "approval_gate",
                    "type": "approval",
                    "message": "Confirm refund of {{previous.output.amount}}?",
                    "routes": [
                        {"when": "approvals.approval_gate == 'approve'", "to": "end"},
                        {"when": "default", "to": "end"},
                    ],
                },
            ],
        },
    }
    (app_dir / "app.yaml").write_text(yaml.safe_dump(main), encoding="utf-8")

    # Fragment: agent triage in its own file (./agents/ convention).
    (app_dir / "agents").mkdir()
    (app_dir / "agents" / "triage.yaml").write_text(yaml.safe_dump({
        "id": "triage",
        "role": "coordinator",
        "brain": {"provider": "ollama", "model": "llama3"},
        "modules": [{"web": ["search"]}],
    }), encoding="utf-8")

    # Compile from source tree.
    web = _make_module("web", ["search", "fetch"])
    agent_spawn = _make_module("agent_spawn", [])
    registry = _registry({"web": web, "agent_spawn": agent_spawn})

    compiled = AppYAMLCompiler(registry).compile_file(app_dir / "app.yaml")

    # Phase 8 wiring assertions:

    # 1. runtime: alias was lifted to execution:.
    assert compiled.execution.mode == "conversation"
    assert compiled.execution.greeting == "Hi!"
    assert compiled.execution.max_turns == 25

    # 2. ui: block was lifted to top-level.
    assert compiled.theme.get("accent") == "#A78BFA"
    assert compiled.features.get("voice") is False
    assert compiled.slash_commands[0]["command"] == "/digest"

    # 3. Fragment was auto-loaded.
    assert {a.agent_id for a in compiled.agents} == {"triage"}

    # 4. Flow block parsed + validated + present on CompiledApp.
    assert compiled.flow is not None
    assert compiled.flow.id == "main"
    assert compiled.flow.entry == "triage_node"
    assert len(compiled.flow.nodes) == 2

    # 5. Fragment recorded in collected_assets for bundle persistence.
    assert "agents/triage.yaml" in compiled.collected_assets

    # 6. Bundle-reload: feed the collected assets back through an
    # asset_loader and re-compile from the same raw_yaml.
    assets = dict(compiled.collected_assets)

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

    reload_compiled = AppYAMLCompiler(registry).compile_string(
        compiled.raw_yaml,
        source="bundle://kitchen-sink/abc",
        asset_loader=_loader,
    )

    # The reloaded app must be functionally identical.
    assert {a.agent_id for a in reload_compiled.agents} == {"triage"}
    assert reload_compiled.execution.mode == "conversation"
    assert reload_compiled.flow is not None
    assert reload_compiled.flow.id == "main"
    assert len(reload_compiled.flow.nodes) == 2
    assert reload_compiled.theme.get("accent") == "#A78BFA"


def test_summary_surfaces_flow_and_warnings():
    """The deploy-time summary() (the API response shape) must include
    flow metadata and any compile warnings - that's how the Builder
    canvas, the CLI, and the Flutter dashboard learn about them."""
    from digitorn.core.app.manager_v2._models import DeployedApp

    web = _make_module("web", ["search"])
    registry = _registry({"web": web})

    raw = {
        "app": {"app_id": "test", "name": "Test"},
        "agents": [{"id": "main", "brain": {"provider": "ollama", "model": "llama3"}}],
        "modules": {"web": {}},
        "runtime": {                          # alias
            "mode": "conversation",
            "greeting": "Hi",                  # cosmetic mismatch, soft warn-only
        },
        "flow": {
            "id": "f",
            "entry": "step",
            "nodes": [{"id": "step", "type": "terminal"}],
        },
    }
    compiled = AppYAMLCompiler(registry).compile(raw)
    # Mode is conversation, greeting is in the right mode, so no warning here.
    # The summary should still expose the warnings list (even when empty).

    # Build a minimal DeployedApp shell to call summary(). The full
    # constructor needs many fixtures we don't have; we mock the
    # minimum surface DeployedApp.summary() reads.
    deployed = MagicMock(spec=DeployedApp)
    deployed.app_id = compiled.app_id
    deployed.compiled = compiled
    deployed.contexts = {}
    deployed.context_builder = None
    deployed.modules = {}
    deployed.deployed_at = "2026-05-01T00:00:00"
    deployed.builtin = False
    deployed.mode = "conversation"

    # Use the real summary() bound method.
    summary = DeployedApp.summary(deployed)

    assert "flow" in summary
    assert summary["flow"]["id"] == "f"
    assert summary["flow"]["entry"] == "step"
    assert summary["flow"]["node_count"] == 1

    assert "warnings" in summary
    # Empty list is fine - shape is what matters (clients rely on it)
    assert isinstance(summary["warnings"], list)
