"""Advanced e2e tests for the Memory Module.

These tests simulate REAL multi-turn agent interactions with tool calls,
memory management, compaction, and session resume.

Each test tells a story — a realistic scenario that exercises the memory
system end-to-end through the actual agent_loop.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from digitorn.modules.llm_provider.providers.base import (
    ChatMessage,
    ChatResponse,
    ProviderCapabilities,
    ProviderInfo,
    TokenUsage,
)


# ═══════════════════════════════════════════════════════════════════════
# Scripted Mock LLM — simulates an agent that uses tools
# ═══════════════════════════════════════════════════════════════════════


class ScriptedLLM:
    """Mock LLM that plays a scripted sequence of responses.

    Each response can be text, a tool call, or both.
    Captures every system prompt it receives for assertions.
    """

    def __init__(self, script: list[dict]):
        """
        script: list of dicts with:
          - {"text": "..."} for text responses
          - {"tool": "name", "args": {...}} for tool calls
          - {"text": "...", "tool": "name", "args": {...}} for both
        """
        self.provider_id = "scripted"
        self.model = "scripted-model"
        self.api_key = ""
        self.base_url = None
        self.timeout = 120.0
        self.max_retries = 2
        self.default_params: dict[str, Any] = {}
        self._script = list(script)
        self._call_index = 0
        self.system_prompts: list[str] = []
        self.all_messages: list[list[dict]] = []

    async def initialize(self):
        pass

    async def chat(self, messages, **kwargs):
        # Capture what was sent
        system = ""
        for m in messages:
            if m.role == "system":
                system = m.content
                break
        self.system_prompts.append(system)
        self.all_messages.append([{"role": m.role, "content": m.content[:200]} for m in messages])

        if self._call_index >= len(self._script):
            return ChatResponse(
                content="Done.",
                model="scripted",
                finish_reason="end_turn",
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                tool_calls=None,
                raw={},
            )

        step = self._script[self._call_index]
        self._call_index += 1

        text = step.get("text", "")
        tool_calls = None

        if "tool" in step:
            tool_calls = [{
                "id": f"call_{self._call_index}",
                "type": "function",
                "function": {
                    "name": step["tool"],
                    "arguments": step.get("args", {}),
                },
            }]

        return ChatResponse(
            content=text,
            model="scripted",
            finish_reason="tool_use" if tool_calls else "end_turn",
            usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            tool_calls=tool_calls,
            raw={},
        )

    def get_info(self):
        return ProviderInfo(
            provider_id="scripted",
            backend="mock",
            model="scripted",
            capabilities=ProviderCapabilities(tool_use=True),
            extra={},
        )

    async def close(self):
        pass


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


_MEMORY_APP_YAML = """
    app:
      app_id: memory-adv-test
      name: "Memory Advanced Test"

    modules:
      memory:
        config:
          working_memory: true
          todo_list: true
          semantic:
            vector: true
          episodic: true
          runtime:
            goal_guardian: true
            content_cache: true
      hello: {{}}
      filesystem: {{}}

    agents:
      - id: assistant
        role: assistant
        brain:
          provider: scripted
          model: scripted
          backend: openai_compat
        system_prompt: "You are a methodical coding assistant."

    execution:
      mode: {mode}

    capabilities:
      default_policy: auto
