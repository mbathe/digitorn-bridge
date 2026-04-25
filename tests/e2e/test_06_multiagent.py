"""E2E Tests: Multi-agent — spawn, delegate, parallel agents."""

import pytest
from tests.e2e.conftest import *


class TestAgentSpawn:
    def test_spawn_explore(self, client, ensure_deployed):
        r = client.chat("Spawn an explore agent to find all Python files that contain 'class.*Module'.")
        assert_success(r)
        assert_used_tools(r)

    def test_spawn_worker(self, client, ensure_deployed):
        r = client.chat("Spawn a worker agent to read README.md and summarize it.")
        assert_success(r)
        assert_used_tools(r)

    def test_spawn_multiple(self, client, ensure_deployed):
        r = client.chat(
            "I need you to analyze the project. Spawn 2 agents in parallel: "
            "one to read pyproject.toml and one to list the modules directory."
        )
        assert_success(r)
        assert_used_tools(r)

    def test_agent_list(self, client, ensure_deployed):
        sid = f"e2e-spawn-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat("Spawn an explore agent to find all YAML files.", session_id=sid)
        assert_success(r1)
        r2 = client.chat("List all spawned agents.", session_id=sid)
        assert_success(r2)
        assert_used_tools(r2)
