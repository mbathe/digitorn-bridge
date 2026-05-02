"""Phase 2 tests for the ``flow:`` block.

The flow block describes the explicit graph of how an app's agents and
tools chain together: who runs first, what comes next, when to branch,
when to gate on a human, when to fan-out and join.

Validation contract (compile-time):

  - Every flow has a unique ``id`` and an ``entry`` that points to a
    declared node.
  - Every ``nodes[].id`` is unique within the flow.
  - Every ``routes[].to`` resolves to a declared node id (or the literal
    sentinel ``end`` for terminal flows).
  - Every ``agent`` node references a declared ``agents[].id``.
  - Every ``tool`` node references a real ``module.action``.
  - Every ``parallel`` node has at least two branches and a ``join``.
  - Every ``approval`` node has a ``message``.
  - Every ``decision`` node has an ``expr``.
  - No orphan nodes (every non-entry node is reachable from entry).
  - Cycles require an explicit ``max_iterations`` cap on at least one
    node in the cycle so the runtime cannot loop forever.

These tests drive the implementation in
``packages/digitorn/core/app/flow.py``.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from digitorn.core.app.compiler import AppYAMLCompiler, CompiledApp
from digitorn.core.app.errors import AppCompilationError


# ═══════════════════════════════════════════════════════════════════════
# Helpers (mirror of test_app_compiler_validation.py)
# ═══════════════════════════════════════════════════════════════════════


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
    registry = {}
    for n in actions:
        e = MagicMock()
        e.params_model = None
        e.spec = MagicMock()
        e.spec.name = n
        e.spec.params = []
        registry[n] = e
    module._action_registry = registry
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
    """Minimal app with two agents that flows can reference."""
    return {
        "app": {"app_id": "test-flow", "name": "Test"},
        "agents": [
            {"id": "triage", "brain": {"provider": "ollama", "model": "llama3"}},
            {"id": "writer", "brain": {"provider": "ollama", "model": "llama3"}},
        ],
    }


def _err(exc_info: pytest.ExceptionInfo[AppCompilationError]) -> str:
    return "\n".join(str(e) for e in exc_info.value.errors)


def _compile(raw: dict, modules: dict[str, Any] | None = None) -> CompiledApp:
    return AppYAMLCompiler(_registry(modules or {})).compile(raw)


# ═══════════════════════════════════════════════════════════════════════
# 1. Top-level shape - flow block is optional and well-formed
# ═══════════════════════════════════════════════════════════════════════


class TestFlowShape:

    def test_app_without_flow_compiles(self):
        result = _compile(_base())
        assert isinstance(result, CompiledApp)
        assert getattr(result, "flow", None) is None

    def test_flow_with_single_terminal_compiles(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "done",
            "nodes": [
                {"id": "done", "type": "terminal"},
            ],
        }
        result = _compile(raw)
        assert result.flow is not None
        assert result.flow.id == "main"
        assert result.flow.entry == "done"
        assert len(result.flow.nodes) == 1

    def test_flow_missing_id_caught(self):
        raw = _base()
        raw["flow"] = {
            "entry": "done",
            "nodes": [{"id": "done", "type": "terminal"}],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        assert "id" in _err(exc_info).lower()

    def test_flow_missing_entry_caught(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "nodes": [{"id": "done", "type": "terminal"}],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        assert "entry" in _err(exc_info).lower()

    def test_flow_empty_nodes_caught(self):
        raw = _base()
        raw["flow"] = {"id": "main", "entry": "x", "nodes": []}
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        msg = _err(exc_info).lower()
        assert "nodes" in msg


# ═══════════════════════════════════════════════════════════════════════
# 2. Reference integrity
# ═══════════════════════════════════════════════════════════════════════


class TestFlowReferences:

    def test_entry_pointing_to_unknown_node_caught(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "ghost",
            "nodes": [{"id": "real", "type": "terminal"}],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        msg = _err(exc_info).lower()
        assert "ghost" in msg or "entry" in msg

    def test_duplicate_node_id_caught(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "step",
            "nodes": [
                {"id": "step", "type": "terminal"},
                {"id": "step", "type": "terminal"},
            ],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        msg = _err(exc_info).lower()
        assert "step" in msg and ("duplicate" in msg or "unique" in msg)

    def test_route_to_unknown_node_caught(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "start",
            "nodes": [
                {
                    "id": "start",
                    "type": "agent",
                    "agent": "triage",
                    "routes": [{"to": "ghost"}],
                },
                {"id": "done", "type": "terminal"},
            ],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        msg = _err(exc_info).lower()
        assert "ghost" in msg

    def test_route_to_end_sentinel_compiles(self):
        """``to: end`` is a valid sentinel meaning 'terminate the flow'."""
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "start",
            "nodes": [
                {
                    "id": "start",
                    "type": "agent",
                    "agent": "triage",
                    "routes": [{"to": "end"}],
                },
            ],
        }
        result = _compile(raw)
        assert isinstance(result, CompiledApp)


# ═══════════════════════════════════════════════════════════════════════
# 3. Per-node-type validation
# ═══════════════════════════════════════════════════════════════════════


class TestAgentNode:

    def test_agent_node_unknown_agent_caught(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "step",
            "nodes": [
                {
                    "id": "step",
                    "type": "agent",
                    "agent": "ghost_agent",
                    "routes": [{"to": "end"}],
                },
            ],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        msg = _err(exc_info).lower()
        assert "ghost_agent" in msg

    def test_agent_node_known_agent_compiles(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "step",
            "nodes": [
                {
                    "id": "step",
                    "type": "agent",
                    "agent": "writer",
                    "routes": [{"to": "end"}],
                },
            ],
        }
        result = _compile(raw)
        assert isinstance(result, CompiledApp)


class TestToolNode:

    def test_tool_node_unknown_tool_caught(self):
        web = _make_module("web", ["search", "fetch"])
        raw = _base()
        raw["modules"] = {"web": {}}
        raw["flow"] = {
            "id": "main",
            "entry": "step",
            "nodes": [
                {
                    "id": "step",
                    "type": "tool",
                    "tool": "web.serch",
                    "routes": [{"to": "end"}],
                },
            ],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw, modules={"web": web})
        msg = _err(exc_info).lower()
        assert "web.serch" in msg or "tool" in msg

    def test_tool_node_known_tool_compiles(self):
        web = _make_module("web", ["search", "fetch"])
        raw = _base()
        raw["modules"] = {"web": {}}
        raw["flow"] = {
            "id": "main",
            "entry": "step",
            "nodes": [
                {
                    "id": "step",
                    "type": "tool",
                    "tool": "web.search",
                    "routes": [{"to": "end"}],
                },
            ],
        }
        result = _compile(raw, modules={"web": web})
        assert isinstance(result, CompiledApp)


class TestParallelNode:

    def test_parallel_without_branches_caught(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "fan",
            "nodes": [
                {"id": "fan", "type": "parallel", "branches": [], "join": {"type": "all"}},
            ],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        msg = _err(exc_info).lower()
        assert "branches" in msg

    def test_parallel_branch_to_unknown_caught(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "fan",
            "nodes": [
                {
                    "id": "fan",
                    "type": "parallel",
                    "branches": [{"to": "a"}, {"to": "ghost"}],
                    "join": {"type": "all"},
                    "routes": [{"to": "end"}],
                },
                {"id": "a", "type": "agent", "agent": "writer", "routes": [{"to": "end"}]},
            ],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        msg = _err(exc_info).lower()
        assert "ghost" in msg

    def test_parallel_valid_compiles(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "fan",
            "nodes": [
                {
                    "id": "fan",
                    "type": "parallel",
                    "branches": [{"to": "a"}, {"to": "b"}],
                    "join": {"type": "all", "timeout": 60},
                    "routes": [{"to": "end"}],
                },
                {"id": "a", "type": "agent", "agent": "triage", "routes": [{"to": "end"}]},
                {"id": "b", "type": "agent", "agent": "writer", "routes": [{"to": "end"}]},
            ],
        }
        result = _compile(raw)
        assert isinstance(result, CompiledApp)

    def test_parallel_join_invalid_type_caught(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "fan",
            "nodes": [
                {
                    "id": "fan",
                    "type": "parallel",
                    "branches": [{"to": "a"}, {"to": "b"}],
                    "join": {"type": "ohno"},
                },
                {"id": "a", "type": "terminal"},
                {"id": "b", "type": "terminal"},
            ],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        msg = _err(exc_info).lower()
        assert "join" in msg or "ohno" in msg


class TestApprovalNode:

    def test_approval_without_message_caught(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "gate",
            "nodes": [
                {"id": "gate", "type": "approval", "routes": [{"to": "end"}]},
            ],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        msg = _err(exc_info).lower()
        assert "message" in msg

    def test_approval_with_message_compiles(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "gate",
            "nodes": [
                {
                    "id": "gate",
                    "type": "approval",
                    "message": "Confirm refund?",
                    "routes": [{"to": "end"}],
                },
            ],
        }
        result = _compile(raw)
        assert isinstance(result, CompiledApp)


class TestDecisionNode:

    def test_decision_without_expr_caught(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "pick",
            "nodes": [
                {"id": "pick", "type": "decision", "routes": [{"to": "end"}]},
            ],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        msg = _err(exc_info).lower()
        assert "expr" in msg or "decision" in msg

    def test_decision_with_expr_compiles(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "pick",
            "nodes": [
                {
                    "id": "pick",
                    "type": "decision",
                    "expr": "input.kind",
                    "routes": [
                        {"when": "refund", "to": "a"},
                        {"when": "default", "to": "end"},
                    ],
                },
                {"id": "a", "type": "terminal"},
            ],
        }
        result = _compile(raw)
        assert isinstance(result, CompiledApp)


# ═══════════════════════════════════════════════════════════════════════
# 4. Reachability
# ═══════════════════════════════════════════════════════════════════════


class TestReachability:

    def test_unreachable_node_caught(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "start",
            "nodes": [
                {"id": "start", "type": "agent", "agent": "triage", "routes": [{"to": "end"}]},
                {"id": "orphan", "type": "agent", "agent": "writer", "routes": [{"to": "end"}]},
            ],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        msg = _err(exc_info).lower()
        assert "orphan" in msg or "unreachable" in msg

    def test_reachable_via_parallel_branch_compiles(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "fan",
            "nodes": [
                {
                    "id": "fan",
                    "type": "parallel",
                    "branches": [{"to": "a"}, {"to": "b"}],
                    "join": {"type": "all"},
                    "routes": [{"to": "end"}],
                },
                {"id": "a", "type": "terminal"},
                {"id": "b", "type": "terminal"},
            ],
        }
        result = _compile(raw)
        assert isinstance(result, CompiledApp)


# ═══════════════════════════════════════════════════════════════════════
# 5. Loop safety
# ═══════════════════════════════════════════════════════════════════════


class TestLoopSafety:

    def test_cycle_without_max_iterations_caught(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "a",
            "nodes": [
                {"id": "a", "type": "agent", "agent": "triage", "routes": [{"to": "b"}]},
                {"id": "b", "type": "agent", "agent": "writer", "routes": [{"to": "a"}]},
            ],
        }
        with pytest.raises(AppCompilationError) as exc_info:
            _compile(raw)
        msg = _err(exc_info).lower()
        assert "cycle" in msg or "loop" in msg or "iterations" in msg

    def test_cycle_with_max_iterations_compiles(self):
        raw = _base()
        raw["flow"] = {
            "id": "main",
            "entry": "a",
            "max_iterations": 10,
            "nodes": [
                {"id": "a", "type": "agent", "agent": "triage", "routes": [{"to": "b"}]},
                {
                    "id": "b",
                    "type": "decision",
                    # Phase 9: decision.expr is evaluated against the
                    # FLOW context. Use a path rooted in state/input/etc.
                    "expr": "state.should_loop",
                    "routes": [
                        {"when": "yes", "to": "a"},
                        {"when": "default", "to": "end"},
                    ],
                },
            ],
        }
        result = _compile(raw)
        assert isinstance(result, CompiledApp)