"""


async def _bootstrap_app(tmp_path: Path, provider, mode="one_shot"):
    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.loader import load_modules
    from digitorn.core.runtime.bootstrap import bootstrap
    from digitorn.modules.registry import ModuleRegistry

    yaml_path = tmp_path / "app.yaml"
    yaml_path.write_text(textwrap.dedent(_MEMORY_APP_YAML.format(mode=mode)))

    registry = ModuleRegistry()
    load_modules(registry, load_all=True)

    compiler = AppYAMLCompiler(registry)
    compiled = compiler.compile_file(yaml_path)

    with patch(
        "digitorn.core.runtime.bootstrap._resolve_provider",
        return_value=provider,
    ):
        boot_result = await bootstrap(compiled, registry)

    return boot_result


async def _run_turn(boot_result, provider, message, session_id=None):
    from digitorn.core.runtime.agent_loop import agent_turn

    ctx = boot_result["contexts"]["assistant"]
    if session_id:
        ctx.session_id = session_id

    messages = [
        {"role": "system", "content": ctx.system_prompt},
        {"role": "user", "content": message},
    ]

    result = await agent_turn(ctx, messages, max_turns=10, timeout=15.0)
    return result, ctx


# ═══════════════════════════════════════════════════════════════════════
# Scenario 1: Agent sets goal → plans → creates todos → executes → completes
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scenario_methodical_agent(tmp_path):
    """An agent that follows the memory workflow: goal → plan → todos → work → done."""

    provider = ScriptedLLM([
        # Turn 1: Agent sets goal and plan
        {
            "text": "I'll fix the auth bug. Let me set up my plan.",
            "tool": "execute_tool",
            "args": {"name": "memory.set_goal", "params": {"goal": "Fix NullPointerError in auth.py"}},
        },
        # Turn 2: Agent creates todos (after set_goal returns)
        {
            "text": "Setting up my task list.",
            "tool": "execute_tool",
            "args": {"name": "memory.add_todo", "params": {"content": "Read auth.py and locate the bug"}},
        },
        # Turn 3: Agent starts working
        {
            "text": "Reading the file now.",
            "tool": "execute_tool",
            "args": {"name": "memory.update_todo", "params": {"todo_id": "t1", "status": "in_progress"}},
        },
        # Turn 4: Agent completes the task
        {
            "text": "Found the bug on line 42.",
            "tool": "execute_tool",
            "args": {
                "name": "memory.update_todo",
                "params": {"todo_id": "t1", "status": "done", "notes": "NullPtr on line 42"},
            },
        },
        # Turn 5: Final response
        {
            "text": "I found the bug! It's a NullPointerError on line 42 of auth.py. "
                    "The `user` variable is accessed before null check.",
        },
    ])

    boot_result = await _bootstrap_app(tmp_path, provider)
    result, ctx = await _run_turn(boot_result, provider, "Fix the auth bug in auth.py")

    # Verify memory state
    mem = ctx.memory_module
    assert mem is not None
    store = mem.store

    # Goal was set
    assert store.working.goal == "Fix NullPointerError in auth.py"

    # Todo was completed with notes
    assert len(store.working.todos) == 1
    assert store.working.todos[0].status.value == "done"
    assert "line 42" in store.working.todos[0].notes

    # The LLM received memory instructions
    assert any("Memory System" in sp for sp in provider.system_prompts)

    # The LLM saw the memory snapshot in subsequent turns
    # (after set_goal, the snapshot should contain the goal)
    found_goal_in_prompt = False
    for sp in provider.system_prompts[1:]:  # skip first (before any memory action)
        if "Fix NullPointerError" in sp:
            found_goal_in_prompt = True
            break
    assert found_goal_in_prompt, "Goal should appear in system prompt after set_goal"


# ═══════════════════════════════════════════════════════════════════════
# Scenario 2: Agent receives full progress snapshot on todo update
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scenario_progress_visibility(tmp_path):
    """When agent updates a todo, it receives the FULL progress snapshot."""

    provider = ScriptedLLM([
        # Set goal
        {"tool": "execute_tool", "args": {"name": "memory.set_goal", "params": {"goal": "Build API"}}},
        # Add 3 todos
        {"tool": "execute_tool", "args": {"name": "memory.add_todo", "params": {"content": "Design endpoints"}}},
        {"tool": "execute_tool", "args": {"name": "memory.add_todo", "params": {"content": "Write code"}}},
        {"tool": "execute_tool", "args": {"name": "memory.add_todo", "params": {"content": "Write tests"}}},
        # Complete first
        {"tool": "execute_tool", "args": {"name": "memory.update_todo", "params": {"todo_id": "t1", "status": "done"}}},
        # Final
        {"text": "First task done, 2 remaining."},
    ])

    boot_result = await _bootstrap_app(tmp_path, provider)
    result, ctx = await _run_turn(boot_result, provider, "Build an API")

    store = ctx.memory_module.store
    progress = store.working.get_progress()
    assert progress["total"] == 3
    assert progress["done"] == 1
    assert progress["percent"] == 33


# ═══════════════════════════════════════════════════════════════════════
# Scenario 3: Session resume — agent picks up where it left off
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scenario_session_resume(tmp_path):
    """Agent resumes a session and sees its previous memory state."""

    # First session: set goal and complete 1 of 2 tasks
    provider1 = ScriptedLLM([
        {"tool": "execute_tool", "args": {"name": "memory.set_goal", "params": {"goal": "Deploy app"}}},
        {"tool": "execute_tool", "args": {"name": "memory.add_todo", "params": {"content": "Build Docker image"}}},
        {"tool": "execute_tool", "args": {"name": "memory.add_todo", "params": {"content": "Push to registry"}}},
        {"tool": "execute_tool", "args": {"name": "memory.update_todo", "params": {"todo_id": "t1", "status": "done"}}},
        {"text": "Docker image built. I'll push next."},
    ])

    boot_result = await _bootstrap_app(tmp_path, provider1, mode="conversation")
    _, ctx1 = await _run_turn(boot_result, provider1, "Deploy the app", session_id="ses_deploy")

    # Verify state after first session
    mem = ctx1.memory_module
    store = mem.get_session_store("ses_deploy")
    assert store.working.goal == "Deploy app"
    assert store.working.get_progress()["done"] == 1

    # Second session: RESUME with same session_id
    provider2 = ScriptedLLM([
        {"text": "I see I was deploying. Let me push to the registry now."},
    ])

    # Re-bootstrap with new provider but same memory module persists
    ctx2 = boot_result["contexts"]["assistant"]
    ctx2.session_id = "ses_deploy"

    messages = [
        {"role": "system", "content": ctx2.system_prompt},
        {"role": "user", "content": "Continue please"},
    ]

    # Manually trigger turn_start hook to simulate resume
    from digitorn.modules.memory.hooks import on_turn_start
    on_turn_start(mem, messages, turn=0, session_id="ses_deploy")

    # Check that the resume notice was injected
    resume_msgs = [m for m in messages if "SESSION RESUMED" in m.get("content", "")]
    assert len(resume_msgs) == 1
    assert "Deploy app" in resume_msgs[0]["content"]
    assert "1/2 tasks done" in resume_msgs[0]["content"]

    # Check that the memory snapshot is in the system prompt
    system_content = messages[0]["content"]
    assert "Deploy app" in system_content
    assert "Build Docker image" in system_content


# ═══════════════════════════════════════════════════════════════════════
# Scenario 4: Goal guardian catches drift
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scenario_goal_guardian(tmp_path):
    """Goal guardian reminds the agent when it drifts from its objective."""
    from digitorn.modules.memory.module import MemoryModule
    from digitorn.modules.memory.store import MemoryConfig, MemoryStore
    from digitorn.modules.memory.hooks import on_turn_end

    mem = MemoryModule()
    mem._config = MemoryConfig(working_memory=True, runtime_goal_guardian=True)
    mem._default_store = MemoryStore(mem._config)
    mem._default_store.semantic = mem._app_semantic
    mem.store.working.goal = "Fix authentication bug"

    # Simulate 3 turns of off-topic messages (no mention of "authentication" or "bug")
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "What's the weather?"},
        {"role": "assistant", "content": "I don't have weather data."},
        {"role": "user", "content": "Tell me a joke."},
        {"role": "assistant", "content": "Why did the chicken cross the road?"},
        {"role": "user", "content": "What about CSS?"},
        {"role": "assistant", "content": "CSS is a styling language."},
    ]

    on_turn_end(mem, messages, turn=3, tool_calls_this_turn=[])

    # Should have injected a goal reminder
    reminder_msgs = [m for m in messages if "Reminder" in m.get("content", "")]
    assert len(reminder_msgs) == 1
    assert "Fix authentication bug" in reminder_msgs[0]["content"]


# ═══════════════════════════════════════════════════════════════════════
# Scenario 5: Content cache — agent doesn't re-read files
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scenario_content_cache(tmp_path):
    """Tool results are cached; active entities visible in memory snapshot."""
    from digitorn.modules.memory.module import MemoryModule
    from digitorn.modules.memory.store import MemoryConfig, MemoryStore
    from digitorn.modules.memory.hooks import on_tool_result
    from digitorn.modules.base import ActionResult

    mem = MemoryModule()
    mem._config = MemoryConfig(working_memory=True, runtime_content_cache=True)
    mem._default_store = MemoryStore(mem._config)
    mem._default_store.semantic = mem._app_semantic

    # Simulate filesystem.read result
    mock_result = ActionResult(
        success=True,
        data={"content": "class AuthManager:\n    def login(self):\n        pass\n" * 10},
    )
    on_tool_result(mem, "filesystem.read", {"path": "/app/auth.py"}, mock_result)

    # File should be cached
    assert "/app/auth.py" in mem.store.working.content_cache
    cached = mem.store.working.content_cache["/app/auth.py"]
    assert cached.size > 0
    assert cached.content_hash  # has a hash

    # File should appear in active entities
    assert "/app/auth.py" in mem.store.working.active_entities

    # Memory snapshot should show the cached file
    snapshot = mem.store.render_full_snapshot()
    assert "/app/auth.py" in snapshot


# ═══════════════════════════════════════════════════════════════════════
# Scenario 6: Session isolation under concurrent users
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scenario_concurrent_sessions(tmp_path):
    """Two users working simultaneously don't see each other's memory."""
    from digitorn.modules.memory.module import MemoryModule
    from digitorn.modules.memory.store import MemoryConfig, MemoryStore
    from digitorn.modules.base import ExecutionContext

    mem = MemoryModule()
    mem._config = MemoryConfig(working_memory=True, todo_list=True, semantic_vector=True)
    mem._default_store = MemoryStore(mem._config)
    mem._default_store.semantic = mem._app_semantic
    ctx = ExecutionContext(plan_id="test", action_id="test")

    # User A: working on auth
    mem.set_active_session("user_alice")
    await mem.execute("set_goal", {"goal": "Fix auth module"}, ctx)
    await mem.execute("add_todo", {"content": "Patch auth.py"}, ctx)
    await mem.execute("add_fact", {"content": "Project uses FastAPI"}, ctx)  # shared

    # User B: working on frontend
    mem.set_active_session("user_bob")
    await mem.execute("set_goal", {"goal": "Build dashboard UI"}, ctx)
    await mem.execute("add_todo", {"content": "Create React components"}, ctx)

    # Verify isolation
    mem.set_active_session("user_alice")
    alice_snapshot = mem.store.render_full_snapshot()
    assert "Fix auth module" in alice_snapshot
    assert "Patch auth.py" in alice_snapshot
    assert "Build dashboard" not in alice_snapshot  # Bob's goal is NOT here
    assert "React" not in alice_snapshot  # Bob's todo is NOT here

    mem.set_active_session("user_bob")
    bob_snapshot = mem.store.render_full_snapshot()
    assert "Build dashboard UI" in bob_snapshot
    assert "React" in bob_snapshot
    assert "Fix auth" not in bob_snapshot  # Alice's goal is NOT here

    # But shared facts are visible to both
    assert "FastAPI" in alice_snapshot  # shared semantic fact
    mem.set_active_session("user_bob")
    bob_snapshot2 = mem.store.render_full_snapshot()
    # Bob needs semantic_vector enabled to see facts in snapshot
    # (it's enabled in config, so it should be there)
    assert "FastAPI" in bob_snapshot2


# ═══════════════════════════════════════════════════════════════════════
# Scenario 7: Memory survives compaction
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scenario_compaction_survival(tmp_path):
    """After compaction, the agent's memory is fully restored in the context reminder."""
    from digitorn.modules.memory.module import MemoryModule
    from digitorn.modules.memory.store import MemoryConfig, MemoryStore
    from digitorn.modules.memory.hooks import on_compaction
    from digitorn.modules.base import ExecutionContext

    mem = MemoryModule()
    mem._config = MemoryConfig(
        working_memory=True, todo_list=True,
        semantic_vector=True, episodic=True,
    )
    mem._default_store = MemoryStore(mem._config)
    mem._default_store.semantic = mem._app_semantic
    ctx = ExecutionContext(plan_id="test", action_id="test")

    # Build rich memory state
    await mem.execute("set_goal", {"goal": "Migrate database from SQLite to PostgreSQL"}, ctx)
    await mem.execute("set_plan", {"steps": [
        "Export SQLite data",
        "Create PostgreSQL schema",
        "Import data",
        "Update connection config",
        "Run integration tests",
    ]}, ctx)
    await mem.execute("add_todo", {"content": "Export SQLite data"}, ctx)
    await mem.execute("add_todo", {"content": "Create PG schema"}, ctx)
    await mem.execute("add_todo", {"content": "Import data"}, ctx)
    await mem.execute("update_todo", {"todo_id": "t1", "status": "done", "notes": "Exported to dump.sql"}, ctx)
    await mem.execute("update_todo", {"todo_id": "t2", "status": "in_progress"}, ctx)
    await mem.execute("add_fact", {"content": "SQLite DB has 15 tables, 2.3GB data"}, ctx)
    await mem.execute("add_fact", {"content": "PostgreSQL runs on port 5433"}, ctx)
    await mem.execute("track_entity", {"name": "dump.sql", "summary": "Full SQLite export, 2.3GB"}, ctx)

    # Simulate compaction — this is what gets reinjected
    reinjected = on_compaction(mem, [])

    # EVERYTHING must survive compaction
    assert "Migrate database from SQLite to PostgreSQL" in reinjected  # goal
    assert "Export SQLite data" in reinjected  # plan step
    assert "Create PostgreSQL schema" in reinjected  # plan step
    assert "✅" in reinjected  # completed todo
    assert "🔄" in reinjected  # in-progress todo
    assert "Exported to dump.sql" in reinjected  # todo notes
    assert "15 tables" in reinjected  # semantic fact
    assert "port 5433" in reinjected  # semantic fact
    assert "dump.sql" in reinjected  # active entity
    assert "Memory System" in reinjected  # instructions
    assert "set_goal" in reinjected  # tool reminders


