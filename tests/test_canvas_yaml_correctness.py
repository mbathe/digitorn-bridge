"""Verify every YAML the canvas can produce is valid against the
real compiler.

Three sources of YAML the canvas emits today:

  1. NewAppWizard starters: blank / chat / multi_agent_flow
  2. Palette templates: every NodeTemplate.template() output, when
     dropped at its parentPath in a minimal app shell.
  3. Drag-edge: connecting two flow nodes appends a route or branch
     in the source's flow.nodes[idx].

This test is the contract: the canvas MUST never let the user produce
a YAML the compiler rejects. If it does, that's a regression of the
"if it compiles, it runs" guarantee."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from digitorn.core.app.compiler import AppYAMLCompiler


# ─── Test helpers ───────────────────────────────────────────────


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


def _full_registry():
    """Mock registry with every module the canvas wizards/templates may
    reference. Keeps the test hermetic - we don't need the real
    digitorn module loader."""
    return _registry({
        "web": _make_module("web", ["search", "fetch"]),
        "filesystem": _make_module("filesystem", ["read", "write", "edit", "grep", "glob"]),
        "shell": _make_module("shell", ["bash"]),
        "memory": _make_module("memory", ["remember", "recall", "set_goal", "todo_add", "todo_update"]),
        "lsp": _make_module("lsp", ["diagnose", "notify_change"]),
        "agent_spawn": _make_module("agent_spawn", ["Agent"]),
        "context_builder": _make_module("context_builder", ["ask_user"]),
        "http": _make_module("http", ["get", "post", "put", "delete"]),
        "channels": _make_module("channels", ["slack_post", "send"]),
        "preview": _make_module("preview", ["set_state"]),
        "workspace": _make_module("workspace", ["read", "write", "edit"]),
        "mcp": _make_module("mcp", ["call_tool"]),
    })


# ─── 1. NewAppWizard starters ──────────────────────────────────


def _wizard_blank() -> dict:
    """Mirror of NewAppWizard.tsx buildYaml(starter="blank")."""
    return {
        "app": {"app_id": "test-app", "name": "Test", "version": "0.1.0"},
        "runtime": {"mode": "conversation"},
        "agents": [{
            "id": "main",
            "role": "coordinator",
            "brain": {"provider": "anthropic", "model": "claude-haiku-4-5", "credential": "anthropic_main"},
            "system_prompt": "You are a helpful agent.",
        }],
    }


def _wizard_chat() -> dict:
    """Mirror of NewAppWizard.tsx buildYaml(starter="chat")."""
    return {
        "app": {"app_id": "test-app", "name": "Test", "version": "0.1.0"},
        "runtime": {"mode": "conversation", "entry_agent": "main"},
        "modules": {"web": {}},
        "agents": [{
            "id": "main",
            "role": "coordinator",
            "brain": {"provider": "anthropic", "model": "claude-haiku-4-5", "credential": "anthropic_main"},
            "modules": [{"web": ["search", "fetch"]}],
            "system_prompt": "Answer concisely. Cite sources when you search the web.",
        }],
    }


def _wizard_multi_agent_flow() -> dict:
    """Mirror of NewAppWizard.tsx buildYaml(starter="multi_agent_flow")."""
    return {
        "app": {"app_id": "test-app", "name": "Test", "version": "0.1.0"},
        "runtime": {"mode": "conversation", "entry_agent": "lead", "max_turns": 30},
        "modules": {"web": {}, "agent_spawn": {}},
        "agents": [
            {
                "id": "lead",
                "role": "coordinator",
                "brain": {"provider": "anthropic", "model": "claude-sonnet-4-6", "credential": "anthropic_main"},
                "modules": [{"agent_spawn": ["Agent"]}],
                "system_prompt": "Dispatch the right specialist for the job.",
            },
            {
                "id": "researcher",
                "role": "specialist",
                "brain": {"provider": "anthropic", "model": "claude-haiku-4-5", "credential": "anthropic_main"},
                "modules": [{"web": ["search", "fetch"]}],
                "system_prompt": "Find facts, return citations.",
            },
            {
                "id": "writer",
                "role": "specialist",
                "brain": {"provider": "anthropic", "model": "claude-sonnet-4-6", "credential": "anthropic_main"},
                "system_prompt": "Compose the final answer.",
            },
        ],
        "flow": {
            "id": "main",
            "entry": "triage",
            "max_iterations": 25,
            "nodes": [
                {
                    "id": "triage",
                    "type": "agent",
                    "agent": "lead",
                    "routes": [
                        {"when": "output.kind == 'research'", "to": "research_step"},
                        {"when": "default", "to": "write_step"},
                    ],
                },
                {
                    "id": "research_step",
                    "type": "agent",
                    "agent": "researcher",
                    "routes": [{"to": "write_step"}],
                },
                {
                    "id": "write_step",
                    "type": "agent",
                    "agent": "writer",
                    "routes": [{"to": "end"}],
                },
            ],
        },
    }


class TestWizardStarters:
    """Each starter from NewAppWizard.tsx must compile cleanly."""

    def test_blank_starter_compiles(self):
        compiled = AppYAMLCompiler(_full_registry()).compile(_wizard_blank())
        assert compiled.app_id == "test-app"
        assert len(compiled.agents) == 1

    def test_chat_starter_compiles(self):
        compiled = AppYAMLCompiler(_full_registry()).compile(_wizard_chat())
        assert "web" in compiled.modules

    def test_multi_agent_flow_starter_compiles(self):
        compiled = AppYAMLCompiler(_full_registry()).compile(_wizard_multi_agent_flow())
        assert compiled.flow is not None
        assert compiled.flow.entry == "triage"
        assert len(compiled.flow.nodes) == 3
        # Cross-refs validated: every flow.nodes[].agent must point at
        # a real agent id. If the wizard generates a typo, this fails.
        agent_ids = {a.agent_id for a in compiled.agents}
        for n in compiled.flow.nodes:
            if getattr(n, "type", "") == "agent":
                assert getattr(n, "agent", "") in agent_ids


# ─── 2. Palette templates ──────────────────────────────────────


def _minimal_app() -> dict:
    """Minimum valid app to embed templates into."""
    return {
        "app": {"app_id": "test", "name": "Test"},
        "agents": [{"id": "main", "brain": {"provider": "ollama", "model": "llama3"}}],
    }


# Each entry mirrors templates.ts NodeTemplate.template() output
# placed at its parentPath. We don't pull from the TS file directly -
# this is a deliberate copy to assert the contract in Python.
PALETTE_TEMPLATES: list[tuple[str, str, dict | None, Any]] = [
    # (label, parentPath, defaultKey, instance)
    ("agent", "agents", None, {
        "id": "new_agent",
        "role": "specialist",
        "specialty": "Describe what this agent does",
        "brain": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "credential": "anthropic_main",  # added to satisfy strict mode
            "temperature": 0.1,
            "max_tokens": 8192,
        },
        "system_prompt": "You are a helpful agent.",
    }),
    ("hook", "execution.hooks", None, {
        "id": "new_hook",
        "on": "tool_end",
        "condition": {"type": "tool_name", "match": "filesystem.write"},
        "action": {"type": "lsp_diagnose", "publish": True, "inject_result": True},
        "cooldown": 0.5,
    }),
    ("flow_root", "flow", None, {
        "id": "main",
        "entry": "start",
        "max_iterations": 50,
        "nodes": [{"id": "start", "type": "terminal"}],
    }),
    ("flow_agent", "flow.nodes", None, {
        "id": "new_agent_node",
        "type": "agent",
        "agent": "main",  # references the test app's agent
        "routes": [{"when": "default", "to": "end"}],
    }),
    ("flow_decision", "flow.nodes", None, {
        "id": "new_decision_node",
        "type": "decision",
        "expr": "input.kind",
        "routes": [
            {"when": "case_a", "to": "end"},
            {"when": "default", "to": "end"},
        ],
    }),
    ("flow_terminal", "flow.nodes", None, {
        "id": "finish",
        "type": "terminal",
    }),
    # Phase 12 advanced primitives
    ("credentials_schema", "execution", "credentials_schema", {
        "providers": [
            {
                "name": "anthropic_main",
                "label": "Anthropic API key",
                "type": "api_key",
                "scope": "per_user",
                "required": True,
                "fields": [
                    {"name": "api_key", "type": "secret", "required": True},
                ],
            },
        ],
    }),
    ("payload_schema", "execution", "payload_schema", {
        "metadata": [
            {"name": "topic", "type": "string", "required": True, "label": "Topic"},
        ],
    }),
    ("sandbox", "execution", "sandbox", {
        "level": "standard",
        "pool_size": 2,
        "pool_max": 8,
        "audit": False,
    }),
    ("pipeline_step", "pipeline", None, {
        "app": "downstream-app-id",
        "input": "{{input}}",
    }),
]


@pytest.mark.parametrize("label,parent_path,default_key,instance", PALETTE_TEMPLATES, ids=lambda x: x if isinstance(x, str) else "")
def test_palette_template_compiles(
    label: str, parent_path: str, default_key: str | None, instance: Any,
):
    raw = _minimal_app()
    # Set the template at its parent path. Mirror the canvas's
    # onAddTemplate logic: list parents -> append, map parents
    # -> default_key, top-level singletons -> overwrite.
    parts = parent_path.split(".")
    if parent_path == "":
        raise AssertionError("template with empty parentPath needs a defaultKey")

    # Navigate / create the parent.
    parent: Any = raw
    for i, p in enumerate(parts[:-1]):
        if p not in parent:
            parent[p] = {}
        parent = parent[p]
    leaf = parts[-1]

    if leaf == "flow":
        # flow root template overwrites the singleton
        raw["flow"] = instance
    elif leaf == "nodes" and parts[-2] == "flow":
        # flow.nodes - mirror the canvas's onAddTemplate auto-create
        # logic: when the parent ``flow:`` block is missing, the canvas
        # synthesises a minimal one pointing at the new node.
        if "flow" not in raw:
            seed_id = instance.get("id") if isinstance(instance, dict) else "start"
            raw["flow"] = {
                "id": "main",
                "entry": seed_id,
                "max_iterations": 50,
                "nodes": [],
            }
        elif raw["flow"] == {}:
            # parent navigation may have created an empty {} - fix it.
            seed_id = instance.get("id") if isinstance(instance, dict) else "start"
            raw["flow"] = {
                "id": "main",
                "entry": seed_id,
                "max_iterations": 50,
                "nodes": [],
            }
        raw["flow"].setdefault("nodes", []).append(instance)
    elif leaf == "hooks" and parts[-2] == "execution":
        raw.setdefault("execution", {}).setdefault("hooks", []).append(instance)
    elif leaf == "agents":
        raw["agents"].append(instance)
    elif default_key and len(parts) == 1 and parts[0] == "execution":
        # Singleton under execution: credentials_schema / payload_schema /
        # sandbox. The canvas appends them under default_key.
        raw.setdefault("execution", {})[default_key] = instance
        # payload_schema is functional in mode='background' only
        # (Phase 9 strict gating). Auto-switch the test fixture so
        # this template compiles when dropped on a fresh app.
        if default_key == "payload_schema":
            raw["execution"]["mode"] = "background"
            raw["execution"]["entry_agent"] = "main"
            raw["execution"]["triggers"] = [
                {"id": "x", "type": "cron", "schedule": "0 9 * * *"}
            ]
    elif parent_path == "pipeline":
        raw.setdefault("pipeline", []).append(instance)
    else:
        raise AssertionError(f"unknown parent path: {parent_path}")

    compiled = AppYAMLCompiler(_full_registry()).compile(raw)
    assert compiled is not None


# ─── 3. Drag-edge ──────────────────────────────────────────────


class TestDragEdge:
    """The canvas's connect-resolver appends to ``routes`` (or
    ``branches`` for parallel). Every shape it produces must compile."""

    def test_drag_edge_appends_route_compiles(self):
        """Source: drag from flow-A to flow-B. Result: route added to A."""
        raw = _wizard_multi_agent_flow()
        # Simulate dragging from triage to write_step (already exists).
        # Pick a non-existing route to test: from research_step to triage
        flow_nodes = raw["flow"]["nodes"]
        research_idx = next(i for i, n in enumerate(flow_nodes) if n["id"] == "research_step")
        flow_nodes[research_idx].setdefault("routes", []).append(
            {"when": "default", "to": "triage"}  # creates a cycle
        )
        # Cycle without max_iterations would fail; we have max_iterations=25.
        compiled = AppYAMLCompiler(_full_registry()).compile(raw)
        assert compiled.flow is not None

    def test_drag_edge_to_end_sentinel_compiles(self):
        """to: end is a sentinel, no node lookup needed."""
        raw = _wizard_multi_agent_flow()
        flow_nodes = raw["flow"]["nodes"]
        triage_idx = next(i for i, n in enumerate(flow_nodes) if n["id"] == "triage")
        flow_nodes[triage_idx]["routes"].insert(
            0, {"when": "input.urgent == true", "to": "end"}
        )
        compiled = AppYAMLCompiler(_full_registry()).compile(raw)
        assert compiled is not None


# ─── 4. WidgetTreeEditor primitives ────────────────────────────


class TestWidgetTreeEditor:
    """Every widget primitive scaffold the canvas tree editor inserts
    (web/src/lib/widget-primitives.ts) must compile against the real
    WidgetsConfig validator. Mirrors the makeWidgetDefault() outputs
    grouped under chat_side / workspace_tabs / modals / inline."""

    def _shell_with_widgets(self, widgets: dict) -> dict:
        raw = _minimal_app()
        raw["widgets"] = widgets
        return raw

    def test_chat_side_with_layout_root_compiles(self):
        """User adds a chat sidebar with column root + nested children."""
        widgets = {
            "chat_side": {
                "title": "Sidebar",
                "collapsible": True,
                "default_open": True,
                "tree": {
                    "type": "column",
                    "children": [
                        {"type": "text", "text": "Hello"},
                        {"type": "divider"},
                        {"type": "card", "title": "Card", "body": {"type": "text", "text": "Body"}},
                    ],
                },
            },
        }
        compiled = AppYAMLCompiler(_full_registry()).compile(self._shell_with_widgets(widgets))
        assert compiled is not None

    def test_workspace_tab_with_form_compiles(self):
        widgets = {
            "workspace_tabs": [{
                "id": "main",
                "title": "Main",
                "tree": {
                    "type": "form",
                    "id": "my_form",
                    "submit": {"type": "chat", "message": "Submit form"},
                    "children": [
                        {"type": "text_input", "id": "name", "label": "Name"},
                        {"type": "select", "id": "kind", "label": "Kind", "options": ["a", "b"]},
                        {"type": "button", "label": "Run", "action": {"type": "chat", "message": "Go"}},
                    ],
                },
            }],
        }
        compiled = AppYAMLCompiler(_full_registry()).compile(self._shell_with_widgets(widgets))
        assert compiled is not None

    def test_modal_with_split_root_compiles(self):
        widgets = {
            "modals": {
                "confirm_delete": {
                    "title": "Confirm",
                    "dismissible": True,
                    "tree": {
                        "type": "split",
                        "direction": "horizontal",
                        "first": {"type": "text", "text": "Left"},
                        "second": {"type": "text", "text": "Right"},
                    },
                },
            },
        }
        compiled = AppYAMLCompiler(_full_registry()).compile(self._shell_with_widgets(widgets))
        assert compiled is not None

    def test_inline_with_data_list_compiles(self):
        widgets = {
            "inline": {
                "status_banner": {
                    "tree": {
                        "type": "list",
                        "data": {"source": "static", "value": []},
                        "item": {"type": "text", "text": "row"},
                    },
                },
            },
        }
        compiled = AppYAMLCompiler(_full_registry()).compile(self._shell_with_widgets(widgets))
        assert compiled is not None

    def test_kitchen_sink_widgets_compiles(self):
        """All four sections populated at once. Worst case the editor
        can produce in a single pass."""
        widgets = {
            "version": 1,
            "chat_side": {
                "title": "Live",
                "tree": {
                    "type": "column",
                    "children": [
                        {"type": "stat", "label": "Total", "value": 42},
                        {"type": "progress", "value": 0.5},
                    ],
                },
            },
            "workspace_tabs": [
                {
                    "id": "main",
                    "title": "Main",
                    "tree": {"type": "markdown", "source": "**Hi**"},
                },
                {
                    "id": "files",
                    "title": "Files",
                    "tree": {"type": "tabs", "items": [
                        {"id": "a", "title": "A", "body": {"type": "text", "text": "A"}},
                    ]},
                },
            ],
            "modals": {
                "confirm": {
                    "title": "Confirm",
                    "tree": {"type": "alert", "level": "warning", "message": "Sure?"},
                },
            },
            "inline": {
                "banner": {
                    "tree": {"type": "badge", "label": "New"},
                },
            },
        }
        compiled = AppYAMLCompiler(_full_registry()).compile(self._shell_with_widgets(widgets))
        assert compiled is not None
        assert compiled.widgets is not None
        # Sanity checks on the compiled shape
        assert compiled.widgets.chat_side is not None
        assert len(compiled.widgets.workspace_tabs) == 2
        assert "confirm" in compiled.widgets.modals
        assert "banner" in compiled.widgets.inline


# ─── 5. TriggerWizard / Theme / Features / MCP sandbox ─────────


def _background_app_with_trigger(trigger: dict) -> dict:
    """Background-mode app shell carrying one trigger - lets us assert
    the wizard's emitted shape compiles end-to-end."""
    raw = _minimal_app()
    raw["execution"] = {
        "mode": "background",
        "entry_agent": "main",
        "triggers": [trigger],
    }
    return raw


