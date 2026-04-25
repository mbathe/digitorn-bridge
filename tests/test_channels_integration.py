"""Integration tests for the channels module.

Tests the full lifecycle: module init → provider start → inbound event →
pipeline → agent activation → reply:auto → outbound delivery.

Also tests: concurrent activations, session management, RuntimeApp wiring,
backward compatibility, module actions, and edge cases.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.core.app.channels.base import ChannelPayload, DeliveryResult
from digitorn.modules.channels.adapter import (
    AdapterCapabilities,
    BaseChannelAdapter,
    InboundCallback,
    InboundEvent,
    make_event_id,
)
from digitorn.modules.channels.module import (
    ActivationConfig,
    ChannelsModule,
    ChannelsModuleConfig,
    FilterCondition,
    PrepareStep,
    ProviderConfig,
    RouteConfig,
    RouteRule,
)
from digitorn.modules.channels.pipeline import ActivationPipeline
from digitorn.modules.channels.security import filter_secrets, sanitize_payload
from digitorn.modules.channels.session_manager import ChannelSessionManager
from digitorn.modules.channels.template import render


# ── Test fixtures ────────────────────────────────────────────────────


class RecordingAdapter(BaseChannelAdapter):
    """Adapter that records all operations for verification."""

    CHANNEL_ID = "recording"
    CHANNEL_NAME = "Recording"
    CHANNEL_VERSION = "1.0.0"
    SUPPORTS_INBOUND = True
    SUPPORTS_OUTBOUND = True

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        super().__init__(channel_config=channel_config)
        self.deliveries: list[tuple[str, ChannelPayload, dict]] = []
        self.replies: list[tuple[dict, str]] = []
        self.started = False
        self.stopped = False
        self._callback: InboundCallback | None = None

    async def start_listener(self, callback: InboundCallback) -> None:
        self.started = True
        self._callback = callback

    async def stop_listener(self) -> None:
        self.stopped = True

    async def deliver(
        self, app_id: str, payload: ChannelPayload, config: dict[str, Any]
    ) -> DeliveryResult:
        self.deliveries.append((app_id, payload, config))
        return DeliveryResult(success=True, channel_id="recording", delivery_id=f"del-{len(self.deliveries)}")

    async def send_reply(
        self, reply_context: dict[str, Any], text: str, payload=None
    ) -> DeliveryResult:
        self.replies.append((reply_context, text))
        return DeliveryResult(success=True, channel_id="recording", delivery_id="reply-ok")

    def adapter_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(supports_inbound=True, supports_outbound=True)

    async def inject_event(self, event: InboundEvent) -> None:
        if self._callback:
            await self._callback(event)


class FailingAdapter(BaseChannelAdapter):
    """Adapter that always fails delivery."""

    CHANNEL_ID = "failing"
    CHANNEL_NAME = "Failing"
    CHANNEL_VERSION = "1.0.0"
    SUPPORTS_INBOUND = False
    SUPPORTS_OUTBOUND = True

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        super().__init__(channel_config=channel_config)

    async def deliver(
        self, app_id: str, payload: ChannelPayload, config: dict[str, Any]
    ) -> DeliveryResult:
        return DeliveryResult(
            success=False,
            channel_id="failing",
            error="Simulated failure",
            retryable=True,
        )

    def adapter_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(supports_inbound=False, supports_outbound=True)


def _make_event(
    provider: str = "test",
    source: str = "127.0.0.1",
    message: str = "hello",
    payload: dict | None = None,
    reply_context: dict | None = None,
) -> InboundEvent:
    return InboundEvent(
        event_id=make_event_id(),
        provider_id=provider,
        adapter_type="recording",
        source=source,
        message=message,
        payload=payload or {},
        reply_context=reply_context or {"_app_id": "test-app"},
    )


# ── Module Lifecycle Tests ───────────────────────────────────────────


class TestModuleLifecycle:
    @pytest.mark.asyncio
    async def test_start_with_providers(self):
        """Module creates providers on config_update, activates on start_listeners."""
        module = ChannelsModule()
        await module.on_start()
        await module.on_config_update({
            "providers": {
                "test1": {"adapter": "log", "config": {"level": "info"}},
                "test2": {"adapter": "log", "config": {"level": "debug"}, "enabled": False},
            }
        })

        assert "test1" in module._providers
        assert "test2" not in module._providers  # disabled
        assert module._providers["test1"].status == "ready"  # not active yet

        # Listeners start when run_background calls start_listeners
        await module.start_listeners()
        assert module._providers["test1"].status == "active"

        await module.on_stop()

    @pytest.mark.asyncio
    async def test_start_empty_config(self):
        """Module handles empty config gracefully."""
        module = ChannelsModule()
        module._config = {}
        await module.on_start()
        assert len(module._providers) == 0
        await module.on_stop()

    @pytest.mark.asyncio
    async def test_start_invalid_adapter(self):
        """Module logs error but doesn't crash on invalid adapter."""
        module = ChannelsModule()
        module._config = {
            "providers": {
                "bad": {"adapter": "nonexistent_adapter_xyz"},
            }
        }
        await module.on_start()
        # Should not crash, provider just won't be started
        assert "bad" not in module._providers
        await module.on_stop()

    @pytest.mark.asyncio
    async def test_stop_cleans_sessions(self):
        """on_stop clears all sessions."""
        module = ChannelsModule()
        module._config = {"providers": {}}
        await module.on_start()
        module._session_mgr.append_to_history("sess-1", "user", "msg")
        assert module._session_mgr.active_session_count() == 1
        await module.on_stop()
        assert module._session_mgr.active_session_count() == 0


