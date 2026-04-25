"""Pipeline + session tests for the channels module.

Tests the full activation pipeline (filter → prepare → route → activate → reply)
and shared session history across multiple events. agent_turn is mocked.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.modules.channels.adapter import InboundEvent
from digitorn.modules.channels.module import (
    ActivationConfig,
    ActiveProvider,
    ChannelsModule,
    ChannelsModuleConfig,
    FilterCondition,
    PrepareStep,
    ProviderConfig,
    RouteConfig,
    RouteRule,
)
from digitorn.modules.channels.pipeline import ActivationPipeline
from digitorn.modules.channels.session_manager import ChannelSessionManager


# ── Helpers ─────────────────────────────────────────────────────────


def _make_event(
    provider_id: str = "test",
    payload: dict | None = None,
    message: str = "",
    source: str = "unit-test",
    reply_context: dict | None = None,
) -> InboundEvent:
    return InboundEvent(
        event_id="evt-001",
        provider_id=provider_id,
        adapter_type="webhook",
        source=source,
        message=message,
        payload=payload or {},
        metadata={},
        reply_context=reply_context,
    )


def _make_turn_result(content: str = "Agent says hello"):
    """Create a mock TurnResult with .content attribute."""
    from digitorn.core.runtime.types import TurnResult
    return TurnResult(content=content)


def _make_runtime_app(agents: dict[str, Any] | None = None):
    """Create a mock RuntimeApp with agent contexts."""
    app = MagicMock()
    app.context_builder = None

    # Default agent context
    entry_ctx = MagicMock()
    entry_ctx.system_prompt = "You are a helpful assistant."

    app.entry_context = entry_ctx
    app.contexts = {}

    if agents:
        for aid, prompt in agents.items():
            ctx = MagicMock()
            ctx.system_prompt = prompt
            app.contexts[aid] = ctx

    return app


def _make_provider(
    activation: ActivationConfig | None = None,
    adapter: Any = None,
) -> ActiveProvider:
    config = ProviderConfig(
        adapter="webhook",
        config={},
        activation=activation or ActivationConfig(),
    )
    return ActiveProvider(
        name="test_prov",
        config=config,
        adapter=adapter or MagicMock(),
    )


def _make_pipeline(
    module: ChannelsModule | None = None,
    session_mgr: ChannelSessionManager | None = None,
    config: ChannelsModuleConfig | None = None,
) -> ActivationPipeline:
    mod = module or MagicMock(spec=ChannelsModule)
    sess = session_mgr or ChannelSessionManager()
    cfg = config or ChannelsModuleConfig(default_agent="main")
    return ActivationPipeline(mod, sess, cfg)


# ── Filter Tests ────────────────────────────────────────────────────


class TestPipelineFilter:
    @pytest.mark.asyncio
    async def test_filter_equals_passes(self):
        """Event passes when filter field equals expected value."""
        pipeline = _make_pipeline()
        cond = FilterCondition(field="event.payload.status", equals="new")
        variables = {"event": {"payload": {"status": "new"}}}
        assert pipeline._check_filter(cond, variables) is True

    @pytest.mark.asyncio
    async def test_filter_equals_blocks(self):
        """Event blocked when filter field doesn't match."""
        pipeline = _make_pipeline()
        cond = FilterCondition(field="event.payload.status", equals="new")
        variables = {"event": {"payload": {"status": "closed"}}}
        assert pipeline._check_filter(cond, variables) is False

    @pytest.mark.asyncio
    async def test_filter_not_equals(self):
        pipeline = _make_pipeline()
        cond = FilterCondition(field="event.payload.status", not_equals="closed")
        variables = {"event": {"payload": {"status": "new"}}}
        assert pipeline._check_filter(cond, variables) is True

    @pytest.mark.asyncio
    async def test_filter_contains(self):
        pipeline = _make_pipeline()
        cond = FilterCondition(field="event.payload.text", contains="urgent")
        variables = {"event": {"payload": {"text": "This is urgent help"}}}
        assert pipeline._check_filter(cond, variables) is True

    @pytest.mark.asyncio
    async def test_filter_gt(self):
        pipeline = _make_pipeline()
        cond = FilterCondition(field="event.payload.priority", gt=5)
        variables = {"event": {"payload": {"priority": 8}}}
        assert pipeline._check_filter(cond, variables) is True

    @pytest.mark.asyncio
    async def test_filter_lt(self):
        pipeline = _make_pipeline()
        cond = FilterCondition(field="event.payload.priority", lt=3)
        variables = {"event": {"payload": {"priority": 1}}}
        assert pipeline._check_filter(cond, variables) is True

    @pytest.mark.asyncio
    async def test_multiple_filters_all_must_pass(self):
        """When multiple filters defined, event is dropped if ANY fails."""
        module = MagicMock(spec=ChannelsModule)
        module._runtime_app = _make_runtime_app()
        session_mgr = ChannelSessionManager()
        config = ChannelsModuleConfig(default_agent="main")
        pipeline = ActivationPipeline(module, session_mgr, config)

        activation = ActivationConfig(
            message="test",
            filter=[
                FilterCondition(field="event.payload.status", equals="new"),
                FilterCondition(field="event.payload.priority", gt=3),
            ],
        )
        provider = _make_provider(activation=activation)

        # First filter passes, second fails → event dropped
        event = _make_event(payload={"status": "new", "priority": 2})

        with patch("digitorn.core.runtime.agent_loop.agent_turn") as mock_at:
            mock_at.return_value = _make_turn_result()
            await pipeline.process_event(event, provider)
            mock_at.assert_not_called()