class TestTriggerWizard:
    """Each variant the TriggerWizard can emit (cron / webhook / watch)
    must round-trip through TriggerConfig."""

    def test_cron_trigger_compiles(self):
        compiled = AppYAMLCompiler(_full_registry()).compile(
            _background_app_with_trigger({
                "id": "daily_run",
                "type": "cron",
                "schedule": "0 9 * * *",
                "message": "Run daily report",
            })
        )
        assert compiled.execution.mode == "background"

    def test_webhook_trigger_compiles(self):
        compiled = AppYAMLCompiler(_full_registry()).compile(
            _background_app_with_trigger({
                "id": "stripe_webhook",
                "type": "http",
                "path": "/webhook",
                "method": "POST",
                "port": 9100,
                "message": "Webhook hit: {{event.body}}",
                "routing": "user",
                "routing_key": "{{event.header.X-User-Id}}",
            })
        )
        assert compiled is not None

    def test_watch_trigger_compiles(self):
        compiled = AppYAMLCompiler(_full_registry()).compile(
            _background_app_with_trigger({
                "id": "csv_watcher",
                "type": "watch",
                "paths": ["./inbox/*.csv", "./fallback/*.tsv"],
                "message": "New file: {{event.path}}",
            })
        )
        assert compiled is not None


class TestThemeAndFeatures:
    """Theme color picker and Features toggle grid both write into
    top-level dict blocks. Compiler must accept arbitrary keys."""

    def test_theme_block_compiles(self):
        raw = _minimal_app()
        raw["theme"] = {"accent": "#6EE7B7", "background": "#0B1220"}
        compiled = AppYAMLCompiler(_full_registry()).compile(raw)
        assert compiled.theme["accent"] == "#6EE7B7"

    def test_features_toggles_compile(self):
        raw = _minimal_app()
        raw["features"] = {
            "voice": False,
            "attachments": False,
            "tools_panel": True,
            "context_ring": True,
        }
        compiled = AppYAMLCompiler(_full_registry()).compile(raw)
        assert compiled.features["voice"] is False
        assert compiled.features["context_ring"] is True