# ── Module Action Tests ──────────────────────────────────────────────


class TestModuleActions:
    @pytest.fixture
    async def module_with_adapter(self):
        """Create a module with a recording adapter injected."""
        module = ChannelsModule()
        module._config = {"providers": {}}
        module._config_obj = ChannelsModuleConfig()
        module._app_id = "test-app"

        adapter = RecordingAdapter()
        await adapter.on_start()

        from digitorn.modules.channels.module import ActiveProvider

        module._providers["test_out"] = ActiveProvider(
            name="test_out",
            config=ProviderConfig(adapter="recording"),
            adapter=adapter,
            semaphore=asyncio.Semaphore(5),
        )
        yield module, adapter
        await module.on_stop()

    @pytest.mark.asyncio
    async def test_send_message(self, module_with_adapter):
        module, adapter = module_with_adapter
        from digitorn.modules.channels.params import SendMessageParams

        result = await module.send_message(SendMessageParams(
            provider="test_out",
            text="Hello from agent",
            subject="Test",
        ))
        assert result.success
        assert len(adapter.deliveries) == 1
        assert adapter.deliveries[0][1].message == "Hello from agent"

    @pytest.mark.asyncio
    async def test_send_message_unknown_provider(self, module_with_adapter):
        module, _ = module_with_adapter
        from digitorn.modules.channels.params import SendMessageParams

        result = await module.send_message(SendMessageParams(
            provider="nonexistent",
            text="Hello",
        ))
        assert not result.success
        assert "Unknown provider" in result.error

    @pytest.mark.asyncio
    async def test_send_message_filters_secrets(self, module_with_adapter):
        module, adapter = module_with_adapter
        from digitorn.modules.channels.params import SendMessageParams

        result = await module.send_message(SendMessageParams(
            provider="test_out",
            text="key: sk-ant-api03-abc123def456ghi789jkl012mno345pqr",
        ))
        assert result.success
        assert "sk-ant-" not in adapter.deliveries[0][1].message
        assert "[REDACTED]" in adapter.deliveries[0][1].message

    @pytest.mark.asyncio
    async def test_broadcast(self, module_with_adapter):
        module, adapter = module_with_adapter

        # Add a second provider
        adapter2 = RecordingAdapter()
        from digitorn.modules.channels.module import ActiveProvider

        module._providers["test_out2"] = ActiveProvider(
            name="test_out2",
            config=ProviderConfig(adapter="recording"),
            adapter=adapter2,
            semaphore=asyncio.Semaphore(5),
        )

        from digitorn.modules.channels.params import BroadcastParams

        result = await module.broadcast(BroadcastParams(
            providers=["test_out", "test_out2"],
            text="Alert!",
        ))
        assert result.success
        assert result.data["sent"] == 2
        assert len(adapter.deliveries) == 1
        assert len(adapter2.deliveries) == 1

    @pytest.mark.asyncio
    async def test_list_providers(self, module_with_adapter):
        module, _ = module_with_adapter
        from digitorn.modules.channels.params import ListProvidersParams

        result = await module.list_providers(ListProvidersParams())
        assert result.success
        providers = result.data["providers"]
        assert len(providers) == 1
        assert providers[0]["name"] == "test_out"

    @pytest.mark.asyncio
    async def test_pause_resume(self, module_with_adapter):
        module, adapter = module_with_adapter
        from digitorn.modules.channels.params import PauseProviderParams, ResumeProviderParams

        result = await module.pause_provider(PauseProviderParams(provider="test_out"))
        assert result.success
        assert module._providers["test_out"].status == "paused"

        result = await module.resume_provider(ResumeProviderParams(provider="test_out"))
        assert result.success
        assert module._providers["test_out"].status == "active"

    @pytest.mark.asyncio
    async def test_stats(self, module_with_adapter):
        module, adapter = module_with_adapter
        from digitorn.modules.channels.params import StatsParams, SendMessageParams

        await module.send_message(SendMessageParams(provider="test_out", text="msg1"))
        await module.send_message(SendMessageParams(provider="test_out", text="msg2"))

        result = await module.stats(StatsParams())
        assert result.success
        assert result.data["total_events_sent"] == 2
        assert result.data["providers_count"] == 1

    @pytest.mark.asyncio
    async def test_provider_history(self, module_with_adapter):
        module, _ = module_with_adapter
        from digitorn.modules.channels.params import ProviderHistoryParams, SendMessageParams

        await module.send_message(SendMessageParams(provider="test_out", text="msg"))

        result = await module.provider_history(ProviderHistoryParams(provider="test_out"))
        assert result.success
        assert len(result.data["events"]) == 1
        assert result.data["events"][0]["direction"] == "outbound"

    @pytest.mark.asyncio
    async def test_send_to_failing_adapter(self):
        """Delivery failure is reported, not raised."""
        module = ChannelsModule()
        module._config_obj = ChannelsModuleConfig()
        module._app_id = "test-app"

        from digitorn.modules.channels.module import ActiveProvider

        module._providers["bad"] = ActiveProvider(
            name="bad",
            config=ProviderConfig(adapter="failing"),
            adapter=FailingAdapter(),
            semaphore=asyncio.Semaphore(5),
        )

        from digitorn.modules.channels.params import SendMessageParams

        result = await module.send_message(SendMessageParams(provider="bad", text="test"))
        assert not result.success
        assert "Simulated failure" in result.error