# ═══════════════════════════════════════════════════════════════════════
# Scenario 8: Async events → memory persistence
# ═══════════════════════════════════════════════════════════════════════


class _FakeCtx:
    """Minimal AgentContext mock with memory_module."""
    def __init__(self, memory_module):
        self.memory_module = memory_module


@pytest.mark.asyncio
async def test_watcher_notification_persists_in_memory():
    """Watcher update → auto key_fact in working memory."""
    from digitorn.core.runtime.agent_loop import _persist_notification_to_memory
    from digitorn.modules.memory.module import MemoryModule
    from digitorn.modules.memory.store import MemoryConfig, MemoryStore

    mem = MemoryModule()
    mem._config = MemoryConfig(working_memory=True)
    mem._default_store = MemoryStore(mem._config)
    mem._default_store.semantic = mem._app_semantic

    ctx = _FakeCtx(mem)

    _persist_notification_to_memory(ctx, {
        "type": "watcher",
        "label": "API Health",
        "check_number": 42,
        "strategy": "on_change",
        "result": {"status": 503, "body": "Service Unavailable"},
        "error": "",
    })

    facts = mem.store.working.key_facts
    assert len(facts) == 1
    assert "API Health" in facts[0]
    assert "42" in facts[0]


@pytest.mark.asyncio
async def test_watcher_error_persists():
    """Watcher error → key_fact with ERROR marker."""
    from digitorn.core.runtime.agent_loop import _persist_notification_to_memory
    from digitorn.modules.memory.module import MemoryModule
    from digitorn.modules.memory.store import MemoryConfig, MemoryStore

    mem = MemoryModule()
    mem._config = MemoryConfig(working_memory=True)
    mem._default_store = MemoryStore(mem._config)
    mem._default_store.semantic = mem._app_semantic
    ctx = _FakeCtx(mem)

    _persist_notification_to_memory(ctx, {
        "type": "watcher",
        "label": "DB monitor",
        "check_number": 5,
        "error": "Connection refused",
    })

    assert "ERROR" in mem.store.working.key_facts[0]
    assert "Connection refused" in mem.store.working.key_facts[0]


