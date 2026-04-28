"""Agent spawn module tests - covers data models and utilities."""

from __future__ import annotations

import pytest

from digitorn.modules.agent_spawn.module import AgentSpawnModule
from digitorn.modules.agent_spawn.runner import AgentResult


class TestAgentResult:
    def test_create_result(self):
        r = AgentResult(
            agent_id="a1",
            task="Test task",
            specialist=None,
            status="completed",
            content="Done",
            turns_used=5,
            duration_seconds=10.0,
            memory={},
            errors=[],
        )
        assert r.agent_id == "a1"
        assert r.status == "completed"
        assert r.turns_used == 5

    def test_failed_result(self):
        r = AgentResult(
            agent_id="a2",
            task="Failed task",
            specialist="explore",
            status="failed",
            content="",
            turns_used=1,
            duration_seconds=2.0,
            memory={},
            errors=["Connection timeout"],
        )
        assert r.status == "failed"
        assert "Connection timeout" in r.errors


class TestAgentSpawnModule:
    @pytest.fixture
    def spawn(self):
        return AgentSpawnModule()

    def test_module_id(self, spawn):
        assert spawn.MODULE_ID == "agent_spawn"

    def test_has_required_actions(self, spawn):
        actions = set(spawn._action_registry.keys())
        assert "spawn_agent" in actions
        assert "agent_status" in actions
        assert "agent_result" in actions
        assert "agent_list" in actions
        assert "agent_cancel" in actions

    def test_action_count(self, spawn):
        assert len(spawn._action_registry) >= 6

    @pytest.mark.asyncio
    async def test_agent_list_empty(self, spawn):
        r = await spawn.execute("agent_list", {})
        assert r.success
        assert r.data["agents"] == []
        assert r.data["total"] == 0

    @pytest.mark.asyncio
    async def test_agent_status_not_found(self, spawn):
        r = await spawn.execute("agent_status", {"agent_id": "nonexistent"})
        assert not r.success
        assert "not found" in r.error.lower()

    @pytest.mark.asyncio
    async def test_agent_result_not_found(self, spawn):
        r = await spawn.execute("agent_result", {"agent_id": "nonexistent"})
        assert not r.success

    @pytest.mark.asyncio
    async def test_agent_cancel_not_found(self, spawn):
        r = await spawn.execute("agent_cancel", {"agent_id": "nonexistent"})
        assert not r.success