# ── Pipeline Tests ───────────────────────────────────────────────────


class TestPipeline:
    @pytest.mark.asyncio
    async def test_filter_equals(self):
        """Filter drops events not matching equals condition."""
        pipeline = ActivationPipeline(
            module=MagicMock(),
            session_mgr=ChannelSessionManager(),
            config=ChannelsModuleConfig(),
        )
        cond = FilterCondition(field="event.payload.status", equals="new")

        variables = {"event": {"payload": {"status": "new"}}}
        assert pipeline._check_filter(cond, variables) is True

        variables = {"event": {"payload": {"status": "closed"}}}
        assert pipeline._check_filter(cond, variables) is False

    @pytest.mark.asyncio
    async def test_filter_contains(self):
        pipeline = ActivationPipeline(
            module=MagicMock(),
            session_mgr=ChannelSessionManager(),
            config=ChannelsModuleConfig(),
        )
        cond = FilterCondition(field="event.payload.title", contains="URGENT")

        variables = {"event": {"payload": {"title": "URGENT: Server down"}}}
        assert pipeline._check_filter(cond, variables) is True

        variables = {"event": {"payload": {"title": "Normal ticket"}}}
        assert pipeline._check_filter(cond, variables) is False

    @pytest.mark.asyncio
    async def test_filter_gt_lt(self):
        pipeline = ActivationPipeline(
            module=MagicMock(),
            session_mgr=ChannelSessionManager(),
            config=ChannelsModuleConfig(),
        )
        cond_gt = FilterCondition(field="event.payload.amount", gt=100)
        cond_lt = FilterCondition(field="event.payload.amount", lt=50)

        variables = {"event": {"payload": {"amount": 150}}}
        assert pipeline._check_filter(cond_gt, variables) is True
        assert pipeline._check_filter(cond_lt, variables) is False

        variables = {"event": {"payload": {"amount": 30}}}
        assert pipeline._check_filter(cond_gt, variables) is False
        assert pipeline._check_filter(cond_lt, variables) is True

    @pytest.mark.asyncio
    async def test_filter_not_equals(self):
        pipeline = ActivationPipeline(
            module=MagicMock(),
            session_mgr=ChannelSessionManager(),
            config=ChannelsModuleConfig(),
        )
        cond = FilterCondition(field="event.payload.status", not_equals="spam")

        variables = {"event": {"payload": {"status": "new"}}}
        assert pipeline._check_filter(cond, variables) is True

        variables = {"event": {"payload": {"status": "spam"}}}
        assert pipeline._check_filter(cond, variables) is False

    @pytest.mark.asyncio
    async def test_route_static(self):
        """Route picks agent based on field match."""
        pipeline = ActivationPipeline(
            module=MagicMock(),
            session_mgr=ChannelSessionManager(),
            config=ChannelsModuleConfig(),
        )
        route = RouteConfig(
            field="caller.department",
            rules=[
                RouteRule(match="tech", agent="tech_support"),
                RouteRule(match="billing", agent="billing_agent"),
                RouteRule(default=True, agent="general"),
            ],
        )

        variables = {"caller": {"department": "tech"}}
        assert pipeline._resolve_route(route, variables) == "tech_support"

        variables = {"caller": {"department": "billing"}}
        assert pipeline._resolve_route(route, variables) == "billing_agent"

        variables = {"caller": {"department": "unknown"}}
        assert pipeline._resolve_route(route, variables) == "general"

    @pytest.mark.asyncio
    async def test_build_messages_with_template(self):
        pipeline = ActivationPipeline(
            module=MagicMock(),
            session_mgr=ChannelSessionManager(),
            config=ChannelsModuleConfig(),
        )
        event = _make_event(
            source="+33612345678",
            message="Help me",
            payload={"from": "+33612345678", "body": "Help me"},
        )
        activation = ActivationConfig(
            message="Incoming from {{event.source}}: {{event.payload.body}}",
            context="Channel: WhatsApp\nCaller: {{caller.name}}",
        )
        variables = {
            "event": {
                "event_id": event.event_id,
                "provider": event.provider_id,
                "adapter": event.adapter_type,
                "source": event.source,
                "message": event.message,
                "payload": event.payload,
                "data": event.payload,
                "metadata": event.metadata,
                "timestamp": event.timestamp,
            },
            "caller": {"name": "Jean Dupont"},
        }
        context_extra, user_msg = pipeline._build_messages(event, activation, variables)
        assert "+33612345678" in user_msg
        assert "Help me" in user_msg
        assert "Jean Dupont" in context_extra

    @pytest.mark.asyncio
    async def test_build_messages_auto_from_payload(self):
        """When no message template and no event message, payload is JSON-formatted."""
        pipeline = ActivationPipeline(
            module=MagicMock(),
            session_mgr=ChannelSessionManager(),
            config=ChannelsModuleConfig(),
        )
        # Event with NO message but WITH payload
        event = _make_event(
            message="",  # No text message
            payload={"action": "created", "id": 42},
        )
        activation = ActivationConfig()  # No message template
        variables = {
            "event": {
                "event_id": event.event_id,
                "provider": event.provider_id,
                "adapter": event.adapter_type,
                "source": event.source,
                "message": "",
                "payload": event.payload,
                "data": event.payload,
                "metadata": {},
                "timestamp": event.timestamp,
            },
        }
        _, user_msg = pipeline._build_messages(event, activation, variables)
        assert '"action": "created"' in user_msg
        assert '"id": 42' in user_msg

    @pytest.mark.asyncio
    async def test_build_messages_cron_no_data(self):
        """Cron events with no data get a simple activation message."""
        pipeline = ActivationPipeline(
            module=MagicMock(),
            session_mgr=ChannelSessionManager(),
            config=ChannelsModuleConfig(),
        )
        event = _make_event(message="", payload={})
        activation = ActivationConfig()
        variables = {
            "event": {
                "event_id": event.event_id,
                "provider": "daily_report",
                "adapter": "cron",
                "source": "",
                "message": "",
                "payload": {},
                "data": {},
                "metadata": {},
                "timestamp": event.timestamp,
            },
        }
        _, user_msg = pipeline._build_messages(event, activation, variables)
        assert "activation" in user_msg.lower() or "daily_report" in user_msg