@pytest.mark.asyncio
async def test_remember_creates_todo():
    """Scheduled job (remember) → auto todo in working memory."""
    from digitorn.core.runtime.agent_loop import _persist_notification_to_memory
    from digitorn.modules.memory.module import MemoryModule
    from digitorn.modules.memory.store import MemoryConfig, MemoryStore

    mem = MemoryModule()
    mem._config = MemoryConfig(working_memory=True, todo_list=True)
    mem._default_store = MemoryStore(mem._config)
    mem._default_store.semantic = mem._app_semantic
    ctx = _FakeCtx(mem)

    _persist_notification_to_memory(ctx, {
        "type": "scheduled_job",
        "label": "Remember: vérifier le déploiement",
        "memory_context": "vérifier le déploiement",
        "prompt": "vérifier le déploiement",
        "action_type": "llm_prompt",
    })

    # Should create a todo
    assert len(mem.store.working.todos) == 1
    assert "REMINDER" in mem.store.working.todos[0].content
    assert "déploiement" in mem.store.working.todos[0].content


@pytest.mark.asyncio
async def test_background_task_persists():
    """Background task result → key_fact with result summary."""
    from digitorn.core.runtime.agent_loop import _persist_notification_to_memory
    from digitorn.modules.memory.module import MemoryModule
    from digitorn.modules.memory.store import MemoryConfig, MemoryStore

    mem = MemoryModule()
    mem._config = MemoryConfig(working_memory=True)
    mem._default_store = MemoryStore(mem._config)
    mem._default_store.semantic = mem._app_semantic
    ctx = _FakeCtx(mem)

    _persist_notification_to_memory(ctx, {
        "task_id": "task_abc",
        "tool_name": "shell.run",
        "status": "completed",
        "elapsed_seconds": 12.5,
        "result": {"exit_code": 0, "stdout": "42 passed, 3 failed"},
    })

    facts = mem.store.working.key_facts
    assert len(facts) == 1
    assert "shell.run" in facts[0]
    assert "42 passed" in facts[0]


