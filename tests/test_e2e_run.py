"""End-to-end tests for the full Digitorn pipeline.

Tests the complete flow: YAML → compile → bootstrap → runtime → result.
Uses a mock LLM provider to avoid API dependencies.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.modules.llm_provider.providers.base import (
    ChatMessage,
    ChatResponse,
    ProviderCapabilities,
    ProviderInfo,
    TokenUsage,
)


# ---------------------------------------------------------------------------
# Mock LLM provider
# ---------------------------------------------------------------------------


class MockLLMProvider:
    """A mock LLM provider that returns scripted responses."""

    def __init__(self, responses: list[ChatResponse] | None = None):
        self.provider_id = "mock_provider"
        self.model = "mock-model"
        self.api_key = ""
        self.base_url = None
        self.timeout = 120.0
        self.max_retries = 2
        self.default_params: dict[str, Any] = {}
        self._responses = list(responses or [])
        self._call_index = 0
        self.call_log: list[dict[str, Any]] = []

    async def initialize(self) -> None:
        pass

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatResponse:
        self.call_log.append({
            "messages": [(m.role, m.content[:100]) for m in messages],
            "tools": [t["function"]["name"] for t in (tools or [])],
        })

        if self._call_index < len(self._responses):
            resp = self._responses[self._call_index]
            self._call_index += 1
            return resp
        # Default: simple text response
        return ChatResponse(
            content="Hello! I'm a mock assistant.",
            model="mock-model",
            finish_reason="end_turn",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            tool_calls=None,
            raw={},
        )

    def get_info(self) -> ProviderInfo:
        return ProviderInfo(
            provider_id=self.provider_id,
            backend="mock",
            model=self.model,
            capabilities=ProviderCapabilities(tool_use=True),
            extra={},
        )

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_call_response(
    tool_name: str, arguments: dict[str, Any], call_id: str = "call_1"
) -> ChatResponse:
    """Create a ChatResponse that requests a tool call."""
    return ChatResponse(
        content="",
        model="mock-model",
        finish_reason="tool_use",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        tool_calls=[{
            "id": call_id,
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": arguments,
            },
        }],
        raw={},
    )


def _make_text_response(text: str) -> ChatResponse:
    """Create a simple text ChatResponse."""
    return ChatResponse(
        content=text,
        model="mock-model",
        finish_reason="end_turn",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        tool_calls=None,
        raw={},
    )


def _write_yaml(tmp_path: Path, content: str) -> Path:
    """Write a YAML file and return its path."""
    p = tmp_path / "app.yaml"
    p.write_text(textwrap.dedent(content))
    return p


async def _compile_and_bootstrap(
    yaml_path: Path, mock_provider: MockLLMProvider
) -> tuple:
    """Compile and bootstrap an app with a mock provider.

    Returns (app, compiled, boot_result).
    """
    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.loader import load_modules
    from digitorn.core.runtime.app import RuntimeApp
    from digitorn.core.runtime.bootstrap import bootstrap
    from digitorn.modules.registry import ModuleRegistry

    registry = ModuleRegistry()
    load_modules(registry, load_all=True)

    compiler = AppYAMLCompiler(registry)
    compiled = compiler.compile_file(yaml_path)

    # Patch _resolve_provider to return our mock
    with patch(
        "digitorn.core.runtime.bootstrap._resolve_provider",
        return_value=mock_provider,
    ):
        boot_result = await bootstrap(compiled, registry)

    app = RuntimeApp(
        app_id=compiled.app_id,
        execution=compiled.execution,
        contexts=boot_result["contexts"],
        modules=boot_result["modules"],
        context_builder=boot_result["context_builder"],
    )

    return app, compiled, boot_result


# Common YAML template (uses inline brain — no real provider needed)
_ONESHOT_YAML = """
    app:
      app_id: {app_id}
      name: "{name}"

    modules:
      hello: {{}}

    agents:
      - id: assistant
        role: assistant
        brain:
          provider: mock
          model: mock-model
          backend: openai_compat
        system_prompt: "{prompt}"

    execution:
      mode: {mode}
      {extra}

    capabilities:
      default_policy: auto
      {capabilities}
