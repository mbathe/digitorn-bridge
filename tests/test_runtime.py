"""Tests for the Digitorn Runtime.

Covers:
- Schema: ExecutionConfig, InputConfig, OutputConfig, TriggerConfig
- Compiler: CompiledExecution, execution validation
- Agent loop: chat → tools → chat cycle
- One-shot mode: input building, output extraction
- Conversation mode: message loop
- RuntimeApp: orchestration
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.core.runtime.callbacks import AgentTurnCallbacks
from digitorn.core.app.compiler import (
    AppYAMLCompiler,
    CompiledApp,
    CompiledExecution,
    CompiledInput,
    CompiledOutput,
    CompiledTrigger,
)
from digitorn.core.app.schema import (
    AppDefinition,
    ExecutionConfig,
    InputConfig,
    OutputConfig,
    TriggerConfig,
)
from digitorn.core.runtime.agent_loop import (
    _extract_content,
    _extract_tool_calls,
    _serialize_result,
    agent_turn,
)
from digitorn.core.runtime.streaming import _StreamState, _finalize_tool_calls
from digitorn.core.runtime.app import RuntimeApp
from digitorn.core.runtime.modes.one_shot import (
    _build_file_message,
    _build_json_message,
    _build_user_message,
    _extract_json_output,
    run_one_shot,
)
from digitorn.core.runtime.types import AgentContext, TurnResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_action_entry(name: str):
    entry = MagicMock()
    entry.name = name
    entry.params_model = None
    spec = MagicMock()
    spec.name = name
    spec.risk_level = "low"
    spec.permissions = []
    spec.irreversible = False
    entry.spec = spec
    return entry


def _make_module(module_id: str, actions: list[str] | None = None):
    module = MagicMock()
    module.MODULE_ID = module_id
    manifest = MagicMock()
    manifest.action_names.return_value = actions or []
    manifest.supported_constraints = []
    module.get_manifest.return_value = manifest
    module.CONFIG_MODEL = None
    registry = {}
    for a in (actions or []):
        registry[a] = _make_action_entry(a)
    module._action_registry = registry
    return module


def _make_registry(modules: dict[str, MagicMock]):
    registry = MagicMock()
    registry.list_available.return_value = list(modules.keys())

    def get(mid):
        if mid not in modules:
            raise Exception(f"Module '{mid}' not found")
        return modules[mid]

    registry.get = MagicMock(side_effect=get)
    return registry


def _compile(raw: dict, modules: dict | None = None) -> CompiledApp:
    if modules is None:
        modules = {}
    if "llm_provider" not in modules:
        modules["llm_provider"] = _make_module(
            "llm_provider", ["configure", "chat", "list_providers"]
        )
    registry = _make_registry(modules)
    compiler = AppYAMLCompiler(registry)
    return compiler.compile(raw)


def _make_agent_context(
    *,
    agent_id: str = "test_agent",
    role: str = "worker",
    responses: list[Any] | None = None,
    context_builder: Any | None = None,
) -> AgentContext:
    """Create an AgentContext with a mock provider."""
    provider = AsyncMock()

    # Set up provider.chat to return responses in sequence
    if responses:
        provider.chat = AsyncMock(side_effect=responses)
    else:
        provider.chat = AsyncMock(return_value=MagicMock(
            content="Default response",
            tool_calls=None,
        ))

    ctx = AgentContext(
        agent_id=agent_id,
        role=role,
        provider=provider,
        system_prompt="You are a test agent.",
        tools=[],
        generation_params={},
    )

    if context_builder:
        ctx._context_builder = context_builder  # type: ignore[attr-defined]

    return ctx


# ===========================================================================
# Tests - Schema
# ===========================================================================


class TestExecutionSchema:

    def test_default_execution(self):
        defn = AppDefinition.model_validate({
            "app": {"app_id": "test", "name": "Test"},
        })
        assert defn.execution.mode == "one_shot"
        assert defn.execution.max_turns == 50
        assert defn.execution.timeout == 300.0
        assert defn.execution.entry_agent == ""
        assert defn.execution.input.type == "text"
        assert defn.execution.output.type == "text"

    def test_one_shot_with_io(self):
        defn = AppDefinition.model_validate({
            "app": {"app_id": "test", "name": "Test"},
            "execution": {
                "mode": "one_shot",
                "max_turns": 20,
                "timeout": 120,
                "input": {
                    "type": "image",
                    "description": "Screenshot to analyse",
                    "required": True,
                },
                "output": {
                    "type": "json",
                    "description": "Analysis report",
                },
            },
        })
        assert defn.execution.mode == "one_shot"
        assert defn.execution.input.type == "image"
        assert defn.execution.input.description == "Screenshot to analyse"
        assert defn.execution.output.type == "json"

    def test_conversation_with_greeting(self):
        defn = AppDefinition.model_validate({
            "app": {"app_id": "test", "name": "Test"},
            "execution": {
                "mode": "conversation",
                "greeting": "Hello! How can I help?",
            },
        })
        assert defn.execution.mode == "conversation"
        assert defn.execution.greeting == "Hello! How can I help?"

    def test_background_with_triggers(self):
        defn = AppDefinition.model_validate({
            "app": {"app_id": "test", "name": "Test"},
            "execution": {
                "mode": "background",
                "triggers": [
                    {
                        "id": "check",
                        "type": "cron",
                        "schedule": "*/5 * * * *",
                        "message": "Check status",
                    },
                    {
                        "id": "watcher",
                        "type": "watch",
                        "paths": ["./data/*.csv"],
                        "message": "New file: {{event.path}}",
                    },
                ],
            },
        })
        assert defn.execution.mode == "background"
        assert len(defn.execution.triggers) == 2
        assert defn.execution.triggers[0].type == "cron"
        assert defn.execution.triggers[0].schedule == "*/5 * * * *"
        assert defn.execution.triggers[1].type == "watch"
        assert defn.execution.triggers[1].paths == ["./data/*.csv"]

    def test_input_types(self):
        for t in ("text", "image", "file", "json", "any"):
            cfg = InputConfig(type=t)
            assert cfg.type == t

    def test_output_schema(self):
        defn = AppDefinition.model_validate({
            "app": {"app_id": "test", "name": "Test"},
            "execution": {
                "output": {
                    "type": "json",
                    "schema": {
                        "type": "object",
                        "properties": {"score": {"type": "integer"}},
                    },
                },
            },
        })
        assert defn.execution.output.schema_def["type"] == "object"


# ===========================================================================
# Tests - Compiler
# ===========================================================================


class TestCompileExecution:

    def test_default_execution_compiled(self):
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "agents": [{"id": "a1", "brain": {"model": "gpt-4o"}}],
        })
        assert compiled.execution.mode == "one_shot"
        assert compiled.execution.entry_agent == "a1"  # auto-resolved to first agent
        assert compiled.execution.max_turns == 50


class TestStreamingToolCallNormalization:

    def test_finalize_tool_calls_accepts_dict_arguments_from_provider(self):
        state = _StreamState(AgentTurnCallbacks())
        state._accumulate_tool_calls([
            {
                "id": "toolu_1",
                "name": "filesystem.read",
                "arguments": {"path": "README.md", "start_line": 1},
            }
        ])

        tool_calls = _finalize_tool_calls(state)

        assert tool_calls == [
            {
                "id": "toolu_1",
                "type": "function",
                "function": {
                    "name": "filesystem.read",
                    "arguments": {"path": "README.md", "start_line": 1},
                },
            }
        ]

    def test_explicit_entry_agent(self):
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "agents": [
                {"id": "a1", "brain": {"model": "gpt-4o"}},
                {"id": "a2", "brain": {"model": "llama3"}},
            ],
            "execution": {"entry_agent": "a2"},
        })
        assert compiled.execution.entry_agent == "a2"

    def test_invalid_entry_agent(self):
        from digitorn.core.app.errors import AppCompilationError
        with pytest.raises(AppCompilationError, match="not found"):
            _compile({
                "app": {"app_id": "test", "name": "Test"},
                "agents": [{"id": "a1", "brain": {"model": "gpt-4o"}}],
                "execution": {"entry_agent": "nonexistent"},
            })

    def test_invalid_mode(self):
        from digitorn.core.app.errors import AppCompilationError
        with pytest.raises(AppCompilationError):
            _compile({
                "app": {"app_id": "test", "name": "Test"},
                "execution": {"mode": "invalid"},
            })

    def test_background_requires_triggers(self):
        from digitorn.core.app.errors import AppCompilationError
        with pytest.raises(AppCompilationError, match="at least one trigger"):
            _compile({
                "app": {"app_id": "test", "name": "Test"},
                "agents": [{"id": "a1", "brain": {"model": "gpt-4o"}}],
                "execution": {"mode": "background"},
            })

    def test_background_triggers_compiled(self):
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "agents": [{"id": "a1", "brain": {"model": "gpt-4o"}}],
            "execution": {
                "mode": "background",
                "triggers": [
                    {"id": "t1", "type": "cron", "schedule": "*/5 * * * *", "message": "go"},
                ],
            },
        })
        assert len(compiled.execution.triggers) == 1
        assert compiled.execution.triggers[0].id == "t1"
        assert compiled.execution.triggers[0].schedule == "*/5 * * * *"

    def test_duplicate_trigger_id(self):
        from digitorn.core.app.errors import AppCompilationError
        with pytest.raises(AppCompilationError, match="Duplicate trigger"):
            _compile({
                "app": {"app_id": "test", "name": "Test"},
                "agents": [{"id": "a1", "brain": {"model": "gpt-4o"}}],
                "execution": {
                    "mode": "background",
                    "triggers": [
                        {"id": "t1", "type": "cron", "schedule": "*/5 * * * *"},
                        {"id": "t1", "type": "cron", "schedule": "*/10 * * * *"},
                    ],
                },
            })

    def test_cron_trigger_requires_schedule(self):
        from digitorn.core.app.errors import AppCompilationError
        with pytest.raises(AppCompilationError, match="schedule"):
            _compile({
                "app": {"app_id": "test", "name": "Test"},
                "agents": [{"id": "a1", "brain": {"model": "gpt-4o"}}],
                "execution": {
                    "mode": "background",
                    "triggers": [{"id": "t1", "type": "cron"}],
                },
            })

    def test_watch_trigger_requires_paths(self):
        from digitorn.core.app.errors import AppCompilationError
        with pytest.raises(AppCompilationError, match="paths"):
            _compile({
                "app": {"app_id": "test", "name": "Test"},
                "agents": [{"id": "a1", "brain": {"model": "gpt-4o"}}],
                "execution": {
                    "mode": "background",
                    "triggers": [{"id": "t1", "type": "watch"}],
                },
            })

    def test_io_config_compiled(self):
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "agents": [{"id": "a1", "brain": {"model": "gpt-4o"}}],
            "execution": {
                "input": {"type": "image", "required": True},
                "output": {"type": "json"},
            },
        })
        assert compiled.execution.input.type == "image"
        assert compiled.execution.output.type == "json"

    def test_greeting_compiled(self):
        compiled = _compile({
            "app": {"app_id": "test", "name": "Test"},
            "agents": [{"id": "a1", "brain": {"model": "gpt-4o"}}],
            "execution": {
                "mode": "conversation",
                "greeting": "Welcome!",
            },
        })
        assert compiled.execution.greeting == "Welcome!"

    def test_no_agents_rejected(self):
        from digitorn.core.app.errors import AppCompilationError
        with pytest.raises(AppCompilationError, match="At least one agent"):
            _compile({
                "app": {"app_id": "test", "name": "Test"},
            })


# ===========================================================================
# Tests - Agent Loop
# ===========================================================================


class TestAgentLoop:

    @pytest.mark.asyncio()
    async def test_simple_response_no_tools(self):
        """Agent responds without calling any tools."""
        ctx = _make_agent_context(responses=[
            MagicMock(content="Hello!", tool_calls=None),
        ])

        result = await agent_turn(ctx, [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hi"},
        ])

        assert result.content == "Hello!"
        assert result.tool_calls_count == 0
        assert result.turns_used == 1
        assert not result.truncated
        assert result.error is None

    @pytest.mark.asyncio()
    async def test_tool_call_then_response(self):
        """Agent calls a tool then responds."""
        cb = AsyncMock()
        cb.execute = AsyncMock(return_value=MagicMock(
            success=True,
            data={"result": "42"},
            error=None,
        ))

        ctx = _make_agent_context(
            responses=[
                # First response: tool call
                MagicMock(
                    content="",
                    tool_calls=[{
                        "id": "call_1",
                        "function": {
                            "name": "search_tools",
                            "arguments": json.dumps({"query": "calculator"}),
                        },
                    }],
                ),
                # Second response: final answer
                MagicMock(content="The answer is 42.", tool_calls=None),
            ],
            context_builder=cb,
        )

        result = await agent_turn(ctx, [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Calculate 6*7"},
        ])

        assert result.content == "The answer is 42."
        assert result.tool_calls_count == 1
        assert result.turns_used == 2

    @pytest.mark.asyncio()
    async def test_multiple_tool_calls(self):
        """Agent calls multiple tools in sequence."""
        cb = AsyncMock()
        cb.execute = AsyncMock(return_value=MagicMock(
            success=True, data={"ok": True}, error=None,
        ))

        ctx = _make_agent_context(
            responses=[
                MagicMock(content="", tool_calls=[
                    {"id": "c1", "function": {"name": "search_tools", "arguments": '{"query": "file"}'}},
                    {"id": "c2", "function": {"name": "get_tool", "arguments": '{"name": "fs.read"}'}},
                ]),
                MagicMock(content="Done.", tool_calls=None),
            ],
            context_builder=cb,
        )

        result = await agent_turn(ctx, [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "Read a file"},
        ])

        assert result.tool_calls_count == 2
        assert result.content == "Done."

    @pytest.mark.asyncio()
    async def test_max_turns_limit(self):
        """Agent is stopped when max_turns is reached."""
        # Provider always returns tool calls (infinite loop)
        provider = AsyncMock()
        provider.chat = AsyncMock(return_value=MagicMock(
            content="",
            tool_calls=[{
                "id": "c1",
                "function": {"name": "search_tools", "arguments": '{"query": "x"}'},
            }],
        ))

        cb = AsyncMock()
        cb.execute = AsyncMock(return_value=MagicMock(
            success=True, data={}, error=None,
        ))

        ctx = AgentContext(
            agent_id="test", role="worker",
            provider=provider, system_prompt="S", tools=[],
        )
        ctx._context_builder = cb  # type: ignore[attr-defined]

        result = await agent_turn(ctx, [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "Loop forever"},
        ], max_turns=3)

        assert result.truncated
        assert result.turns_used == 3
        assert "Max turns" in result.error

    @pytest.mark.asyncio()
    async def test_timeout(self):
        """Agent is stopped on timeout."""
        async def slow_chat(*args, **kwargs):
            import asyncio
            await asyncio.sleep(10)

        provider = AsyncMock()
        provider.chat = slow_chat

        ctx = AgentContext(
            agent_id="test", role="worker",
            provider=provider, system_prompt="S", tools=[],
        )

        result = await agent_turn(ctx, [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "Slow"},
        ], timeout=0.1)

        assert result.truncated
        assert "Timeout" in result.error

    @pytest.mark.asyncio()
    async def test_tool_call_callback(self):
        """on_tool_call callback is invoked."""
        cb = AsyncMock()
        cb.execute = AsyncMock(return_value=MagicMock(
            success=True, data={"x": 1}, error=None,
        ))
        callback = AsyncMock()

        ctx = _make_agent_context(
            responses=[
                MagicMock(content="", tool_calls=[{
                    "id": "c1",
                    "function": {"name": "search_tools", "arguments": '{"query": "test"}'},
                }]),
                MagicMock(content="Done.", tool_calls=None),
            ],
            context_builder=cb,
        )

        await agent_turn(ctx, [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "Go"},
        ], on_tool_call=callback)

        callback.assert_awaited_once()
        call_args = callback.call_args
        assert call_args[0][0] == "search_tools"


# ===========================================================================
# Tests - One-Shot Mode
# ===========================================================================


class TestOneShotMode:

    def test_build_text_message(self):
        msg = _build_user_message("Hello world", "text")
        assert msg["role"] == "user"
        assert msg["content"] == "Hello world"

    def test_build_json_message_raw(self):
        msg = _build_json_message('{"key": "value"}')
        assert "key" in msg["content"]
        assert "value" in msg["content"]
        assert "```json" in msg["content"]

    def test_build_file_message_nonexistent(self):
        msg = _build_file_message("/nonexistent/file.txt")
        assert "File path:" in msg["content"]

    def test_extract_json_from_code_block(self):
        result = TurnResult(
            content='Here is the result:\n```json\n{"score": 85}\n```\nDone.',
            tool_calls_count=1, turns_used=2,
        )
        extracted = _extract_json_output(result)
        parsed = json.loads(extracted.content)
        assert parsed["score"] == 85

    def test_extract_json_raw(self):
        result = TurnResult(
            content='{"bugs": [], "score": 100}',
            tool_calls_count=0, turns_used=1,
        )
        extracted = _extract_json_output(result)
        parsed = json.loads(extracted.content)
        assert parsed["score"] == 100

    def test_extract_json_not_found(self):
        result = TurnResult(
            content="This is just text, no JSON here.",
            tool_calls_count=0, turns_used=1,
        )
        extracted = _extract_json_output(result)
        assert extracted.content == "This is just text, no JSON here."

    @pytest.mark.asyncio()
    async def test_run_one_shot(self):
        ctx = _make_agent_context(responses=[
            MagicMock(content="Analysis complete.", tool_calls=None),
        ])

        result = await run_one_shot(
            ctx,
            user_input="Analyse this code",
            input_type="text",
            output_type="text",
        )

        assert result.content == "Analysis complete."
        assert not result.error

    @pytest.mark.asyncio()
    async def test_run_one_shot_json_output(self):
        ctx = _make_agent_context(responses=[
            MagicMock(
                content='```json\n{"score": 95}\n```',
                tool_calls=None,
            ),
        ])

        result = await run_one_shot(
            ctx,
            user_input="Analyse",
            output_type="json",
        )

        parsed = json.loads(result.content)
        assert parsed["score"] == 95


# ===========================================================================
# Tests - Conversation Mode
# ===========================================================================


class TestConversationMode:

    @pytest.mark.asyncio()
    async def test_conversation_quit(self):
        """Conversation exits on /quit."""
        from digitorn.core.runtime.modes.conversation import run_conversation

        inputs = iter(["Hello", "/quit"])
        outputs = []

        async def fake_input(prompt):
            return next(inputs)

        ctx = _make_agent_context(responses=[
            MagicMock(content="Hi there!", tool_calls=None),
        ])

        await run_conversation(
            ctx,
            greeting="Welcome!",
            input_fn=fake_input,
            output_fn=outputs.append,
        )

        # Should have greeting + response + goodbye
        combined = "\n".join(outputs)
        assert "Welcome!" in combined
        assert "Hi there!" in combined
        assert "Goodbye" in combined

    @pytest.mark.asyncio()
    async def test_conversation_eof(self):
        """Conversation exits on EOF."""
        from digitorn.core.runtime.modes.conversation import run_conversation

        async def eof_input(prompt):
            raise EOFError()

        outputs = []
        ctx = _make_agent_context()

        await run_conversation(
            ctx,
            input_fn=eof_input,
            output_fn=outputs.append,
        )

        combined = "\n".join(outputs)
        assert "Goodbye" in combined

    @pytest.mark.asyncio()
    async def test_conversation_empty_input_skipped(self):
        """Empty input is skipped."""
        from digitorn.core.runtime.modes.conversation import run_conversation

        inputs = iter(["", "  ", "Hello", "/quit"])
        outputs = []

        async def fake_input(prompt):
            return next(inputs)

        ctx = _make_agent_context(responses=[
            MagicMock(content="Response", tool_calls=None),
        ])

        await run_conversation(
            ctx,
            input_fn=fake_input,
            output_fn=outputs.append,
        )

        # Provider should only be called once (for "Hello")
        assert ctx.provider.chat.await_count == 1


# ===========================================================================
# Tests - RuntimeApp
# ===========================================================================


class TestRuntimeApp:

    @pytest.mark.asyncio()
    async def test_run_one_shot_mode(self):
        ctx = _make_agent_context(responses=[
            MagicMock(content="Result", tool_calls=None),
        ])

        app = RuntimeApp(
            app_id="test",
            execution=CompiledExecution(mode="one_shot"),
            contexts={"test_agent": ctx},
        )

        result = await app.run(user_input="Do something")
        assert result.content == "Result"

    @pytest.mark.asyncio()
    async def test_run_conversation_mode(self):
        async def eof_input(prompt):
            raise EOFError()

        ctx = _make_agent_context()
        app = RuntimeApp(
            app_id="test",
            execution=CompiledExecution(mode="conversation"),
            contexts={"test_agent": ctx},
        )

        result = await app.run(input_fn=eof_input, output_fn=lambda x: None)
        assert result is None

    @pytest.mark.asyncio()
    async def test_shutdown(self):
        module = AsyncMock()
        cb = AsyncMock()

        app = RuntimeApp(
            app_id="test",
            execution=CompiledExecution(),
            modules={"mod": module},
            context_builder=cb,
        )

        await app.shutdown()
        module.on_stop.assert_awaited_once()
        cb.on_stop.assert_awaited_once()

    def test_entry_context_fallback(self):
        ctx = _make_agent_context(agent_id="a1")
        app = RuntimeApp(
            app_id="test",
            execution=CompiledExecution(entry_agent="missing"),
            contexts={"a1": ctx},
        )
        assert app.entry_context.agent_id == "a1"


# ===========================================================================
# Tests - Helper functions
# ===========================================================================


class TestHelpers:

    def test_extract_content_dict(self):
        assert _extract_content({"content": "hello"}) == "hello"

    def test_extract_content_object(self):
        obj = MagicMock()
        obj.content = "world"
        assert _extract_content(obj) == "world"

    def test_extract_tool_calls_empty(self):
        assert _extract_tool_calls({"content": "no tools"}) == []
        assert _extract_tool_calls(MagicMock(tool_calls=None)) == []

    def test_extract_tool_calls_present(self):
        calls = [{"id": "1", "function": {"name": "test"}}]
        assert _extract_tool_calls({"tool_calls": calls}) == calls

    def test_serialize_action_result(self):
        result = MagicMock()
        result.success = True
        result.data = {"key": "value"}
        serialized = _serialize_result(result)
        assert json.loads(serialized) == {"key": "value"}

    def test_serialize_action_result_error(self):
        result = MagicMock()
        result.success = False
        result.error = "Something failed"
        serialized = _serialize_result(result)
        assert "Something failed" in serialized

    def test_serialize_plain_dict(self):
        serialized = _serialize_result({"x": 1})
        assert json.loads(serialized) == {"x": 1}

    def test_serialize_string(self):
        assert _serialize_result("hello") == "hello"


# ===========================================================================
# Tests - Cron interval parsing
# ===========================================================================


class TestCronParsing:

    def test_every_5_minutes(self):
        from digitorn.core.runtime.modes.background import _seconds_until_next
        result = _seconds_until_next("*/5 * * * *")
        assert result is not None
        assert 1 <= result <= 300  # at most 5 minutes away

    def test_every_6_hours(self):
        from digitorn.core.runtime.modes.background import _seconds_until_next
        result = _seconds_until_next("0 */6 * * *")
        assert result is not None
        assert 1 <= result <= 21600  # at most 6 hours away

    def test_daily(self):
        from digitorn.core.runtime.modes.background import _seconds_until_next
        result = _seconds_until_next("0 8 * * *")
        assert result is not None
        assert 1 <= result <= 86400  # at most 24 hours away

    def test_invalid_returns_none(self):
        from digitorn.core.runtime.modes.background import _seconds_until_next
        assert _seconds_until_next("bad") is None