@pytest.mark.asyncio
async def test_background_failure_persists():
    """Background task failure → key_fact with FAILED marker."""
    from digitorn.core.runtime.agent_loop import _persist_notification_to_memory
    from digitorn.modules.memory.module import MemoryModule
    from digitorn.modules.memory.store import MemoryConfig, MemoryStore

    mem = MemoryModule()
    mem._config = MemoryConfig(working_memory=True)
    mem._default_store = MemoryStore(mem._config)
    mem._default_store.semantic = mem._app_semantic
    ctx = _FakeCtx(mem)

    _persist_notification_to_memory(ctx, {
        "task_id": "task_xyz",
        "tool_name": "http.get",
        "status": "failed",
        "error": "Connection timeout after 30s",
    })

    assert "FAILED" in mem.store.working.key_facts[0]
    assert "timeout" in mem.store.working.key_facts[0]


@pytest.mark.asyncio
async def test_no_memory_module_no_crash():
    """Without memory module, notifications are silently ignored."""
    from digitorn.core.runtime.agent_loop import _persist_notification_to_memory

    ctx = _FakeCtx(None)
    # Should not crash
    _persist_notification_to_memory(ctx, {
        "type": "watcher",
        "label": "test",
    })


@pytest.mark.asyncio
async def test_key_facts_bounded():
    """Key facts list is bounded to prevent memory bloat."""
    from digitorn.core.runtime.agent_loop import _persist_notification_to_memory
    from digitorn.modules.memory.module import MemoryModule
    from digitorn.modules.memory.store import MemoryConfig, MemoryStore

    mem = MemoryModule()
    mem._config = MemoryConfig(working_memory=True)
    mem._default_store = MemoryStore(mem._config)
    mem._default_store.semantic = mem._app_semantic
    ctx = _FakeCtx(mem)

    # Push 20 notifications
    for i in range(20):
        _persist_notification_to_memory(ctx, {
            "type": "watcher",
            "label": f"Check #{i}",
            "check_number": i,
            "result": f"ok_{i}",
        })

    # Should be bounded to 15
    assert len(mem.store.working.key_facts) <= 15
    # Most recent should be the last ones
    assert "ok_19" in mem.store.working.key_facts[-1]
