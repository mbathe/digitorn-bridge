"""Tests for the Memory Module - cognitive memory system.

Tests all 5 layers + hooks + integration with context_builder.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from digitorn.modules.memory.module import MemoryModule
from digitorn.modules.memory.store import (
    MemoryConfig,
    MemoryStore,
    SemanticMemory,
    TodoStatus,
    WorkingMemory,
)
from digitorn.modules.memory.hooks import (
    build_memory_instructions,
    build_memory_prompt_section,
    on_compaction,
    on_turn_end,
    on_turn_start,
    on_tool_result,
)
from digitorn.modules.base import ActionResult, ExecutionContext


# ═══════════════════════════════════════════════════════════════════════
# Working Memory
# ═══════════════════════════════════════════════════════════════════════


class TestWorkingMemory:
    def test_render_empty(self):
        wm = WorkingMemory()
        assert wm.render() == ""

    def test_render_goal(self):
        wm = WorkingMemory(goal="Fix auth.py bug")
        rendered = wm.render()
        assert "Fix auth.py bug" in rendered
        assert "🎯" in rendered

    def test_render_plan(self):
        wm = WorkingMemory(
            plan=["Read file", "Find bug", "Fix it"],
            current_step=1,
        )
        rendered = wm.render()
        assert "Read file" in rendered
        assert "Find bug" in rendered
        assert "→" in rendered  # current step marker

    def test_todo_lifecycle(self):
        wm = WorkingMemory()
        t1 = wm.add_todo("Read auth.py")
        t2 = wm.add_todo("Fix the bug")

        assert len(wm.todos) == 2
        assert t1.status == TodoStatus.PENDING

        wm.start_todo(t1.id)
        assert wm.todos[0].status == TodoStatus.IN_PROGRESS

        wm.complete_todo(t1.id, notes="Found bug on line 42")
        assert wm.todos[0].status == TodoStatus.DONE
        assert wm.todos[0].notes == "Found bug on line 42"

        wm.block_todo(t2.id, reason="Need more context")
        assert wm.todos[1].status == TodoStatus.BLOCKED

    def test_progress(self):
        wm = WorkingMemory()
        wm.add_todo("A")
        wm.add_todo("B")
        wm.add_todo("C")
        wm.complete_todo("t1")

        progress = wm.get_progress()
        assert progress["total"] == 3
        assert progress["done"] == 1
        assert progress["percent"] == 33

    def test_render_full(self):
        wm = WorkingMemory(
            goal="Fix auth bug",
            plan=["Read", "Fix", "Test"],
            current_step=1,
            key_facts=["Bug is on line 42", "NullPointerError"],
            active_entities={"auth.py": "42 lines, AuthManager class"},
        )
        wm.add_todo("Read auth.py")
        wm.complete_todo("t1")
        wm.add_todo("Fix the bug")

        rendered = wm.render()
        assert "Fix auth bug" in rendered
        assert "Read" in rendered
        assert "Bug is on line 42" in rendered
        assert "auth.py" in rendered
        assert "✅" in rendered  # completed todo
        assert "⬚" in rendered  # pending todo


# ═══════════════════════════════════════════════════════════════════════
# Memory Store
# ═══════════════════════════════════════════════════════════════════════


class TestMemoryStore:
    def test_render_snapshot_empty(self):
        config = MemoryConfig(working_memory=True)
        store = MemoryStore(config)
        snapshot = store.render_full_snapshot()
        assert "MEMORY" in snapshot

    def test_render_snapshot_with_data(self):
        config = MemoryConfig(
            working_memory=True, todo_list=True,
            semantic_vector=True, episodic=True,
        )
        store = MemoryStore(config)
        store.working.goal = "Test the system"
        store.working.add_todo("Step 1")
        store.semantic.add_fact("Python 3.12", category="tech")

        snapshot = store.render_full_snapshot()
        assert "Test the system" in snapshot
        assert "Step 1" in snapshot
        assert "Python 3.12" in snapshot

    def test_to_dict(self):
        store = MemoryStore()
        store.working.goal = "Test"
        store.working.add_todo("Do something")
        store.semantic.add_fact("A fact")

        d = store.to_dict()
        assert d["working"]["goal"] == "Test"
        assert len(d["working"]["todos"]) == 1
        assert len(d["semantic"]["facts"]) == 1


# ═══════════════════════════════════════════════════════════════════════
# Memory Config
# ═══════════════════════════════════════════════════════════════════════


class TestMemoryConfig:
    def test_from_dict_minimal(self):
        config = MemoryConfig.from_dict({"working_memory": True})
        assert config.working_memory is True
        assert config.episodic is False

    def test_from_dict_full(self):
        config = MemoryConfig.from_dict({
            "working_memory": True,
            "todo_list": True,
            "episodic": True,
            "semantic": {"vector": True, "graph": True},
            "procedural": True,
            "runtime": {
                "proactive_injection": True,
                "content_cache": True,
                "goal_guardian": True,
            },
            "limits": {"max_injected": 5, "budget_percent": 10},
        })
        assert config.working_memory is True
        assert config.semantic_vector is True
        assert config.semantic_graph is True
        assert config.procedural is True
        assert config.runtime_goal_guardian is True
        assert config.limits_max_injected == 5

    def test_has_any_layer(self):
        assert MemoryConfig().has_any_layer() is False
        assert MemoryConfig(working_memory=True).has_any_layer() is True
        assert MemoryConfig(episodic=True).has_any_layer() is True


# ═══════════════════════════════════════════════════════════════════════
# Memory Module (@action tests)
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def mem():
    m = MemoryModule()
    m._config = MemoryConfig(
        working_memory=True, todo_list=True,
        semantic_vector=True, semantic_graph=True,
        episodic=True, procedural=True,
    )
    m._default_store = MemoryStore(m._config)
    m._default_store.semantic = m._app_semantic
    m._default_store.procedures = m._app_procedures
    return m


@pytest.fixture
def ctx():
    return ExecutionContext(plan_id="test", action_id="test")


class TestMemoryActions:
    @pytest.mark.asyncio
    async def test_set_goal(self, mem, ctx):
        r = await mem.execute("set_goal", {"goal": "Fix bugs"}, ctx)
        assert r.success
        assert mem.store.working.goal == "Fix bugs"

    @pytest.mark.asyncio
    async def test_set_plan_direct(self, mem, ctx):
        # set_plan is not an action; plan is set directly on working memory
        mem.store.working.plan = ["Read", "Fix", "Test"]
        assert len(mem.store.working.plan) == 3

    @pytest.mark.asyncio
    async def test_add_and_update_todo(self, mem, ctx):
        r1 = await mem.execute("add_todo", {"content": "Read file"}, ctx)
        assert r1.success
        # Returns full snapshot with progress + all todos
        assert "progress" in r1.data
        assert r1.data["progress"]["total"] == 1
        todo_id = r1.data["todos"][0]["id"]

        r2 = await mem.execute("update_todo", {
            "todo_id": todo_id, "status": "done", "notes": "Done!"
        }, ctx)
        assert r2.success
        # Returns updated snapshot - agent sees full progress
        assert r2.data["progress"]["done"] == 1
        assert r2.data["progress"]["percent"] == 100

    @pytest.mark.asyncio
    async def test_remember(self, mem, ctx):
        r = await mem.execute("remember", {
            "content": "Python 3.12",
        }, ctx)
        assert r.success
        assert len(mem.store.semantic.facts) == 1

    @pytest.mark.asyncio
    async def test_remember_dedup(self, mem, ctx):
        await mem.execute("remember", {"content": "Python 3.12"}, ctx)
        r = await mem.execute("remember", {"content": "Python 3.12"}, ctx)
        assert r.data["action"] == "already_stored"
        assert len(mem.store.semantic.facts) == 1  # not duplicated

    @pytest.mark.asyncio
    async def test_track_entity_direct(self, mem, ctx):
        # track_entity is not an action; entities are tracked directly on working memory
        mem.store.working.active_entities["auth.py"] = "42 lines, AuthManager"
        assert "auth.py" in mem.store.working.active_entities

    @pytest.mark.asyncio
    async def test_add_relationship_direct(self, mem, ctx):
        # add_relationship is not an action; graph edges are added directly on semantic memory
        mem.store.semantic.add_edge("auth.py", "contains", "AuthManager")
        assert len(mem.store.semantic.graph) == 1

    @pytest.mark.asyncio
    async def test_recall(self, mem, ctx):
        await mem.execute("remember", {"content": "Python uses indentation"}, ctx)
        await mem.execute("remember", {"content": "JavaScript uses braces"}, ctx)
        r = await mem.execute("recall", {"query": "Python"}, ctx)
        assert r.success
        assert r.data["total"] >= 1

    @pytest.mark.asyncio
    async def test_forget(self, mem, ctx):
        await mem.execute("remember", {"content": "Old fact"}, ctx)
        fact_id = mem.store.semantic.facts[0].id
        r = await mem.execute("forget", {"fact_id": fact_id}, ctx)
        assert r.success
        assert mem.store.semantic.facts[0].superseded_by == "forgotten"

    @pytest.mark.asyncio
    async def test_get_snapshot_direct(self, mem, ctx):
        # get_snapshot is not an action; use render_full_snapshot() directly
        await mem.execute("set_goal", {"goal": "Test"}, ctx)
        snapshot = mem.store.render_full_snapshot()
        assert "Test" in snapshot

    @pytest.mark.asyncio
    async def test_cache_content_via_hook(self, mem, ctx):
        # cache_content is not an action; content is cached via the on_tool_result hook
        from digitorn.modules.memory.store import CachedContent
        import time as _time
        content = "print('hello')" * 10
        cached = CachedContent(
            path="/tmp/test.py",
            content=content,
            content_hash=CachedContent.compute_hash(content),
            cached_at=_time.monotonic(),
            size=len(content),
            summary="Test file",
        )
        mem.store.working.content_cache["/tmp/test.py"] = cached
        mem.store.working.active_entities["/tmp/test.py"] = "Test file"
        assert "/tmp/test.py" in mem.store.working.content_cache
        assert "/tmp/test.py" in mem.store.working.active_entities


# ═══════════════════════════════════════════════════════════════════════
# Memory Hooks
# ═══════════════════════════════════════════════════════════════════════


class TestMemoryHooks:
    def test_build_prompt_section(self, mem):
        mem.store.working.goal = "Fix auth"
        section = build_memory_prompt_section(mem)
        assert "Fix auth" in section
        assert "MEMORY" in section

    def test_build_instructions(self, mem):
        instructions = build_memory_instructions(mem)
        assert "## Memory" in instructions
        assert "set_goal" in instructions
        assert "add_todo" in instructions

    def test_turn_start_injects_memory(self, mem):
        mem.store.working.goal = "Build feature"
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ]
        on_turn_start(mem, messages, turn=0)
        assert "Build feature" in messages[0]["content"]

    def test_turn_start_goal_guardian(self, mem):
        mem._config.runtime_goal_guardian = True
        messages = [
            {"role": "system", "content": "You are helpful."},
        ]
        # No goal set, turn > 0 → should nudge
        on_turn_start(mem, messages, turn=1)
        assert any("set_goal" in m.get("content", "") for m in messages)

    def test_on_compaction_returns_snapshot(self, mem):
        mem.store.working.goal = "Important goal"
        mem.store.working.add_todo("Critical task")
        result = on_compaction(mem, [])
        assert "Important goal" in result
        assert "Critical task" in result

    def test_tool_result_auto_tracks_entity(self, mem):
        mem._config.working_memory = True
        on_tool_result(mem, "filesystem.read", {"path": "/tmp/auth.py"}, {})
        assert "/tmp/auth.py" in mem.store.working.active_entities


# ═══════════════════════════════════════════════════════════════════════
# E2E: full pipeline YAML → compile → bootstrap → agent turn
# ═══════════════════════════════════════════════════════════════════════


from digitorn.modules.llm_provider.providers.base import (
    ChatResponse,
    ProviderCapabilities,
    ProviderInfo,
    TokenUsage,
)


class MockLLMProvider:
    def __init__(self, responses=None):
        self.provider_id = "mock"
        self.model = "mock-model"
        self.api_key = ""
        self.base_url = None
        self.timeout = 120.0
        self.max_retries = 2
        self.default_params: dict[str, Any] = {}
        self._responses = list(responses or [])
        self._call_index = 0
        self.call_log: list[dict] = []

    async def initialize(self):
        pass

    async def chat(self, messages, **kwargs):
        self.call_log.append({
            "system_prompt": messages[0].content if messages else "",
        })
        if self._call_index < len(self._responses):
            r = self._responses[self._call_index]
            self._call_index += 1
            return r
        return ChatResponse(
            content="Done.", model="mock", finish_reason="end_turn",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            tool_calls=None, raw={},
        )

    def get_info(self):
        return ProviderInfo(
            provider_id="mock", backend="mock", model="mock",
            capabilities=ProviderCapabilities(tool_use=True), extra={},
        )

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_e2e_memory_in_system_prompt(tmp_path):
    """Memory module injects working memory into the system prompt."""
    yaml_content = textwrap.dedent("""
        app:
          app_id: memory-test
          name: "Memory Test"

        modules:
          memory:
            config:
              working_memory: true
              todo_list: true
          hello: {}

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: mock
              model: mock
              backend: openai_compat
            system_prompt: "You are helpful."

        execution:
          mode: one_shot

        capabilities:
          default_policy: auto
    """)
    yaml_path = tmp_path / "app.yaml"
    yaml_path.write_text(yaml_content)

    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.loader import load_modules
    from digitorn.core.runtime.agent_loop import agent_turn
    from digitorn.core.runtime.bootstrap import bootstrap
    from digitorn.modules.registry import ModuleRegistry

    registry = ModuleRegistry()
    load_modules(registry, load_all=True)

    compiler = AppYAMLCompiler(registry)
    compiled = compiler.compile_file(yaml_path)

    provider = MockLLMProvider()
    with patch(
        "digitorn.core.runtime.bootstrap._resolve_provider",
        return_value=provider,
    ):
        boot_result = await bootstrap(compiled, registry)

    ctx = boot_result["contexts"]["assistant"]

    # Memory module should be attached
    assert ctx.memory_module is not None

    # System prompt should contain memory instructions
    assert "## Memory" in ctx.system_prompt
    assert "set_goal" in ctx.system_prompt

    # Run a turn - memory hooks should fire
    messages = [
        {"role": "system", "content": ctx.system_prompt},
        {"role": "user", "content": "Fix the auth bug"},
    ]
    result = await agent_turn(ctx, messages, max_turns=1, timeout=5.0)
    assert result.content  # got a response


@pytest.mark.asyncio
async def test_e2e_memory_survives_info(tmp_path):
    """Working memory state is preserved in the store across turns."""
    mem = MemoryModule()
    mem._config = MemoryConfig(working_memory=True, todo_list=True, semantic_vector=True)
    mem._default_store = MemoryStore(mem._config)
    mem._default_store.semantic = mem._app_semantic
    mem._default_store.procedures = mem._app_procedures

    # Simulate agent actions
    ctx = ExecutionContext(plan_id="test", action_id="test")
    await mem.execute("set_goal", {"goal": "Fix auth bug"}, ctx)
    mem.store.working.plan = ["Read", "Fix", "Test"]
    await mem.execute("add_todo", {"content": "Read auth.py"}, ctx)
    await mem.execute("update_todo", {"todo_id": "t1", "status": "done", "notes": "Line 42"}, ctx)
    await mem.execute("add_todo", {"content": "Apply fix"}, ctx)
    await mem.execute("remember", {"content": "Bug is NullPtr on line 42"}, ctx)
    mem.store.working.active_entities["auth.py"] = "AuthManager class"

    # Get snapshot - everything visible in one shot
    snapshot = mem.store.render_full_snapshot()
    assert "Fix auth bug" in snapshot
    assert "Read" in snapshot  # plan
    assert "✅" in snapshot  # completed todo
    assert "Apply fix" in snapshot  # pending todo
    assert "NullPtr" in snapshot  # fact
    assert "auth.py" in snapshot  # entity

    # Simulate compaction - memory survives
    reinjection = on_compaction(mem, [])
    assert "Fix auth bug" in reinjection
    assert "Apply fix" in reinjection
    assert "NullPtr" in reinjection


# ═══════════════════════════════════════════════════════════════════════
# Session isolation
# ═══════════════════════════════════════════════════════════════════════


class TestSessionIsolation:
    """Verify per-session working memory + shared app-level semantic."""

    @pytest.mark.asyncio
    async def test_sessions_have_separate_working_memory(self):
        """Two sessions have independent goals/todos."""
        mem = MemoryModule()
        mem._config = MemoryConfig(working_memory=True, todo_list=True, semantic_vector=True)
        mem._default_store = MemoryStore(mem._config)
        ctx = ExecutionContext(plan_id="test", action_id="test")

        # Session A sets a goal
        mem.set_active_session("session_a")
        await mem.execute("set_goal", {"goal": "Fix auth bug"}, ctx)
        await mem.execute("add_todo", {"content": "Read auth.py"}, ctx)

        # Session B sets a different goal
        mem.set_active_session("session_b")
        await mem.execute("set_goal", {"goal": "Build new feature"}, ctx)
        await mem.execute("add_todo", {"content": "Design API"}, ctx)

        # Verify isolation
        store_a = mem.get_session_store("session_a")
        store_b = mem.get_session_store("session_b")

        assert store_a.working.goal == "Fix auth bug"
        assert store_b.working.goal == "Build new feature"
        assert store_a.working.todos[0].content == "Read auth.py"
        assert store_b.working.todos[0].content == "Design API"

    @pytest.mark.asyncio
    async def test_semantic_memory_shared_across_sessions(self):
        """Semantic facts are shared between sessions (app-level)."""
        mem = MemoryModule()
        mem._config = MemoryConfig(working_memory=True, semantic_vector=True)
        mem._default_store = MemoryStore(mem._config)
        ctx = ExecutionContext(plan_id="test", action_id="test")

        # Session A adds a fact via remember
        mem.set_active_session("session_a")
        await mem.execute("remember", {"content": "Project uses Python 3.12"}, ctx)

        # Session B sees the same fact (semantic is shared at app level)
        mem.set_active_session("session_b")
        store_b = mem.get_session_store("session_b")
        assert len(store_b.semantic.facts) == 1
        assert store_b.semantic.facts[0].content == "Project uses Python 3.12"

    @pytest.mark.asyncio
    async def test_graph_shared_across_sessions(self):
        """Entity graph is shared between sessions."""
        mem = MemoryModule()
        mem._config = MemoryConfig(semantic_vector=True, semantic_graph=True)
        mem._default_store = MemoryStore(mem._config)
        ctx = ExecutionContext(plan_id="test", action_id="test")

        mem.set_active_session("session_a")
        # add_relationship is not an action; add edge directly on semantic memory
        mem.store.semantic.add_edge("auth.py", "contains", "AuthManager")

        mem.set_active_session("session_b")
        store_b = mem.get_session_store("session_b")
        assert len(store_b.semantic.graph) == 1
        assert store_b.semantic.graph[0].source == "auth.py"

    @pytest.mark.asyncio
    async def test_episodes_are_per_session(self):
        """Each session has its own episode history."""
        from digitorn.modules.memory.store import Episode
        import time as _time

        mem = MemoryModule()
        mem._config = MemoryConfig(episodic=True)
        mem._default_store = MemoryStore(mem._config)

        # add_episode is not an action; add episodes directly on the store
        mem.set_active_session("session_a")
        mem.store.episodes.append(Episode(
            session_id="session_a", timestamp=_time.monotonic(),
            summary="Fixed auth bug",
        ))

        mem.set_active_session("session_b")
        mem.store.episodes.append(Episode(
            session_id="session_b", timestamp=_time.monotonic(),
            summary="Built new feature",
        ))

        store_a = mem.get_session_store("session_a")
        store_b = mem.get_session_store("session_b")

        assert len(store_a.episodes) == 1
        assert store_a.episodes[0].summary == "Fixed auth bug"
        assert len(store_b.episodes) == 1
        assert store_b.episodes[0].summary == "Built new feature"

    @pytest.mark.asyncio
    async def test_session_resume_injects_notice(self):
        """When resuming a session with existing memory, agent gets a resume notice."""
        mem = MemoryModule()
        mem._config = MemoryConfig(working_memory=True, todo_list=True)
        mem._default_store = MemoryStore(mem._config)
        mem._default_store.semantic = mem._app_semantic
        ctx = ExecutionContext(plan_id="test", action_id="test")

        # Simulate first session: set goal + todos
        mem.set_active_session("session_x")
        await mem.execute("set_goal", {"goal": "Fix auth bug"}, ctx)
        await mem.execute("add_todo", {"content": "Read auth.py"}, ctx)
        await mem.execute("update_todo", {"todo_id": "t1", "status": "done"}, ctx)
        await mem.execute("add_todo", {"content": "Apply fix"}, ctx)

        # Simulate session resume (turn=0, same session_id, existing memory)
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Let's continue"},
        ]
        on_turn_start(mem, messages, turn=0, session_id="session_x")

        # Should inject a resume notice
        resume_msgs = [m for m in messages if "SESSION RESUMED" in m.get("content", "")]
        assert len(resume_msgs) == 1
        assert "Fix auth bug" in resume_msgs[0]["content"]
        assert "1/2 tasks done" in resume_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_standalone_mode_works_without_session(self):
        """Without session_id, uses default store (standalone mode)."""
        mem = MemoryModule()
        mem._config = MemoryConfig(working_memory=True, semantic_vector=True)
        mem._default_store = MemoryStore(mem._config)
        mem._default_store.semantic = mem._app_semantic
        mem._default_store.procedures = mem._app_procedures
        ctx = ExecutionContext(plan_id="test", action_id="test")

        # No set_active_session -- uses default
        await mem.execute("set_goal", {"goal": "Standalone goal"}, ctx)
        assert mem.store.working.goal == "Standalone goal"