# ── RuntimeApp Wiring Tests ──────────────────────────────────────────


class TestRuntimeAppWiring:
    def test_channels_module_receives_runtime_app(self):
        from digitorn.core.runtime.app import RuntimeApp
        from digitorn.core.app.compiler import CompiledExecution

        ch = ChannelsModule()
        hook = object()

        app = RuntimeApp(
            app_id="test",
            execution=CompiledExecution(),
            modules={"channels": ch},
            hook_runner=hook,
        )

        assert ch._runtime_app is app
        assert ch._hook_runner is hook

    def test_no_channels_module_no_crash(self):
        from digitorn.core.runtime.app import RuntimeApp
        from digitorn.core.app.compiler import CompiledExecution

        # Should not crash when channels module is not present
        app = RuntimeApp(
            app_id="test",
            execution=CompiledExecution(),
            modules={"filesystem": MagicMock()},
        )
        assert app.app_id == "test"


# ── Inbound Event Flow Tests ────────────────────────────────────────


class TestInboundEventFlow:
    @pytest.mark.asyncio
    async def test_inbound_event_recorded_in_history(self):
        """Inbound events appear in the event history."""
        module = ChannelsModule()
        module._config = {"providers": {}}
        module._config_obj = ChannelsModuleConfig()
        module._app_id = "test-app"

        # Manually set up pipeline (skip agent_turn)
        module._pipeline = MagicMock()
        module._pipeline.process_event = AsyncMock()

        adapter = RecordingAdapter()

        from digitorn.modules.channels.module import ActiveProvider

        module._providers["test_in"] = ActiveProvider(
            name="test_in",
            config=ProviderConfig(adapter="recording"),
            adapter=adapter,
            semaphore=asyncio.Semaphore(5),
        )

        event = _make_event(provider="test_in")
        await module._on_inbound_event("test_in", event)

        # Wait for the background task to complete
        await asyncio.sleep(0.1)

        assert module._providers["test_in"].events_received == 1
        assert len(module._event_history) == 1
        assert module._event_history[0].direction == "inbound"

    @pytest.mark.asyncio
    async def test_paused_provider_ignores_events(self):
        """Paused providers don't process inbound events."""
        module = ChannelsModule()
        module._config_obj = ChannelsModuleConfig()
        module._pipeline = MagicMock()
        module._pipeline.process_event = AsyncMock()

        from digitorn.modules.channels.module import ActiveProvider

        module._providers["paused"] = ActiveProvider(
            name="paused",
            config=ProviderConfig(adapter="recording"),
            adapter=RecordingAdapter(),
            semaphore=asyncio.Semaphore(5),
            status="paused",
        )

        event = _make_event(provider="paused")
        await module._on_inbound_event("paused", event)
        await asyncio.sleep(0.1)

        assert module._providers["paused"].events_received == 0
        module._pipeline.process_event.assert_not_called()