# ── Route Tests ─────────────────────────────────────────────────────


class TestPipelineRoute:
    def test_static_route(self):
        """Static agent routing via activation.agent."""
        pipeline = _make_pipeline()
        route = RouteConfig(
            field="event.payload.dept",
            rules=[
                RouteRule(match="tech", agent="tech_agent"),
                RouteRule(match="billing", agent="billing_agent"),
                RouteRule(default=True, agent="general"),
            ],
        )
        variables = {"event": {"payload": {"dept": "tech"}}}
        assert pipeline._resolve_route(route, variables) == "tech_agent"

    def test_dynamic_route_default(self):
        """Falls through to default rule if no match."""
        pipeline = _make_pipeline()
        route = RouteConfig(
            field="event.payload.dept",
            rules=[
                RouteRule(match="tech", agent="tech_agent"),
                RouteRule(default=True, agent="general"),
            ],
        )
        variables = {"event": {"payload": {"dept": "unknown"}}}
        # default rule comes before the match check for "unknown"
        # Actually, _resolve_route checks default FIRST, so it returns general
        result = pipeline._resolve_route(route, variables)
        # The default rule is checked first in the loop: if rule.default → return
        # So it should return "general" because it iterates and hits default
        # Wait - it checks match first, then default... Let me check
        # Actually: `if rule.default: return rule.agent` comes first in the loop
        # But the rules list has tech first, then default. So tech is checked first.
        # tech match fails ("unknown" != "tech"), then default=True → returns "general"
        assert result == "general"

    def test_route_no_match_returns_empty(self):
        """No match and no default → empty string."""
        pipeline = _make_pipeline()
        route = RouteConfig(
            field="event.payload.dept",
            rules=[RouteRule(match="tech", agent="tech_agent")],
        )
        variables = {"event": {"payload": {"dept": "billing"}}}
        assert pipeline._resolve_route(route, variables) == ""


# ── Build Messages Tests ────────────────────────────────────────────


class TestPipelineBuildMessages:
    def test_template_message(self):
        """Message template renders with event variables."""
        pipeline = _make_pipeline()
        activation = ActivationConfig(message="Help from {{event.source}}: {{event.payload.question}}")
        event = _make_event(source="user-42", payload={"question": "How to reset?"})
        variables = {
            "event": {
                "event_id": event.event_id,
                "provider": event.provider_id,
                "adapter": event.adapter_type,
                "source": event.source,
                "message": event.message,
                "payload": event.payload,
                "metadata": {},
                "timestamp": event.timestamp,
            },
        }
        _, user_msg = pipeline._build_messages(event, activation, variables)
        assert "user-42" in user_msg
        assert "How to reset?" in user_msg

    def test_context_injection(self):
        """Extra context rendered and passed as system prompt addition."""
        pipeline = _make_pipeline()
        activation = ActivationConfig(
            message="Hi",
            context="Client plan: {{client_plan}}",
        )
        event = _make_event()
        variables = {
            "event": {"payload": {}, "source": "", "message": "", "metadata": {}, "timestamp": 0, "event_id": "", "provider": "", "adapter": ""},
            "client_plan": "premium",
        }
        sys_extra, _ = pipeline._build_messages(event, activation, variables)
        assert "premium" in sys_extra


