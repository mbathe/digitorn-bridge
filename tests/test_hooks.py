"""Tests for the internal hooks system.

Tests cover:
1. TurnState — token estimation, properties
2. Built-in conditions — context_pressure, turn_count, tool_calls, message_count, always
3. Built-in actions — compact_context (truncate/summarize), inject_message, log
4. HookRunner — evaluation, cooldown, firing, ordering
5. Compilation — YAML → CompiledHook → Hook
6. Integration — hooks in the agent loop
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.core.runtime.hooks import (
    Hook,
    HookAction,
    HookCondition,
    HookRunner,
    RuntimeHook,
    TurnState,
    compile_hooks,
    build_hook_runner,
    get_action,
    get_condition,
    register_action,
    register_condition,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_state(
    message_count: int = 10,
    turn: int = 5,
    max_turns: int = 50,
    tool_calls_count: int = 3,
    agent_id: str = "test_agent",
    char_per_message: int = 400,
    max_context_tokens: int = 128_000,
    output_reserved: int = 4096,
) -> TurnState:
    """Create a TurnState with N messages of given size."""
    messages = [
        {"role": "system", "content": "You are a test agent."},
    ]
    for i in range(message_count - 1):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({"role": role, "content": "x" * char_per_message})

    return TurnState(
        messages=messages,
        turn=turn,
        max_turns=max_turns,
        tool_calls_count=tool_calls_count,
        agent_id=agent_id,
        max_context_tokens=max_context_tokens,
        output_reserved=output_reserved,
    )


# ---------------------------------------------------------------------------
# TurnState tests
# ---------------------------------------------------------------------------


class TestTurnState:
    def test_estimated_tokens(self):
        state = _make_state(message_count=5, char_per_message=400)
        # 1 system msg (~26 chars) + 4 msgs × 400 chars = ~1626 chars / 4 ≈ 406 tokens
        assert state.estimated_tokens > 0
        assert state.estimated_tokens < 1000

    def test_estimated_tokens_cached(self):
        state = _make_state()
        first = state.estimated_tokens
        second = state.estimated_tokens
        assert first == second

    def test_message_count(self):
        state = _make_state(message_count=15)
        assert state.message_count == 15

    def test_token_pressure(self):
        state = _make_state(message_count=5, char_per_message=100)
        assert 0 < state.token_pressure < 0.01  # very small

    def test_tool_calls_in_estimation(self):
        """Tool calls in messages should be counted in token estimation."""
        messages = [
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "search_tools", "arguments": '{"query": "test"}'}}
            ]},
        ]
        state = TurnState(messages=messages, turn=0, max_turns=10, tool_calls_count=1, agent_id="a")
        assert state.estimated_tokens > 0


# ---------------------------------------------------------------------------
# Condition tests
# ---------------------------------------------------------------------------


class TestConditions:
    def test_always(self):
        fn = get_condition("always")
        assert fn is not None
        state = _make_state()
        assert fn(state, {}) is True

    def test_context_pressure_below(self):
        fn = get_condition("context_pressure")
        state = _make_state(message_count=5, char_per_message=100)
        assert fn(state, {"threshold": 0.75}) is False

    def test_context_pressure_above(self):
        fn = get_condition("context_pressure")
        # 100 messages × 4000 chars = 400,000 chars → ~100,000 tokens
        state = _make_state(message_count=100, char_per_message=4000)
        assert fn(state, {"threshold": 0.5}) is True

    def test_turn_count_threshold(self):
        fn = get_condition("turn_count")
        state = _make_state(turn=9)
        assert fn(state, {"threshold": 10}) is False
        state = _make_state(turn=10)
        assert fn(state, {"threshold": 10}) is True

    def test_turn_count_every(self):
        fn = get_condition("turn_count")
        assert fn(_make_state(turn=5), {"every": 5}) is True
        assert fn(_make_state(turn=6), {"every": 5}) is False
        assert fn(_make_state(turn=10), {"every": 5}) is True
        assert fn(_make_state(turn=0), {"every": 5}) is False

    def test_tool_calls_threshold(self):
        fn = get_condition("tool_calls")
        assert fn(_make_state(tool_calls_count=19), {"threshold": 20}) is False
        assert fn(_make_state(tool_calls_count=20), {"threshold": 20}) is True

    def test_message_count_threshold(self):
        fn = get_condition("message_count")
        assert fn(_make_state(message_count=49), {"threshold": 50}) is False
        assert fn(_make_state(message_count=50), {"threshold": 50}) is True

    def test_unknown_condition(self):
        assert get_condition("nonexistent") is None

    def test_register_custom_condition(self):
        @register_condition("test_custom")
        def my_condition(state: TurnState, params: dict) -> bool:
            return params.get("fire", False)

        fn = get_condition("test_custom")
        assert fn is not None
        assert fn(_make_state(), {"fire": True}) is True
        assert fn(_make_state(), {"fire": False}) is False


# ---------------------------------------------------------------------------
# Action tests
# ---------------------------------------------------------------------------


class TestActions:
    @pytest.mark.asyncio
    async def test_compact_context_truncate(self):
        state = _make_state(message_count=20, char_per_message=100)
        assert len(state.messages) == 20

        action_fn = get_action("compact_context")
        await action_fn(state, {"strategy": "truncate", "keep_last": 5})

        # Should have: system + compaction note + 5 recent = 7
        assert len(state.messages) == 7
        assert state.messages[0]["role"] == "system"
        assert "compacted" in state.messages[1]["content"].lower()

    @pytest.mark.asyncio
    async def test_compact_context_truncate_nothing_to_do(self):
        state = _make_state(message_count=5, char_per_message=100)
        original_count = len(state.messages)

        action_fn = get_action("compact_context")
        await action_fn(state, {"strategy": "truncate", "keep_last": 10})

        # Nothing to compact
        assert len(state.messages) == original_count

    @pytest.mark.asyncio
    async def test_compact_context_summarize_with_provider(self):
        state = _make_state(message_count=20, char_per_message=100)

        # Mock provider
        provider = AsyncMock()
        provider.chat = AsyncMock(return_value=MagicMock(
            content="Summary of the conversation: user asked questions and assistant answered."
        ))

        action_fn = get_action("compact_context")
        await action_fn(
            state,
            {"strategy": "summarize", "keep_last": 5},
            provider=provider,
        )

        # Should have: system + summary + 5 recent = 7
        assert len(state.messages) == 7
        assert "summary" in state.messages[1]["content"].lower()
        provider.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_compact_context_summarize_fallback_no_provider(self):
        state = _make_state(message_count=20, char_per_message=100)

        action_fn = get_action("compact_context")
        await action_fn(
            state,
            {"strategy": "summarize", "keep_last": 5},
            provider=None,  # No provider → fallback to truncate
        )

        # Should fall back to truncate
        assert len(state.messages) == 7

    @pytest.mark.asyncio
    async def test_inject_message_default(self):
        state = _make_state(message_count=3)
        original_count = len(state.messages)

        action_fn = get_action("inject_message")
        await action_fn(state, {
            "content": "Reminder: be concise.",
            "role": "system",
        })

        assert len(state.messages) == original_count + 1
        # Inserted before last
        injected = state.messages[-2]
        assert injected["role"] == "system"
        assert injected["content"] == "Reminder: be concise."

    @pytest.mark.asyncio
    async def test_inject_message_at_end(self):
        state = _make_state(message_count=3)

        action_fn = get_action("inject_message")
        await action_fn(state, {
            "content": "End note.",
            "position": "end",
        })

        assert state.messages[-1]["content"] == "End note."

    @pytest.mark.asyncio
    async def test_inject_message_no_content(self):
        state = _make_state(message_count=3)
        original_count = len(state.messages)

        action_fn = get_action("inject_message")
        await action_fn(state, {})  # No content

        # Should do nothing
        assert len(state.messages) == original_count

    @pytest.mark.asyncio
    async def test_log_action(self):
        state = _make_state(turn=5, tool_calls_count=10)

        action_fn = get_action("log")
        # Should not raise
        await action_fn(state, {
            "message": "Turn {turn}, tools {tools}",
            "level": "info",
        })

    @pytest.mark.asyncio
    async def test_module_action(self):
        state = _make_state()

        cb = AsyncMock()
        cb.execute = AsyncMock(return_value=MagicMock(success=True))

        action_fn = get_action("module_action")
        await action_fn(
            state,
            {"name": "hello.say_hello", "action_params": {"greeting": "hi"}},
            context_builder=cb,
        )

        cb.execute.assert_called_once_with(
            "execute_tool",
            {"name": "hello.say_hello", "params": {"greeting": "hi"}},
        )

    @pytest.mark.asyncio
    async def test_module_action_no_builder(self):
        state = _make_state()

        action_fn = get_action("module_action")
        # Should not raise
        await action_fn(state, {"name": "hello.say_hello"}, context_builder=None)


# ---------------------------------------------------------------------------
# HookRunner tests
# ---------------------------------------------------------------------------


class TestHookRunner:
    @pytest.mark.asyncio
    async def test_basic_fire(self):
        hook = Hook(
            id="test_hook",
            on="turn_end",
            condition=HookCondition(type="always"),
            action=HookAction(type="log", params={"message": "fired"}),
        )

        runner = HookRunner([hook])
        state = _make_state()
        fired = await runner.run("turn_end", state)

        assert fired == ["test_hook"]

    @pytest.mark.asyncio
    async def test_wrong_event_not_fired(self):
        hook = Hook(
            id="test_hook",
            on="turn_start",
            condition=HookCondition(type="always"),
            action=HookAction(type="log"),
        )

        runner = HookRunner([hook])
        state = _make_state()
        fired = await runner.run("turn_end", state)

        assert fired == []

    @pytest.mark.asyncio
    async def test_condition_false_not_fired(self):
        hook = Hook(
            id="test_hook",
            on="turn_end",
            condition=HookCondition(type="turn_count", params={"threshold": 100}),
            action=HookAction(type="log"),
        )

        runner = HookRunner([hook])
        state = _make_state(turn=5)
        fired = await runner.run("turn_end", state)

        assert fired == []

    @pytest.mark.asyncio
    async def test_cooldown(self):
        hook = Hook(
            id="cooldown_hook",
            on="turn_end",
            condition=HookCondition(type="always"),
            action=HookAction(type="log"),
            cooldown=10.0,  # 10 second cooldown
        )

        runner = HookRunner([hook])
        state = _make_state()

        # First fire: should succeed
        fired1 = await runner.run("turn_end", state)
        assert fired1 == ["cooldown_hook"]

        # Second fire immediately: should be blocked by cooldown
        fired2 = await runner.run("turn_end", state)
        assert fired2 == []

    @pytest.mark.asyncio
    async def test_multiple_hooks_ordering(self):
        """Hooks fire in declaration order."""
        fired_order = []

        @register_action("track_a")
        async def track_a(state, params, **kwargs):
            fired_order.append("a")

        @register_action("track_b")
        async def track_b(state, params, **kwargs):
            fired_order.append("b")

        hooks = [
            Hook(id="first", on="turn_end", condition=HookCondition(type="always"), action=HookAction(type="track_a")),
            Hook(id="second", on="turn_end", condition=HookCondition(type="always"), action=HookAction(type="track_b")),
        ]

        runner = HookRunner(hooks)
        state = _make_state()
        fired = await runner.run("turn_end", state)

        assert fired == ["first", "second"]
        assert fired_order == ["a", "b"]

    @pytest.mark.asyncio
    async def test_compact_via_runner(self):
        """Full integration: compact_context triggered by context_pressure."""
        hook = Hook(
            id="auto_compact",
            on="turn_end",
            condition=HookCondition(
                type="context_pressure",
                params={"threshold": 0.01},  # Very low threshold
            ),
            action=HookAction(
                type="compact_context",
                params={"strategy": "truncate", "keep_recent": 3},
            ),
        )

        runner = HookRunner([hook])
        # Use small max_context_tokens so pressure is high
        state = _make_state(message_count=20, char_per_message=200, max_context_tokens=1000, output_reserved=0)

        fired = await runner.run("turn_end", state)
        assert fired == ["auto_compact"]
        # Messages should be compacted
        assert len(state.messages) == 5  # system + note + 3 recent

    @pytest.mark.asyncio
    async def test_unknown_condition_ignored(self):
        hook = Hook(
            id="bad_condition",
            on="turn_end",
            condition=HookCondition(type="nonexistent_condition"),
            action=HookAction(type="log"),
        )

        runner = HookRunner([hook])
        state = _make_state()
        fired = await runner.run("turn_end", state)
        assert fired == []

    @pytest.mark.asyncio
    async def test_unknown_action_ignored(self):
        hook = Hook(
            id="bad_action",
            on="turn_end",
            condition=HookCondition(type="always"),
            action=HookAction(type="nonexistent_action"),
        )

        runner = HookRunner([hook])
        state = _make_state()
        fired = await runner.run("turn_end", state)
        assert fired == []

    @pytest.mark.asyncio
    async def test_action_error_handled(self):
        @register_action("error_action")
        async def error_action(state, params, **kwargs):
            raise ValueError("boom")

        hook = Hook(
            id="error_hook",
            on="turn_end",
            condition=HookCondition(type="always"),
            action=HookAction(type="error_action"),
        )

        runner = HookRunner([hook])
        state = _make_state()
        # Should not raise
        fired = await runner.run("turn_end", state)
        assert fired == []  # Error means not counted as fired

    def test_hook_count(self):
        hooks = [
            Hook(id="a", condition=HookCondition(type="always"), action=HookAction(type="log")),
            Hook(id="b", condition=HookCondition(type="always"), action=HookAction(type="log")),
        ]
        runner = HookRunner(hooks)
        assert runner.hook_count == 2

    def test_empty_runner(self):
        runner = HookRunner([])
        assert runner.hook_count == 0


# ---------------------------------------------------------------------------
# RuntimeHook tests
# ---------------------------------------------------------------------------


class TestRuntimeHook:
    def test_can_fire_no_cooldown(self):
        rh = RuntimeHook(hook=Hook(id="test", cooldown=0))
        assert rh.can_fire is True

    def test_can_fire_with_cooldown(self):
        rh = RuntimeHook(hook=Hook(id="test", cooldown=1.0))
        assert rh.can_fire is True

        rh.mark_fired()
        assert rh.can_fire is False

    def test_fire_count(self):
        rh = RuntimeHook(hook=Hook(id="test", cooldown=0))
        assert rh.fire_count == 0
        rh.mark_fired()
        assert rh.fire_count == 1
        rh.mark_fired()
        assert rh.fire_count == 2


# ---------------------------------------------------------------------------
# Compilation tests
# ---------------------------------------------------------------------------


class TestCompileHooks:
    def test_compile_hooks_basic(self):
        from dataclasses import dataclass, field
        from typing import Any

        @dataclass
        class FakeCompiledHook:
            id: str
            on: str = "turn_end"
            condition_type: str = "always"
            condition_params: dict[str, Any] = field(default_factory=dict)
            action_type: str = "log"
            action_params: dict[str, Any] = field(default_factory=dict)
            cooldown: float = 0.0

        compiled = [
            FakeCompiledHook(
                id="ctx_compact",
                on="turn_end",
                condition_type="context_pressure",
                condition_params={"threshold": 0.75},
                action_type="compact_context",
                action_params={"strategy": "truncate", "keep_last": 10},
                cooldown=30.0,
            ),
        ]

        hooks = compile_hooks(compiled)
        assert len(hooks) == 1
        h = hooks[0]
        assert h.id == "ctx_compact"
        assert h.on == "turn_end"
        assert h.condition.type == "context_pressure"
        assert h.condition.params == {"threshold": 0.75}
        assert h.action.type == "compact_context"
        assert h.action.params == {"strategy": "truncate", "keep_last": 10}
        assert h.cooldown == 30.0

    def test_build_hook_runner_none(self):
        result = build_hook_runner([])
        assert result is None

    def test_build_hook_runner_with_hooks(self):
        from digitorn.core.app.compiler import CompiledHook

        compiled = [
            CompiledHook(
                id="test",
                on="turn_end",
                condition_type="always",
                action_type="log",
            ),
        ]

        runner = build_hook_runner(compiled)
        assert runner is not None
        assert runner.hook_count == 1


# ---------------------------------------------------------------------------
# YAML Schema + Compiler integration tests
# ---------------------------------------------------------------------------


class TestHookYAMLSchema:
    def test_hook_in_yaml(self):
        """Test that hooks can be parsed from YAML-like dict."""
        from digitorn.core.app.schema import ExecutionConfig

        raw = {
            "mode": "conversation",
            "hooks": [
                {
                    "id": "auto_compact",
                    "on": "turn_end",
                    "condition": {
                        "type": "context_pressure",
                        "threshold": 0.75,
                        "max_tokens": 128000,
                    },
                    "action": {
                        "type": "compact_context",
                        "strategy": "summarize",
                        "keep_last": 10,
                    },
                    "cooldown": 30.0,
                },
            ],
        }

        config = ExecutionConfig.model_validate(raw)
        assert len(config.hooks) == 1
        h = config.hooks[0]
        assert h.id == "auto_compact"
        assert h.on == "turn_end"
        assert h.condition.type == "context_pressure"
        assert h.action.type == "compact_context"
        assert h.cooldown == 30.0

    def test_hook_extra_params_in_condition(self):
        """Test that extra fields in condition/action are preserved."""
        from digitorn.core.app.schema import HookConditionConfig

        config = HookConditionConfig.model_validate({
            "type": "context_pressure",
            "threshold": 0.8,
            "max_tokens": 64000,
        })
        assert config.type == "context_pressure"
        data = config.model_dump()
        assert data["threshold"] == 0.8
        assert data["max_tokens"] == 64000


class TestCompilerHookIntegration:
    """Test hook compilation through the full compiler pipeline."""

    def test_compile_app_with_hooks(self):
        """Compile a YAML with hooks and verify CompiledExecution.hooks."""
        from unittest.mock import MagicMock
        from digitorn.core.app.compiler import AppYAMLCompiler

        # Minimal mock registry
        registry = MagicMock()
        registry.list_available.return_value = ["hello"]
        module = MagicMock()
        manifest = MagicMock()
        manifest.action_names.return_value = ["say_hello"]
        manifest.supported_constraints = []
        module.get_manifest.return_value = manifest
        module._action_registry = {}
        module.CONFIG_MODEL = None
        registry.get.return_value = module

        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test-hooks", "name": "Test Hooks"},
            "modules": {"hello": {}},
            "agents": [{
                "id": "agent",
                "brain": {"provider": "test", "model": "test-model"},
                "system_prompt": "test",
            }],
            "execution": {
                "mode": "conversation",
                "hooks": [
                    {
                        "id": "auto_compact",
                        "on": "turn_end",
                        "condition": {"type": "context_pressure", "threshold": 0.75},
                        "action": {"type": "compact_context", "strategy": "truncate", "keep_last": 5},
                        "cooldown": 60.0,
                    },
                    {
                        "id": "periodic_log",
                        "on": "turn_end",
                        "condition": {"type": "turn_count", "every": 10},
                        "action": {"type": "log", "message": "Turn {turn}"},
                    },
                ],
            },
        }

        compiled = compiler.compile(raw)

        assert len(compiled.execution.hooks) == 2

        h1 = compiled.execution.hooks[0]
        assert h1.id == "auto_compact"
        assert h1.condition_type == "context_pressure"
        assert h1.condition_params["threshold"] == 0.75
        assert h1.action_type == "compact_context"
        assert h1.action_params["strategy"] == "truncate"
        assert h1.cooldown == 60.0

        h2 = compiled.execution.hooks[1]
        assert h2.id == "periodic_log"
        assert h2.condition_type == "turn_count"
        assert h2.condition_params["every"] == 10
        assert h2.action_type == "log"

    def test_compile_hooks_validation_errors(self):
        """Invalid hook config should produce compilation errors."""
        from unittest.mock import MagicMock
        from digitorn.core.app.compiler import AppYAMLCompiler
        from digitorn.core.app.errors import AppCompilationError

        registry = MagicMock()
        registry.list_available.return_value = ["hello"]
        module = MagicMock()
        manifest = MagicMock()
        manifest.action_names.return_value = ["say_hello"]
        manifest.supported_constraints = []
        module.get_manifest.return_value = manifest
        module._action_registry = {}
        module.CONFIG_MODEL = None
        registry.get.return_value = module

        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "test-bad-hooks", "name": "Test"},
            "modules": {"hello": {}},
            "agents": [{
                "id": "agent",
                "brain": {"provider": "test", "model": "test-model"},
            }],
            "execution": {
                "mode": "one_shot",
                "hooks": [
                    {
                        "id": "bad1",
                        "on": "invalid_event",
                        "condition": {"type": "nonexistent"},
                        "action": {"type": "nonexistent"},
                    },
                    {
                        "id": "bad1",  # duplicate ID
                        "on": "turn_end",
                        "condition": {"type": "always"},
                        "action": {"type": "log"},
                    },
                ],
            },
        }

        with pytest.raises(AppCompilationError) as exc_info:
            compiler.compile(raw)

        errors = exc_info.value.errors
        # Should have errors for: invalid event, unknown condition, unknown action, duplicate ID
        assert len(errors) >= 3

    def test_compile_no_hooks(self):
        """App without hooks should have empty hooks list."""
        from unittest.mock import MagicMock
        from digitorn.core.app.compiler import AppYAMLCompiler

        registry = MagicMock()
        registry.list_available.return_value = ["hello"]
        module = MagicMock()
        manifest = MagicMock()
        manifest.action_names.return_value = ["say_hello"]
        manifest.supported_constraints = []
        module.get_manifest.return_value = manifest
        module._action_registry = {}
        module.CONFIG_MODEL = None
        registry.get.return_value = module

        compiler = AppYAMLCompiler(registry)

        raw = {
            "app": {"app_id": "no-hooks", "name": "Test"},
            "modules": {"hello": {}},
            "agents": [{
                "id": "agent",
                "brain": {"provider": "test", "model": "test-model"},
            }],
            "execution": {"mode": "one_shot"},
        }

        compiled = compiler.compile(raw)
        assert compiled.execution.hooks == []


# ---------------------------------------------------------------------------
# Agent loop integration tests
# ---------------------------------------------------------------------------


class TestAgentLoopHooks:
    """Test hooks firing during the agent loop."""

    @pytest.mark.asyncio
    async def test_hooks_called_in_agent_loop(self):
        """Verify hooks are called during agent_turn."""
        from digitorn.core.runtime.agent_loop import agent_turn
        from digitorn.core.runtime.types import AgentContext

        # Track hook fires
        fired_events = []

        @register_action("track_fire")
        async def track_fire(state, params, **kwargs):
            fired_events.append({
                "event": params.get("event", "unknown"),
                "turn": state.turn,
            })

        hooks = [
            Hook(
                id="track_start",
                on="turn_start",
                condition=HookCondition(type="always"),
                action=HookAction(type="track_fire", params={"event": "turn_start"}),
            ),
            Hook(
                id="track_end",
                on="turn_end",
                condition=HookCondition(type="always"),
                action=HookAction(type="track_fire", params={"event": "turn_end"}),
            ),
        ]
        runner = HookRunner(hooks)

        # Mock provider that returns tool_calls on first call, then text
        provider = AsyncMock()
        call_count = 0

        async def mock_chat(messages, tools=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: return a tool call
                return MagicMock(
                    content="Let me search.",
                    tool_calls=[{
                        "id": "call_1",
                        "function": {"name": "search_tools", "arguments": '{"query": "test"}'},
                    }],
                )
            else:
                # Second call: return text (no tool calls)
                return MagicMock(content="Done.", tool_calls=None)

        provider.chat = mock_chat

        # Mock context builder
        cb = AsyncMock()
        cb.execute = AsyncMock(return_value=MagicMock(success=True, data={"tools": []}))

        ctx = AgentContext(
            agent_id="test",
            role="worker",
            provider=provider,
            system_prompt="Test",
            tools=[{"type": "function", "function": {"name": "search_tools"}}],
            generation_params={},
        )
        ctx._context_builder = cb

        messages = [
            {"role": "system", "content": "Test"},
            {"role": "user", "content": "Hello"},
        ]

        result = await agent_turn(
            ctx, messages,
            max_turns=10,
            timeout=30.0,
            hook_runner=runner,
        )

        assert result.content == "Done."
        # turn_start fires on turn 0 and turn 1
        # turn_end fires on turn 0 (after tool execution, before turn 1)
        # turn 1: turn_start fires, then LLM returns text → loop exits (no turn_end)
        assert len([e for e in fired_events if e["event"] == "turn_start"]) == 2
        assert len([e for e in fired_events if e["event"] == "turn_end"]) == 1

    @pytest.mark.asyncio
    async def test_no_hooks_no_error(self):
        """Agent loop works fine without hooks."""
        from digitorn.core.runtime.agent_loop import agent_turn
        from digitorn.core.runtime.types import AgentContext

        provider = AsyncMock()
        provider.chat = AsyncMock(return_value=MagicMock(
            content="Hello!", tool_calls=None,
        ))

        ctx = AgentContext(
            agent_id="test",
            role="worker",
            provider=provider,
            system_prompt="Test",
            tools=[],
            generation_params={},
        )

        messages = [
            {"role": "system", "content": "Test"},
            {"role": "user", "content": "Hello"},
        ]

        result = await agent_turn(ctx, messages, max_turns=10, timeout=30.0)
        assert result.content == "Hello!"

    @pytest.mark.asyncio
    async def test_compact_hook_in_agent_loop(self):
        """Context compaction hook actually reduces messages during agent loop."""
        from digitorn.core.runtime.agent_loop import agent_turn
        from digitorn.core.runtime.types import AgentContext

        hooks = [
            Hook(
                id="compact",
                on="turn_end",
                condition=HookCondition(type="always"),
                action=HookAction(
                    type="compact_context",
                    params={"strategy": "truncate", "keep_last": 3},
                ),
            ),
        ]
        runner = HookRunner(hooks)

        # Provider returns tool call then text
        call_count = 0

        async def mock_chat(messages, tools=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MagicMock(
                    content="Working...",
                    tool_calls=[{
                        "id": "call_1",
                        "function": {"name": "search_tools", "arguments": '{"query": "x"}'},
                    }],
                )
            return MagicMock(content="Final answer.", tool_calls=None)

        provider = AsyncMock()
        provider.chat = mock_chat

        cb = AsyncMock()
        cb.execute = AsyncMock(return_value=MagicMock(success=True, data={}))

        ctx = AgentContext(
            agent_id="test", role="worker", provider=provider,
            system_prompt="Test", tools=[{"type": "function", "function": {"name": "search_tools"}}],
            generation_params={},
        )
        ctx._context_builder = cb

        # Start with many messages
        messages = [{"role": "system", "content": "System"}]
        for i in range(20):
            messages.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"})
        messages.append({"role": "user", "content": "Final question"})

        result = await agent_turn(ctx, messages, max_turns=10, timeout=30.0, hook_runner=runner)

        assert result.content == "Final answer."
        # Messages should have been compacted during the loop
        # After compaction: system + note + 3 recent + assistant(tool_call) + tool_result
        # The exact count depends on timing, but should be much less than original 22+
        assert len(messages) < 15


# ---------------------------------------------------------------------------
# Hook event notification tests
# ---------------------------------------------------------------------------


class TestHookEventNotifications:
    """Test that HookRunner emits events via on_hook_event callback."""

    @pytest.mark.asyncio
    async def test_events_emitted_on_fire(self):
        """on_hook_event receives start and end events when a hook fires."""
        events = []

        async def on_event(event):
            events.append(event)

        hook = Hook(
            id="test_hook",
            on="turn_end",
            condition=HookCondition(type="always"),
            action=HookAction(type="log", params={"message": "hello"}),
        )

        runner = HookRunner([hook], on_hook_event=on_event)
        state = _make_state()
        fired = await runner.run("turn_end", state)

        assert fired == ["test_hook"]
        hook_events = [e for e in events if e.hook_id != "_system"]
        assert len(hook_events) == 2

        # Start event
        assert hook_events[0].hook_id == "test_hook"
        assert hook_events[0].action_type == "log"
        assert hook_events[0].phase == "start"
        assert hook_events[0].details["condition"] == "always"

        # End event
        assert hook_events[1].hook_id == "test_hook"
        assert hook_events[1].action_type == "log"
        assert hook_events[1].phase == "end"
        assert hook_events[1].details["fire_count"] == 1

    @pytest.mark.asyncio
    async def test_error_event_on_action_failure(self):
        """on_hook_event receives an error event when action fails."""
        events = []

        async def on_event(event):
            events.append(event)

        @register_action("failing_action_notify")
        async def failing_action(state, params, **kwargs):
            raise RuntimeError("test failure")

        hook = Hook(
            id="fail_hook",
            on="turn_end",
            condition=HookCondition(type="always"),
            action=HookAction(type="failing_action_notify"),
        )

        runner = HookRunner([hook], on_hook_event=on_event)
        state = _make_state()
        fired = await runner.run("turn_end", state)

        assert fired == []  # Failed hooks don't count as fired
        hook_events = [e for e in events if e.hook_id != "_system"]
        assert len(hook_events) == 2  # start + error

        assert hook_events[0].phase == "start"
        assert hook_events[1].phase == "error"
        assert "test failure" in hook_events[1].details["error"]

    @pytest.mark.asyncio
    async def test_no_events_without_callback(self):
        """No crash when on_hook_event is None."""
        hook = Hook(
            id="silent_hook",
            on="turn_end",
            condition=HookCondition(type="always"),
            action=HookAction(type="log"),
        )

        runner = HookRunner([hook])  # no on_hook_event
        state = _make_state()
        fired = await runner.run("turn_end", state)
        assert fired == ["silent_hook"]

    @pytest.mark.asyncio
    async def test_callback_error_does_not_break_hooks(self):
        """Errors in on_hook_event callback don't break the hook pipeline."""
        async def bad_callback(event):
            raise ValueError("callback crash")

        hook = Hook(
            id="robust_hook",
            on="turn_end",
            condition=HookCondition(type="always"),
            action=HookAction(type="log"),
        )

        runner = HookRunner([hook], on_hook_event=bad_callback)
        state = _make_state()
        fired = await runner.run("turn_end", state)
        assert fired == ["robust_hook"]  # Hook still fires despite callback error

    @pytest.mark.asyncio
    async def test_set_on_hook_event_property(self):
        """on_hook_event can be set after construction."""
        events = []

        async def on_event(event):
            events.append(event)

        hook = Hook(
            id="late_bind",
            on="turn_end",
            condition=HookCondition(type="always"),
            action=HookAction(type="log"),
        )

        runner = HookRunner([hook])
        assert runner.on_hook_event is None

        runner.on_hook_event = on_event
        assert runner.on_hook_event is on_event

        state = _make_state()
        await runner.run("turn_end", state)
        hook_events = [e for e in events if e.hook_id != "_system"]
        assert len(hook_events) == 2  # start + end

    @pytest.mark.asyncio
    async def test_multiple_hooks_emit_events(self):
        """Each fired hook emits its own start/end events."""
        events = []

        async def on_event(event):
            events.append(event)

        hooks = [
            Hook(id="hook_a", on="turn_end",
                 condition=HookCondition(type="always"),
                 action=HookAction(type="log")),
            Hook(id="hook_b", on="turn_end",
                 condition=HookCondition(type="always"),
                 action=HookAction(type="log")),
        ]

        runner = HookRunner(hooks, on_hook_event=on_event)
        state = _make_state()
        fired = await runner.run("turn_end", state)

        assert fired == ["hook_a", "hook_b"]
        hook_events = [e for e in events if e.hook_id != "_system"]
        assert len(hook_events) == 4  # 2 hooks × (start + end)
        assert hook_events[0].hook_id == "hook_a"
        assert hook_events[0].phase == "start"
        assert hook_events[1].hook_id == "hook_a"
        assert hook_events[1].phase == "end"
        assert hook_events[2].hook_id == "hook_b"
        assert hook_events[2].phase == "start"
        assert hook_events[3].hook_id == "hook_b"
        assert hook_events[3].phase == "end"

    @pytest.mark.asyncio
    async def test_compact_context_emits_events(self):
        """compact_context action emits proper start/end events."""
        events = []

        async def on_event(event):
            events.append(event)

        hook = Hook(
            id="compact_notify",
            on="turn_end",
            condition=HookCondition(
                type="context_pressure",
                params={"threshold": 0.01},
            ),
            action=HookAction(
                type="compact_context",
                params={"strategy": "truncate", "keep_recent": 3},
            ),
        )

        runner = HookRunner([hook], on_hook_event=on_event)
        state = _make_state(
            message_count=20, char_per_message=200,
            max_context_tokens=1000, output_reserved=0,
        )

        fired = await runner.run("turn_end", state)
        assert fired == ["compact_notify"]

        start_events = [e for e in events if e.phase == "start"]
        end_events = [e for e in events if e.phase == "end"]
        assert len(start_events) == 1
        assert len(end_events) == 1
        assert start_events[0].action_type == "compact_context"
        assert start_events[0].details["params"]["strategy"] == "truncate"

    @pytest.mark.asyncio
    async def test_build_hook_runner_with_on_hook_event(self):
        """build_hook_runner passes on_hook_event to HookRunner."""
        from digitorn.core.app.compiler import CompiledHook

        events = []
        async def on_event(event):
            events.append(event)

        compiled = [
            CompiledHook(
                id="test",
                on="turn_end",
                condition_type="always",
                action_type="log",
            ),
        ]

        runner = build_hook_runner(compiled, on_hook_event=on_event)
        assert runner is not None
        assert runner.on_hook_event is on_event

        state = _make_state()
        await runner.run("turn_end", state)
        hook_events = [e for e in events if e.hook_id != "_system"]
        assert len(hook_events) == 2