# ── Concurrent Activation Tests ──────────────────────────────────────


class TestConcurrentActivations:
    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        """Semaphore prevents more than N concurrent activations."""
        module = ChannelsModule()
        module._config_obj = ChannelsModuleConfig()
        module._app_id = "test-app"

        call_count = 0
        max_concurrent = 0
        current_concurrent = 0

        async def slow_process(event, provider):
            nonlocal call_count, max_concurrent, current_concurrent
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
            await asyncio.sleep(0.05)
            current_concurrent -= 1
            call_count += 1

        module._pipeline = MagicMock()
        module._pipeline.process_event = slow_process

        from digitorn.modules.channels.module import ActiveProvider

        module._providers["test"] = ActiveProvider(
            name="test",
            config=ProviderConfig(adapter="recording", max_concurrent=2),
            adapter=RecordingAdapter(),
            semaphore=asyncio.Semaphore(2),
        )

        # Fire 5 events simultaneously
        events = [_make_event(provider="test") for _ in range(5)]
        for evt in events:
            await module._on_inbound_event("test", evt)

        # Wait for all to complete
        await asyncio.sleep(0.5)

        assert call_count == 5
        assert max_concurrent <= 2  # Semaphore enforced

    @pytest.mark.asyncio
    async def test_shared_session_serialization(self):
        """Shared session events are serialized (not interleaved)."""
        sm = ChannelSessionManager()
        session_id = "shared-test"

        order: list[int] = []

        async def append_with_delay(idx: int):
            lock = sm.get_lock(session_id)
            async with lock:
                order.append(idx)
                await asyncio.sleep(0.02)

        tasks = [asyncio.create_task(append_with_delay(i)) for i in range(5)]
        await asyncio.gather(*tasks)

        # Should be sequential (each waits for the lock)
        assert order == [0, 1, 2, 3, 4]