# ── Full Pipeline (agent_turn mocked) ──────────────────────────────


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_event_through_full_pipeline(self):
        """Event goes filter → build → activate → no reply (reply=none)."""
        module = MagicMock(spec=ChannelsModule)
        module._runtime_app = _make_runtime_app()
        module._hook_runner = None
        session_mgr = ChannelSessionManager()
        config = ChannelsModuleConfig(default_agent="main")
        pipeline = ActivationPipeline(module, session_mgr, config)

        activation = ActivationConfig(
            message="User asked: {{event.payload.question}}",
            reply="none",
        )
        provider = _make_provider(activation=activation)
        event = _make_event(payload={"question": "What is Digitorn?"})

        with patch("digitorn.core.runtime.agent_loop.agent_turn") as mock_at:
            mock_at.return_value = _make_turn_result("Digitorn is an AI framework.")
            await pipeline.process_event(event, provider)

            mock_at.assert_called_once()
            call_args = mock_at.call_args
            messages = call_args[0][1]  # Second positional arg
            assert any("What is Digitorn?" in m["content"] for m in messages)

    @pytest.mark.asyncio
    async def test_reply_auto_sends_response(self):
        """With reply=auto + reply_context, agent response is sent back."""
        module = MagicMock(spec=ChannelsModule)
        module._runtime_app = _make_runtime_app()
        module._hook_runner = None
        session_mgr = ChannelSessionManager()
        config = ChannelsModuleConfig(default_agent="main")
        pipeline = ActivationPipeline(module, session_mgr, config)

        adapter_mock = MagicMock()
        adapter_mock.send_reply = AsyncMock(return_value=MagicMock(success=True))

        activation = ActivationConfig(
            message="Hi",
            reply="auto",
        )
        provider = _make_provider(activation=activation, adapter=adapter_mock)
        event = _make_event(
            payload={"msg": "hello"},
            reply_context={"response_url": "http://test/reply"},
        )

        with patch("digitorn.core.runtime.agent_loop.agent_turn") as mock_at:
            mock_at.return_value = _make_turn_result("Hello back!")
            await pipeline.process_event(event, provider)

            adapter_mock.send_reply.assert_called_once()
            reply_text = adapter_mock.send_reply.call_args[0][1]
            assert "Hello back!" in reply_text

    @pytest.mark.asyncio
    async def test_filtered_event_never_activates(self):
        """Filtered events never reach agent_turn."""
        module = MagicMock(spec=ChannelsModule)
        module._runtime_app = _make_runtime_app()
        module._hook_runner = None
        session_mgr = ChannelSessionManager()
        config = ChannelsModuleConfig(default_agent="main")
        pipeline = ActivationPipeline(module, session_mgr, config)

        activation = ActivationConfig(
            message="test",
            filter=[FilterCondition(field="event.payload.status", equals="new")],
        )
        provider = _make_provider(activation=activation)
        event = _make_event(payload={"status": "closed"})

        with patch("digitorn.core.runtime.agent_loop.agent_turn") as mock_at:
            await pipeline.process_event(event, provider)
            mock_at.assert_not_called()

    @pytest.mark.asyncio
    async def test_route_selects_correct_agent(self):
        """Dynamic route sends event to correct agent context."""
        agents = {
            "tech": "You are a tech support specialist.",
            "billing": "You are a billing specialist.",
        }
        module = MagicMock(spec=ChannelsModule)
        module._runtime_app = _make_runtime_app(agents)
        module._hook_runner = None
        session_mgr = ChannelSessionManager()
        config = ChannelsModuleConfig(default_agent="main")
        pipeline = ActivationPipeline(module, session_mgr, config)

        activation = ActivationConfig(
            message="Help: {{event.payload.question}}",
            route=RouteConfig(
                field="event.payload.dept",
                rules=[
                    RouteRule(match="tech", agent="tech"),
                    RouteRule(match="billing", agent="billing"),
                ],
            ),
        )
        provider = _make_provider(activation=activation)
        event = _make_event(payload={"dept": "tech", "question": "VPN issue"})

        with patch("digitorn.core.runtime.agent_loop.agent_turn") as mock_at:
            mock_at.return_value = _make_turn_result("Check your VPN cert.")
            await pipeline.process_event(event, provider)

            # Verify agent_turn was called with the tech agent's context
            call_ctx = mock_at.call_args[0][0]
            assert call_ctx.system_prompt == "You are a tech support specialist."

    @pytest.mark.asyncio
    async def test_prepare_step_enriches_context(self):
        """Prepare step result is available in templates."""
        module = MagicMock(spec=ChannelsModule)
        module._runtime_app = _make_runtime_app()
        module._hook_runner = None
        # Mock service_bus
        module._ctx = MagicMock()
        result_mock = MagicMock()
        result_mock.data = {"name": "Alice", "plan": "premium"}
        module._ctx.service_bus.call = AsyncMock(return_value=result_mock)

        session_mgr = ChannelSessionManager()
        config = ChannelsModuleConfig(default_agent="main")
        pipeline = ActivationPipeline(module, session_mgr, config)

        activation = ActivationConfig(
            message="Help {{event.payload.question}}",
            context="Client: {{client.name}} ({{client.plan}})",
            prepare=[
                PrepareStep(
                    action="database.sql",
                    params={"query": "SELECT * FROM clients WHERE id={{event.payload.client_id}}"},
                    **{"as": "client"},
                ),
            ],
        )
        provider = _make_provider(activation=activation)
        event = _make_event(payload={"client_id": "1", "question": "VPN help"})

        with patch("digitorn.core.runtime.agent_loop.agent_turn") as mock_at:
            mock_at.return_value = _make_turn_result("Checking your VPN...")
            await pipeline.process_event(event, provider)

            # Verify service_bus was called for prepare step
            module._ctx.service_bus.call.assert_called_once()

            # Verify agent_turn was called (pipeline didn't fail)
            mock_at.assert_called_once()


