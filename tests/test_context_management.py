"""Tests for context management - overflow detection, compaction, and auto-compact.

Tests cover:
1. ContextWindowConfig - effective_max, defaults
2. ContextConfig in YAML schema - parsing and validation
3. CompiledContextConfig - compilation
4. TurnState with real limits - pressure calculation
5. Overflow detection - _is_context_overflow
6. Emergency compaction - _emergency_compact
7. Safe split point - _find_safe_split_point
8. Auto-compact hook injection - bootstrap logic
9. Provider context window detection - model lookup
10. Agent loop overflow recovery - catch 400, compact, retry
"""

from __future__ import annotations

import asyncio
from dataclasses import field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.core.runtime.types import AgentContext, ContextWindowConfig, TurnResult
from digitorn.core.runtime.hooks import (
    TurnState,
    _find_safe_split_point,
    _do_truncate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_messages(count: int, char_per_msg: int = 100) -> list[dict]:
    """Build a message list with system + N conversation messages."""
    messages = [{"role": "system", "content": "System prompt."}]
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({"role": role, "content": "x" * char_per_msg})
    return messages


def _make_tool_call_sequence() -> list[dict]:
    """Build messages with a tool call sequence (assistant + tool results)."""
    return [
        {"role": "system", "content": "System."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Let me check.", "tool_calls": [
            {"id": "call_1", "function": {"name": "search", "arguments": "{}"}},
            {"id": "call_2", "function": {"name": "list", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result1"},
        {"role": "tool", "tool_call_id": "call_2", "content": "result2"},
        {"role": "assistant", "content": "Here is the answer."},
        {"role": "user", "content": "Thanks"},
        {"role": "assistant", "content": "You're welcome."},
    ]


# ===========================================================================
# 1. ContextWindowConfig
# ===========================================================================


class TestContextWindowConfig:

    def test_defaults(self):
        cfg = ContextWindowConfig()
        assert cfg.max_tokens == 128_000
        assert cfg.output_reserved == 4096
        assert cfg.strategy == "summarize"
        assert cfg.keep_recent == 10
        assert cfg.compression_trigger == 0.75
        assert cfg.auto_compact is True

    def test_effective_max(self):
        cfg = ContextWindowConfig(max_tokens=131_072, output_reserved=16_000)
        assert cfg.effective_max == 131_072 - 16_000

    def test_effective_max_floor(self):
        """effective_max never goes below 1."""
        cfg = ContextWindowConfig(max_tokens=100, output_reserved=200)
        assert cfg.effective_max == 1


# ===========================================================================
# 2. TurnState with real limits
# ===========================================================================


class TestTurnStateRealLimits:

    def test_default_limits(self):
        state = TurnState(
            messages=[], turn=0, max_turns=10,
            tool_calls_count=0, agent_id="test",
        )
        assert state.max_context_tokens == 128_000
        assert state.output_reserved == 4096

    def test_custom_limits(self):
        state = TurnState(
            messages=[], turn=0, max_turns=10,
            tool_calls_count=0, agent_id="test",
            max_context_tokens=200_000, output_reserved=8000,
        )
        assert state.effective_max_tokens == 200_000 - 8000

    def test_pressure_with_real_limits(self):
        """Token pressure uses effective_max_tokens."""
        # 50 messages × 800 chars = 40000 chars → ~13333 tokens (÷3)
        messages = _make_messages(50, char_per_msg=800)
        state = TurnState(
            messages=messages, turn=0, max_turns=10,
            tool_calls_count=0, agent_id="test",
            max_context_tokens=20_000, output_reserved=0,
        )
        # ~13333 tokens / 20000 max = ~0.67
        assert 0.5 < state.token_pressure < 0.8

    def test_pressure_with_small_context(self):
        """High pressure with small context window."""
        messages = _make_messages(10, char_per_msg=400)
        state = TurnState(
            messages=messages, turn=0, max_turns=10,
            tool_calls_count=0, agent_id="test",
            max_context_tokens=1_000, output_reserved=0,
        )
        # ~1000 tokens / 1000 max = ~1.0
        assert state.token_pressure >= 0.8


# ===========================================================================
# 3. Safe split point
# ===========================================================================


class TestSafeSplitPoint:

    def test_simple_split(self):
        """Normal messages split cleanly."""
        conv = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
            {"role": "assistant", "content": "d"},
        ]
        assert _find_safe_split_point(conv, 2) == 2

    def test_preserves_tool_sequence(self):
        """Split doesn't break assistant+tool pairs."""
        conv = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "let me check", "tool_calls": [
                {"id": "c1", "function": {"name": "f", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "q2"},
        ]
        # Asking for 2 would try to split at index 3, but that's inside a tool sequence
        safe = _find_safe_split_point(conv, 2)
        # Should keep at least the tool sequence intact
        assert safe >= 2

    def test_all_kept(self):
        conv = [{"role": "user", "content": "a"}]
        assert _find_safe_split_point(conv, 5) == 1


# ===========================================================================
# 4. Overflow detection
# ===========================================================================


class TestOverflowDetection:

    def test_detects_context_length(self):
        from digitorn.core.runtime.agent_loop import _is_context_overflow

        exc = Exception(
            "Error code: 400 - This model's maximum context length is 131072 tokens. "
            "However, you requested 248135 tokens."
        )
        assert _is_context_overflow(exc) is True

    def test_detects_reduce_length(self):
        from digitorn.core.runtime.agent_loop import _is_context_overflow

        exc = Exception("Please reduce the length of the messages or completion.")
        assert _is_context_overflow(exc) is True

    def test_detects_context_length_exceeded(self):
        from digitorn.core.runtime.agent_loop import _is_context_overflow

        exc = Exception("context_length_exceeded")
        assert _is_context_overflow(exc) is True

    def test_ignores_other_errors(self):
        from digitorn.core.runtime.agent_loop import _is_context_overflow

        assert _is_context_overflow(Exception("rate limit exceeded")) is False
        assert _is_context_overflow(Exception("authentication failed")) is False
        assert _is_context_overflow(ValueError("something else")) is False


# ===========================================================================
# 5. Emergency compaction
# ===========================================================================


class TestEmergencyCompact:

    @pytest.mark.asyncio
    async def test_emergency_truncates(self):
        from digitorn.core.runtime.agent_loop import _emergency_compact

        ctx = AgentContext(
            agent_id="test", role="assistant",
            provider=MagicMock(), system_prompt="System",
            tools=[], generation_params={},
            context_config=ContextWindowConfig(keep_recent=10),
        )

        messages = _make_messages(30, char_per_msg=200)
        original_len = len(messages)

        await _emergency_compact(ctx, messages)

        # Should be significantly reduced
        assert len(messages) < original_len
        # System message preserved
        assert messages[0]["role"] == "system"
        # Compaction note present
        assert "compacted" in messages[1]["content"].lower()

    @pytest.mark.asyncio
    async def test_emergency_with_few_messages(self):
        """Emergency compact with too few messages does nothing."""
        from digitorn.core.runtime.agent_loop import _emergency_compact

        ctx = AgentContext(
            agent_id="test", role="assistant",
            provider=MagicMock(), system_prompt="System",
            tools=[], generation_params={},
            context_config=ContextWindowConfig(keep_recent=10),
        )

        messages = _make_messages(3)
        original_len = len(messages)

        await _emergency_compact(ctx, messages)
        assert len(messages) == original_len  # unchanged


# ===========================================================================
# 6. Agent loop overflow recovery
# ===========================================================================


class TestAgentLoopOverflowRecovery:

    @pytest.mark.asyncio
    async def test_recovers_from_overflow(self):
        """Agent loop catches overflow error, compacts, and retries."""
        from digitorn.core.runtime.agent_loop import agent_turn

        call_count = 0

        async def mock_chat(messages, tools=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception(
                    "Error code: 400 - This model's maximum context length is 131072 tokens. "
                    "However, you requested 248135 tokens."
                )
            # After compaction, succeed
            return MagicMock(content="After compaction!", tool_calls=None)

        provider = AsyncMock()
        provider.chat = mock_chat

        ctx = AgentContext(
            agent_id="test", role="worker",
            provider=provider, system_prompt="Test",
            tools=[], generation_params={},
            context_config=ContextWindowConfig(keep_recent=5),
        )

        # Start with many messages
        messages = _make_messages(50, char_per_msg=500)

        result = await agent_turn(ctx, messages, max_turns=10, timeout=30.0)

        assert result.content == "After compaction!"
        assert result.error is None
        assert call_count == 2  # first failed, second succeeded

    @pytest.mark.asyncio
    async def test_non_overflow_error_propagated(self):
        """Non-overflow errors are not caught."""
        from digitorn.core.runtime.agent_loop import agent_turn

        provider = AsyncMock()
        provider.chat = AsyncMock(side_effect=Exception("rate limit exceeded"))

        ctx = AgentContext(
            agent_id="test", role="worker",
            provider=provider, system_prompt="Test",
            tools=[], generation_params={},
        )

        messages = [
            {"role": "system", "content": "Test"},
            {"role": "user", "content": "Hello"},
        ]

        result = await agent_turn(ctx, messages, max_turns=10, timeout=30.0)
        assert result.error == "rate limit exceeded"


# ===========================================================================
# 7. YAML schema + compiler integration
# ===========================================================================


class TestContextConfigSchema:

    def test_default_context_config(self):
        from digitorn.core.app.schema import ContextConfig

        cfg = ContextConfig()
        assert cfg.max_tokens == 0
        assert cfg.strategy == "summarize"
        assert cfg.compression_trigger == 0.75
        assert cfg.auto_compact is True

    def test_context_in_execution_config(self):
        from digitorn.core.app.schema import ExecutionConfig

        exe = ExecutionConfig(
            mode="conversation",
            context={
                "max_tokens": 131072,
                "strategy": "truncate",
                "keep_recent": 20,
                "compression_trigger": 0.6,
                "output_reserved": 16000,
            },
        )
        assert exe.context.max_tokens == 131072
        assert exe.context.strategy == "truncate"
        assert exe.context.keep_recent == 20
        assert exe.context.compression_trigger == 0.6
        assert exe.context.output_reserved == 16000

    def test_yaml_with_context(self):
        """Full YAML parsing with context config."""
        import yaml
        from digitorn.core.app.schema import AppDefinition

        raw = yaml.safe_load("""
app:
  app_id: test
  name: Test
agents:
  - id: assistant
    role: assistant
    brain:
      provider_id: test
    system_prompt: "Test"
execution:
  mode: conversation
  context:
    max_tokens: 131072
    strategy: summarize
    keep_recent: 30
    compression_trigger: 0.70
    output_reserved: 16000
    summary_max_tokens: 1500
    auto_compact: true
""")
        app = AppDefinition(**raw)
        assert app.execution.context.max_tokens == 131072
        assert app.execution.context.keep_recent == 30
        assert app.execution.context.compression_trigger == 0.70


class TestContextConfigCompiler:

    def test_compile_context_config(self):
        from digitorn.core.app.compiler import CompiledContextConfig

        cfg = CompiledContextConfig(
            max_tokens=131072,
            output_reserved=16000,
            strategy="summarize",
            keep_recent=20,
            compression_trigger=0.70,
            summary_max_tokens=1500,
        )
        assert cfg.max_tokens == 131072
        assert cfg.strategy == "summarize"

    def test_compiled_execution_has_context(self):
        from digitorn.core.app.compiler import CompiledExecution, CompiledContextConfig

        exe = CompiledExecution(
            mode="conversation",
            context=CompiledContextConfig(max_tokens=200000, strategy="truncate"),
        )
        assert exe.context.max_tokens == 200000
        assert exe.context.strategy == "truncate"


# ===========================================================================
# 8. Provider context window detection
# ===========================================================================


class TestProviderContextWindow:

    def test_deepseek_chat_lookup(self):
        from digitorn.modules.llm_provider.providers.openai_compat import _lookup_context_window

        assert _lookup_context_window("deepseek-chat") == 131_072

    def test_gpt4o_lookup(self):
        from digitorn.modules.llm_provider.providers.openai_compat import _lookup_context_window

        assert _lookup_context_window("gpt-4o") == 128_000

    def test_prefix_match(self):
        from digitorn.modules.llm_provider.providers.openai_compat import _lookup_context_window

        assert _lookup_context_window("gpt-4o-2024-05-13") == 128_000

    def test_unknown_model(self):
        from digitorn.modules.llm_provider.providers.openai_compat import _lookup_context_window

        assert _lookup_context_window("some-random-model") == 0

    def test_provider_info_includes_context_window(self):
        from digitorn.modules.llm_provider.providers.openai_compat import OpenAICompatProvider

        provider = OpenAICompatProvider(
            provider_id="test", model="deepseek-chat",
            api_key="test", base_url="http://localhost",
        )
        info = provider.get_info()
        assert info.capabilities.max_context_window == 131_072


# ===========================================================================
# 9. Truncate with tool call safety
# ===========================================================================


class TestTruncateWithToolSafety:

    def test_truncate_basic(self):
        messages = _make_messages(20, char_per_msg=100)
        system_msg = messages[0]
        conversation = messages[1:]
        to_compact = conversation[:-5]
        to_keep = conversation[-5:]

        _do_truncate(messages, system_msg, to_compact, to_keep)

        assert len(messages) == 7  # system + note + 5 kept
        assert messages[0]["role"] == "system"
        assert "compacted" in messages[1]["content"].lower()

    def test_truncate_preserves_system(self):
        messages = [
            {"role": "system", "content": "Important system prompt"},
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "old reply"},
            {"role": "user", "content": "recent"},
        ]
        system_msg = messages[0]
        conversation = messages[1:]

        _do_truncate(messages, system_msg, conversation[:2], conversation[2:])

        assert messages[0]["content"] == "Important system prompt"
        assert messages[-1]["content"] == "recent"


# ===========================================================================
# 10. Full integration - context config flows through bootstrap
# ===========================================================================


class TestBootstrapContextConfig:

    def test_resolve_context_config(self):
        from digitorn.core.runtime.bootstrap import _resolve_context_config

        compiled_ctx = MagicMock()
        compiled_ctx.max_tokens = 131072
        compiled_ctx.output_reserved = 16000
        compiled_ctx.strategy = "summarize"
        compiled_ctx.keep_recent = 20
        compiled_ctx.compression_trigger = 0.70
        compiled_ctx.summary_max_tokens = 1500
        compiled_ctx.auto_compact = True

        config = _resolve_context_config(compiled_ctx, {})
        assert config.max_tokens == 131072
        assert config.output_reserved == 16000
        assert config.strategy == "summarize"

    def test_refine_with_provider_capabilities(self):
        from digitorn.core.runtime.bootstrap import _refine_context_config_for_provider
        from digitorn.modules.llm_provider.providers.base import (
            ProviderCapabilities,
            ProviderInfo,
        )

        base = ContextWindowConfig(max_tokens=128_000)

        provider = MagicMock()
        provider.get_info.return_value = ProviderInfo(
            provider_id="test", backend="openai_compat", model="deepseek-chat",
            capabilities=ProviderCapabilities(max_context_window=131_072),
        )

        refined = _refine_context_config_for_provider(base, provider)
        assert refined.max_tokens == 131_072  # updated from provider

    def test_refine_without_capabilities(self):
        from digitorn.core.runtime.bootstrap import _refine_context_config_for_provider

        base = ContextWindowConfig(max_tokens=128_000)

        provider = MagicMock()
        provider.get_info.return_value = MagicMock(
            capabilities=MagicMock(max_context_window=0)
        )

        refined = _refine_context_config_for_provider(base, provider)
        assert refined.max_tokens == 128_000  # unchanged


# ===========================================================================
# 11. Per-brain context config
# ===========================================================================


class TestPerBrainContextConfig:

    def test_brain_context_in_yaml(self):
        """Per-brain context config parsed from YAML."""
        import yaml
        from digitorn.core.app.schema import AppDefinition

        raw = yaml.safe_load("""
app:
  app_id: multi-agent
  name: Multi-Agent
agents:
  - id: fast_agent
    role: assistant
    brain:
      provider_id: deepseek
      context:
        max_tokens: 131072
        strategy: truncate
        keep_recent: 30
        compression_trigger: 0.60
    system_prompt: "Fast agent"
  - id: smart_agent
    role: assistant
    brain:
      provider_id: claude
      context:
        max_tokens: 200000
        strategy: summarize
        keep_recent: 20
        compression_trigger: 0.80
    system_prompt: "Smart agent"
execution:
  mode: conversation
  context:
    strategy: summarize
    keep_recent: 10
""")
        app = AppDefinition(**raw)

        # Per-brain configs
        fast = app.agents[0].brain.context
        assert fast is not None
        assert fast.max_tokens == 131072
        assert fast.strategy == "truncate"
        assert fast.keep_recent == 30

        smart = app.agents[1].brain.context
        assert smart is not None
        assert smart.max_tokens == 200000
        assert smart.strategy == "summarize"

        # Execution-level default
        assert app.execution.context.keep_recent == 10

    def test_brain_without_context_inherits_default(self):
        """Brain without context inherits execution-level default."""
        import yaml
        from digitorn.core.app.schema import AppDefinition

        raw = yaml.safe_load("""
app:
  app_id: test
  name: Test
agents:
  - id: assistant
    role: assistant
    brain:
      provider_id: test
    system_prompt: "Test"
execution:
  mode: one_shot
  context:
    max_tokens: 64000
    strategy: truncate
""")
        app = AppDefinition(**raw)
        assert app.agents[0].brain.context is None  # not set → inherits
        assert app.execution.context.max_tokens == 64000

    def test_compiled_brain_has_context(self):
        """CompiledBrain carries per-brain context config."""
        from digitorn.core.app.compiler import CompiledBrain, CompiledContextConfig

        brain = CompiledBrain(
            provider_id="test",
            context=CompiledContextConfig(
                max_tokens=200000,
                strategy="summarize",
                keep_recent=20,
            ),
        )
        assert brain.context is not None
        assert brain.context.max_tokens == 200000
        assert brain.context.strategy == "summarize"

    def test_compiled_brain_without_context(self):
        from digitorn.core.app.compiler import CompiledBrain

        brain = CompiledBrain(provider_id="test")
        assert brain.context is None