# ── Security Edge Cases ──────────────────────────────────────────────


class TestSecurityEdgeCases:
    def test_deeply_nested_prototype_pollution(self):
        """Sanitizer strips dunder keys at all nesting levels."""
        data = {
            "level1": {
                "level2": {
                    "__proto__": "polluted",
                    "level3": {
                        "constructor": "polluted",
                        "safe": "value",
                    }
                }
            }
        }
        result = sanitize_payload(data)
        assert "__proto__" not in result["level1"]["level2"]
        assert "constructor" not in result["level1"]["level2"]["level3"]
        assert result["level1"]["level2"]["level3"]["safe"] == "value"

    def test_template_injection_via_user_data(self):
        """User data containing {{...}} is NOT expanded."""
        variables = {
            "event": {
                "payload": {
                    "user_input": "{{secret.DB_PASSWORD}}",
                }
            }
        }
        # The template renders the outer {{}} but inner content is just a string
        result = render("User said: {{event.payload.user_input}}", variables)
        # The result contains the literal string — NOT the secret value
        assert result == "User said: {{secret.DB_PASSWORD}}"

    def test_template_with_malicious_dotpath(self):
        """Malicious dot paths don't crash or leak."""
        variables = {"event": {"data": {"x": 1}}}

        # Try to access non-existent deep paths
        assert render("{{a.b.c.d.e.f.g}}", variables) == "{{a.b.c.d.e.f.g}}"
        # Try numeric tricks
        assert render("{{event.data.0}}", variables) == "{{event.data.0}}"

    def test_filter_secrets_preserves_short_strings(self):
        """Short strings that partially match patterns are not falsely redacted."""
        assert filter_secrets("sk-short") == "sk-short"  # Too short for pattern
        assert filter_secrets("Bearer short") == "Bearer short"
        assert filter_secrets("normal text with sk- prefix") == "normal text with sk- prefix"

    def test_sanitize_handles_none_and_edge_types(self):
        result = sanitize_payload({"a": None, "b": True, "c": 0, "d": 0.0, "e": ""})
        assert result == {"a": None, "b": True, "c": 0, "d": 0.0, "e": ""}

    def test_sanitize_non_string_keys(self):
        """Non-string dict keys are converted to strings."""
        result = sanitize_payload({123: "val", True: "bool"})
        assert "123" in result
        assert "True" in result


# ── State Snapshot Tests ─────────────────────────────────────────────


class TestStateSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_and_restore(self):
        module = ChannelsModule()
        module._app_id = "my-app"
        module._config = {"providers": {}}
        module._config_obj = ChannelsModuleConfig()

        snapshot = module.state_snapshot()
        assert snapshot["app_id"] == "my-app"

        module2 = ChannelsModule()
        await module2.restore_state(snapshot)
        assert module2._app_id == "my-app"


# ── Simulate Event Tests ─────────────────────────────────────────────


class TestSimulateEvent:
    @pytest.mark.asyncio
    async def test_simulate_creates_event(self):
        module = ChannelsModule()
        module._config_obj = ChannelsModuleConfig()
        module._app_id = "test-app"
        module._pipeline = MagicMock()
        module._pipeline.process_event = AsyncMock()

        from digitorn.modules.channels.module import ActiveProvider

        module._providers["test"] = ActiveProvider(
            name="test",
            config=ProviderConfig(adapter="recording"),
            adapter=RecordingAdapter(),
            semaphore=asyncio.Semaphore(5),
        )

        from digitorn.modules.channels.params import SimulateEventParams

        result = await module.simulate_event(SimulateEventParams(
            provider="test",
            payload={"action": "test"},
            source="simulator",
            message="Test event",
        ))

        assert result.success
        assert "event_id" in result.data

        # Wait for background task
        await asyncio.sleep(0.1)
        assert module._providers["test"].events_received == 1