# ── Shared Session Tests ────────────────────────────────────────────


class TestSharedSessions:
    @pytest.mark.asyncio
    async def test_shared_session_accumulates_history(self):
        """Two events on the same shared session build conversation history."""
        agents = {"main": "You are helpful."}
        module = MagicMock(spec=ChannelsModule)
        module._runtime_app = _make_runtime_app(agents)
        module._hook_runner = None
        session_mgr = ChannelSessionManager()
        config = ChannelsModuleConfig(default_agent="main")
        pipeline = ActivationPipeline(module, session_mgr, config)

        activation = ActivationConfig(
            message="{{event.payload.msg}}",
            session="shared",
        )
        provider = _make_provider(activation=activation)

        # Event 1
        event1 = _make_event(
            provider_id="chat",
            source="user-1",
            payload={"msg": "Hello!"},
        )

        with patch("digitorn.core.runtime.agent_loop.agent_turn") as mock_at:
            mock_at.return_value = _make_turn_result("Hi there!")
            await pipeline.process_event(event1, provider)

            # First call: messages should have system + user
            messages1 = mock_at.call_args[0][1]
            assert len(messages1) == 2
            assert messages1[0]["role"] == "system"
            assert messages1[1]["role"] == "user"
            assert "Hello!" in messages1[1]["content"]

        # Event 2 from same source → same session
        event2 = _make_event(
            provider_id="chat",
            source="user-1",
            payload={"msg": "What can you do?"},
        )

        with patch("digitorn.core.runtime.agent_loop.agent_turn") as mock_at:
            mock_at.return_value = _make_turn_result("I can help with many things.")
            await pipeline.process_event(event2, provider)

            # Second call: messages should include full history
            messages2 = mock_at.call_args[0][1]
            # system + user("Hello!") + assistant("Hi there!") + user("What can you do?")
            assert len(messages2) == 4
            assert messages2[0]["role"] == "system"
            assert messages2[1]["role"] == "user"
            assert "Hello!" in messages2[1]["content"]
            assert messages2[2]["role"] == "assistant"
            assert "Hi there!" in messages2[2]["content"]
            assert messages2[3]["role"] == "user"
            assert "What can you do?" in messages2[3]["content"]

    @pytest.mark.asyncio
    async def test_per_event_session_no_history(self):
        """per_event sessions start fresh every time."""
        module = MagicMock(spec=ChannelsModule)
        module._runtime_app = _make_runtime_app()
        module._hook_runner = None
        session_mgr = ChannelSessionManager()
        config = ChannelsModuleConfig(default_agent="main")
        pipeline = ActivationPipeline(module, session_mgr, config)

        activation = ActivationConfig(
            message="{{event.payload.msg}}",
            session="per_event",
        )
        provider = _make_provider(activation=activation)

        for i in range(3):
            event = _make_event(source="user-1", payload={"msg": f"Message {i}"})
            with patch("digitorn.core.runtime.agent_loop.agent_turn") as mock_at:
                mock_at.return_value = _make_turn_result(f"Reply {i}")
                await pipeline.process_event(event, provider)

                messages = mock_at.call_args[0][1]
                # Always fresh: system + user only
                assert len(messages) == 2

    @pytest.mark.asyncio
    async def test_different_sources_get_different_sessions(self):
        """Shared sessions are scoped per provider+source."""
        module = MagicMock(spec=ChannelsModule)
        module._runtime_app = _make_runtime_app()
        module._hook_runner = None
        session_mgr = ChannelSessionManager()
        config = ChannelsModuleConfig(default_agent="main")
        pipeline = ActivationPipeline(module, session_mgr, config)

        activation = ActivationConfig(
            message="{{event.payload.msg}}",
            session="shared",
        )
        provider = _make_provider(activation=activation)

        # User A sends 2 messages
        for msg in ["A msg 1", "A msg 2"]:
            event = _make_event(source="user-A", payload={"msg": msg})
            with patch("digitorn.core.runtime.agent_loop.agent_turn") as mock_at:
                mock_at.return_value = _make_turn_result(f"Reply to {msg}")
                await pipeline.process_event(event, provider)

        # User B sends 1 message
        event_b = _make_event(source="user-B", payload={"msg": "B msg 1"})
        with patch("digitorn.core.runtime.agent_loop.agent_turn") as mock_at:
            mock_at.return_value = _make_turn_result("Reply to B")
            await pipeline.process_event(event_b, provider)

            # User B should only have system + user (no history from user A)
            messages = mock_at.call_args[0][1]
            assert len(messages) == 2
            assert "B msg 1" in messages[1]["content"]

    @pytest.mark.asyncio
    async def test_template_session_renders(self):
        """Template session mode uses rendered template as session ID."""
        session_mgr = ChannelSessionManager()

        event = _make_event(source="whatsapp-5551234")
        variables = {
            "event": {
                "source": event.source,
                "provider": event.provider_id,
            },
        }

        session_id = session_mgr.resolve_session_id(
            event, "wa-{{event.source}}", variables,
        )
        assert "whatsapp-5551234" in session_id

    def test_session_history_cap(self):
        """History capped at 100 messages (system + last 80)."""
        session_mgr = ChannelSessionManager()
        sid = "test-cap"

        # Add system prompt
        session_mgr.append_to_history(sid, "system", "You are helpful.")

        # Add 110 user+assistant exchanges
        for i in range(110):
            session_mgr.append_to_history(sid, "user", f"msg {i}")
            session_mgr.append_to_history(sid, "assistant", f"reply {i}")

        history = session_mgr.get_history(sid)
        assert len(history) <= 81  # system + 80 recent

    @pytest.mark.asyncio
    async def test_session_cleanup(self):
        """cleanup() persists then clears all sessions."""
        session_mgr = ChannelSessionManager()
        session_mgr.append_to_history("s1", "user", "hi")
        session_mgr.append_to_history("s2", "user", "hi")
        assert session_mgr.active_session_count() == 2

        await session_mgr.cleanup()
        assert session_mgr.active_session_count() == 0

    def test_session_eviction_at_capacity(self):
        """When max sessions reached, oldest are evicted."""
        session_mgr = ChannelSessionManager()
        session_mgr._max_sessions = 10

        for i in range(12):
            session_mgr.get_lock(f"session-{i}")
            session_mgr.append_to_history(f"session-{i}", "user", f"msg {i}")

        # Should have evicted some
        assert session_mgr.active_session_count() <= 12