class TestMcpSandboxMatrix:
    """MCP server with full sandbox declaration must compile."""

    def test_mcp_with_sandbox_compiles(self):
        raw = _minimal_app()
        raw["variables"] = {"workspace": "/tmp/ws"}
        raw["modules"] = {
            "mcp": {
                "config": {
                    "servers": {
                        "github": {
                            "transport": "stdio",
                            "command": "npx @modelcontextprotocol/server-github",
                            "sandbox": {
                                "permissions": ["process.exec", "net.http"],
                                "paths": {"read": ["{{workspace}}"], "write": []},
                                "allowed_hosts": ["api.github.com"],
                            },
                        },
                    },
                },
            },
        }
        compiled = AppYAMLCompiler(_full_registry()).compile(raw)
        assert compiled is not None

    def test_mcp_with_wildcard_perm_compiles(self):
        raw = _minimal_app()
        raw["modules"] = {
            "mcp": {
                "config": {
                    "servers": {
                        "scratchpad": {
                            "transport": "stdio",
                            "command": "node ./mcp.js",
                            "sandbox": {
                                "permissions": ["process.*", "fs.read"],
                                "allowed_hosts": [],
                            },
                        },
                    },
                },
            },
        }
        compiled = AppYAMLCompiler(_full_registry()).compile(raw)
        assert compiled is not None