"""


# ---------------------------------------------------------------------------
# Tests — One-Shot Simple
# ---------------------------------------------------------------------------


class TestE2EOneShotSimple:
    """One-shot mode: no tools used, just text in → text out."""

    @pytest.fixture
    def yaml_path(self, tmp_path: Path) -> Path:
        return _write_yaml(tmp_path, _ONESHOT_YAML.format(
            app_id="test-oneshot",
            name="Test One-Shot",
            prompt="You are a test assistant.",
            mode="one_shot",
            extra="input:\n        type: text\n      output:\n        type: text",
            capabilities="",
        ))

    @pytest.mark.asyncio
    async def test_simple_text_response(self, yaml_path: Path):
        """Full pipeline: compile → bootstrap → run_one_shot → text result."""
        mock_provider = MockLLMProvider(responses=[
            _make_text_response("Bonjour ! Comment puis-je vous aider ?"),
        ])

        app, compiled, _ = await _compile_and_bootstrap(yaml_path, mock_provider)

        result = await app.run_one_shot("Dis bonjour en français")

        assert result.content == "Bonjour ! Comment puis-je vous aider ?"
        assert result.error is None
        assert result.tool_calls_count == 0

        # Verify the provider was called with correct messages
        assert len(mock_provider.call_log) == 1
        call = mock_provider.call_log[0]
        roles = [m[0] for m in call["messages"]]
        assert "system" in roles
        assert "user" in roles
        # Should have tools (direct mode with few tools, or discovery meta-tools)
        assert len(call["tools"]) > 0

        await app.shutdown()


class TestE2EOneShotWithToolUse:
    """One-shot mode: agent uses tools to fulfill the request."""

    @pytest.fixture
    def yaml_path(self, tmp_path: Path) -> Path:
        return _write_yaml(tmp_path, _ONESHOT_YAML.format(
            app_id="test-tools",
            name="Test Tool Use",
            prompt="You are a test assistant. Use tools when needed.",
            mode="one_shot",
            extra="input:\n        type: text\n      output:\n        type: text",
            capabilities="",
        ))

    @pytest.mark.asyncio
    async def test_agent_uses_search_then_execute(self, yaml_path: Path):
        """Agent searches for tools, then executes one."""
        mock_provider = MockLLMProvider(responses=[
            _make_tool_call_response("search_tools", {"query": "hello greeting"}),
            _make_tool_call_response("execute_tool", {
                "name": "hello.say_hello",
                "params": {"name": "Paul"},
            }),
            _make_text_response("J'ai utilisé l'outil hello : Hello, Paul!"),
        ])

        app, compiled, _ = await _compile_and_bootstrap(yaml_path, mock_provider)

        tool_calls_log: list[tuple[str, dict, Any]] = []

        async def on_tool_call(name: str, params: dict, result: Any) -> None:
            tool_calls_log.append((name, params, result))

        result = await app.run_one_shot("Dis bonjour à Paul", on_tool_call=on_tool_call)

        assert "Hello, Paul!" in result.content
        assert result.tool_calls_count == 2
        assert result.turns_used == 3

        # Verify tool calls were tracked
        assert len(tool_calls_log) == 2
        assert tool_calls_log[0][0] == "search_tools"
        assert tool_calls_log[1][0] == "execute_tool"

        # Verify execute_tool actually called hello.say_hello
        execute_result = tool_calls_log[1][2]
        assert execute_result.success
        assert execute_result.data == "Hello, Paul!"

        await app.shutdown()


class TestE2EOneShotJsonOutput:
    """One-shot mode with JSON output extraction."""

    @pytest.fixture
    def yaml_path(self, tmp_path: Path) -> Path:
        return _write_yaml(tmp_path, _ONESHOT_YAML.format(
            app_id="test-json",
            name="Test JSON Output",
            prompt="Always respond in JSON format.",
            mode="one_shot",
            extra="input:\n        type: text\n      output:\n        type: json",
            capabilities="",
        ))

    @pytest.mark.asyncio
    async def test_json_extraction_from_code_block(self, yaml_path: Path):
        """JSON output is extracted from markdown code block."""
        mock_provider = MockLLMProvider(responses=[
            _make_text_response(
                'Here is the analysis:\n```json\n{"score": 95, "bugs": 0}\n```\nAll good!'
            ),
        ])

        app, compiled, _ = await _compile_and_bootstrap(yaml_path, mock_provider)

        result = await app.run_one_shot("Analyse this code")

        parsed = json.loads(result.content)
        assert parsed["score"] == 95
        assert parsed["bugs"] == 0

        await app.shutdown()


class TestE2EConversation:
    """Conversation mode with scripted input/output."""

    @pytest.fixture
    def yaml_path(self, tmp_path: Path) -> Path:
        return _write_yaml(tmp_path, _ONESHOT_YAML.format(
            app_id="test-chat",
            name="Test Chat",
            prompt="You are a chatbot.",
            mode="conversation",
            extra="greeting: \"Welcome! How can I help?\"",
            capabilities="",
        ))

    @pytest.mark.asyncio
    async def test_conversation_two_turns_then_quit(self, yaml_path: Path):
        """Two user messages then quit."""
        mock_provider = MockLLMProvider(responses=[
            _make_text_response("I'm doing great, thanks!"),
            _make_text_response("Paris is the capital of France."),
        ])

        app, compiled, _ = await _compile_and_bootstrap(yaml_path, mock_provider)

        user_inputs = iter(["How are you?", "What's the capital of France?", "/quit"])
        outputs: list[str] = []

        async def mock_input(prompt: str) -> str:
            return next(user_inputs)

        def mock_output(text: str) -> None:
            outputs.append(text)

        await app.run_conversation(
            input_fn=mock_input,
            output_fn=mock_output,
        )

        all_output = " ".join(outputs)
        assert "Welcome" in all_output
        assert "great" in all_output
        assert "Paris" in all_output

        assert len(mock_provider.call_log) == 2

        await app.shutdown()


class TestE2EToolCallbackDisplay:
    """Verify tool call callbacks fire with correct data."""

    @pytest.fixture
    def yaml_path(self, tmp_path: Path) -> Path:
        return _write_yaml(tmp_path, _ONESHOT_YAML.format(
            app_id="test-callback",
            name="Test Callbacks",
            prompt="Use tools.",
            mode="one_shot",
            extra="input:\n        type: text\n      output:\n        type: text",
            capabilities="",
        ))

    @pytest.mark.asyncio
    async def test_tool_callback_receives_action_result(self, yaml_path: Path):
        """on_tool_call callback receives the ActionResult from execute_tool."""
        mock_provider = MockLLMProvider(responses=[
            _make_tool_call_response("list_categories", {}),
            _make_text_response("Found some categories!"),
        ])

        app, compiled, _ = await _compile_and_bootstrap(yaml_path, mock_provider)

        callbacks: list[tuple[str, dict, Any]] = []

        async def on_tool_call(name: str, params: dict, result: Any) -> None:
            callbacks.append((name, params, result))

        result = await app.run_one_shot("List available tools", on_tool_call=on_tool_call)

        assert len(callbacks) == 1
        name, params, tool_result = callbacks[0]
        assert name == "list_categories"
        assert tool_result.success
        categories = tool_result.data["categories"]
        assert any("hello" in str(cat) for cat in categories)

        await app.shutdown()


class TestE2ESecurityBlocking:
    """Verify that blocked tools are invisible to the agent."""

    @pytest.fixture
    def yaml_path(self, tmp_path: Path) -> Path:
        return _write_yaml(tmp_path, _ONESHOT_YAML.format(
            app_id="test-security",
            name="Test Security",
            prompt="Use tools.",
            mode="one_shot",
            extra="input:\n        type: text\n      output:\n        type: text",
            capabilities="""deny:
        - module: hello
          actions: [say_hello]""",
        ))

    @pytest.mark.asyncio
    async def test_blocked_action_not_in_search(self, yaml_path: Path):
        """A denied action should not appear in search results."""
        mock_provider = MockLLMProvider(responses=[
            _make_tool_call_response("search_tools", {"query": "hello greeting say"}),
            _make_text_response("No hello tool found."),
        ])

        app, compiled, _ = await _compile_and_bootstrap(yaml_path, mock_provider)

        callbacks: list[tuple[str, dict, Any]] = []

        async def on_tool_call(name: str, params: dict, result: Any) -> None:
            callbacks.append((name, params, result))

        result = await app.run_one_shot("Find hello tools", on_tool_call=on_tool_call)

        assert len(callbacks) == 1
        search_name, _, search_result = callbacks[0]
        assert search_name == "search_tools"
        result_str = str(search_result.data)
        assert "say_hello" not in result_str

        await app.shutdown()
