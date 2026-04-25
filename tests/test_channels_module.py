"""Unit tests for the ChannelsModule core.

Tests config validation, adapter instantiation, lifecycle, actions,
and session management.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
    ProviderConfig,
)
from digitorn.modules.channels.session_manager import ChannelSessionManager


# ── Dummy adapter for testing ────────────────────────────────────────


class DummyAdapter(BaseChannelAdapter):
    """Test adapter that records all operations."""

    CHANNEL_ID = "dummy"
    CHANNEL_NAME = "Dummy"
    CHANNEL_VERSION = "1.0.0"
    SUPPORTS_INBOUND = True
    SUPPORTS_OUTBOUND = True

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        super().__init__(channel_config=channel_config)
        self.deliveries: list[tuple[str, ChannelPayload, dict]] = []
        self.replies: list[tuple[dict, str]] = []
        self.listener_started = False
        self.listener_stopped = False
        self._callback: InboundCallback | None = None

    async def start_listener(self, callback: InboundCallback) -> None:
        self.listener_started = True
        self._callback = callback
        # Don't block — just register callback

    async def stop_listener(self) -> None:
        self.listener_stopped = True
        self._callback = None

    async def deliver(
        self, app_id: str, payload: ChannelPayload, config: dict[str, Any]
    ) -> DeliveryResult:
        self.deliveries.append((app_id, payload, config))
        return DeliveryResult(success=True, channel_id="dummy", delivery_id="del-001")

    async def send_reply(
        self, reply_context: dict[str, Any], text: str, payload=None
    ) -> DeliveryResult:
        self.replies.append((reply_context, text))
        return DeliveryResult(success=True, channel_id="dummy", delivery_id="reply-001")

    def adapter_capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_inbound=True,
            supports_outbound=True,
            supports_rich_text=True,
        )

    async def inject_event(self, event: InboundEvent) -> None:
        """Simulate an inbound event for testing."""
        if self._callback:
            await self._callback(event)


# ── Config validation tests ──────────────────────────────────────────


class TestConfigValidation:
    def test_minimal_config(self):
        cfg = ChannelsModuleConfig()
        assert cfg.providers == {}
        assert cfg.max_turns == 30
        assert cfg.timeout == 120.0

    def test_provider_config(self):
        cfg = ProviderConfig(adapter="cron", config={"schedule": "0 * * * *"})
        assert cfg.adapter == "cron"
        assert cfg.enabled is True
        assert cfg.max_concurrent == 5

    def test_full_config(self):
        cfg = ChannelsModuleConfig(
            providers={
                "daily": ProviderConfig(adapter="cron", config={"schedule": "0 9 * * *"}),
                "alerts": ProviderConfig(
                    adapter="webhook",
                    config={"url": "https://hooks.slack.com/xxx"},
                    activation=ActivationConfig(reply="auto"),
                ),
            },
            default_agent="main",
            max_turns=50,
        )
        assert len(cfg.providers) == 2
        assert cfg.providers["alerts"].activation.reply == "auto"

    def test_activation_config_defaults(self):
        ac = ActivationConfig()
        assert ac.session == "per_event"
        assert ac.reply == "none"
        assert ac.filter == []
        assert ac.prepare == []

    def test_max_concurrent_bounds(self):
        with pytest.raises(Exception):  # Pydantic validation
            ProviderConfig(adapter="cron", max_concurrent=0)
        with pytest.raises(Exception):
            ProviderConfig(adapter="cron", max_concurrent=101)


# ── Session manager tests ────────────────────────────────────────────


class TestSessionManager:
    def test_per_event_unique(self):
        sm = ChannelSessionManager()
        evt = InboundEvent(
            event_id="e1", provider_id="wa", adapter_type="webhook", source="+33612345678",
        )
        s1 = sm.resolve_session_id(evt, "per_event", {})
        s2 = sm.resolve_session_id(evt, "per_event", {})
        assert s1 != s2
        assert s1.startswith("ch-evt-")

    def test_shared_deterministic(self):
        sm = ChannelSessionManager()
        evt = InboundEvent(
            event_id="e1", provider_id="wa", adapter_type="webhook", source="+33612345678",
        )
        s1 = sm.resolve_session_id(evt, "shared", {})
        s2 = sm.resolve_session_id(evt, "shared", {})
        assert s1 == s2
        assert s1 == "ch-wa-+33612345678"

    def test_template_mode(self):
        sm = ChannelSessionManager()
        evt = InboundEvent(
            event_id="e1", provider_id="wa", adapter_type="webhook", source="+33612345678",
        )
        variables = {"event": {"source": "+33612345678"}}
        sid = sm.resolve_session_id(evt, "wa-{{event.source}}", variables)
        assert "33612345678" in sid

    def test_history_append_and_cap(self):
        sm = ChannelSessionManager()
        for i in range(110):
            sm.append_to_history("sess-1", "user", f"msg-{i}")
        history = sm.get_history("sess-1")
        # Cap is enforced: first message + last 80 messages
        assert len(history) <= 100

    def test_clear_session(self):
        sm = ChannelSessionManager()
        sm.append_to_history("sess-1", "user", "hello")
        assert sm.active_session_count() == 1
        sm.clear_session("sess-1")
        assert sm.active_session_count() == 0

    @pytest.mark.asyncio
    async def test_cleanup(self):
        sm = ChannelSessionManager()
        for i in range(5):
            sm.append_to_history(f"sess-{i}", "user", "msg")
        assert sm.active_session_count() == 5
        await sm.cleanup()
        assert sm.active_session_count() == 0


# ── InboundEvent tests ───────────────────────────────────────────────


class TestInboundEvent:
    def test_frozen(self):
        evt = InboundEvent(
            event_id="e1", provider_id="test", adapter_type="webhook", source="127.0.0.1",
        )
        with pytest.raises(AttributeError):
            evt.event_id = "changed"  # type: ignore

    def test_make_event_id_unique(self):
        ids = {make_event_id() for _ in range(100)}
        assert len(ids) == 100

    def test_defaults(self):
        evt = InboundEvent(
            event_id="e1", provider_id="p1", adapter_type="cron", source="schedule",
        )
        assert evt.message == ""
        assert evt.payload == {}
        assert evt.metadata == {}
        assert evt.reply_context == {}
        assert evt.timestamp > 0


# ── Adapter base class tests ─────────────────────────────────────────


class TestBaseChannelAdapter:
    def test_dummy_adapter_deliver(self):
        adapter = DummyAdapter()
        result = asyncio.get_event_loop().run_until_complete(
            adapter.deliver("app1", ChannelPayload(message="test"), {})
        )
        assert result.success
        assert len(adapter.deliveries) == 1

    def test_dummy_adapter_reply(self):
        adapter = DummyAdapter()
        result = asyncio.get_event_loop().run_until_complete(
            adapter.send_reply({"_app_id": "app1"}, "response text")
        )
        assert result.success
        assert adapter.replies[0][1] == "response text"

    def test_max_inbound_payload_bytes_default(self):
        adapter = DummyAdapter()
        assert adapter.max_inbound_payload_bytes() == 1_048_576

    def test_validate_inbound_default(self):
        adapter = DummyAdapter()
        ok, err = asyncio.get_event_loop().run_until_complete(
            adapter.validate_inbound(b"test", {})
        )
        assert ok is True
