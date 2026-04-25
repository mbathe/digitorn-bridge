"""Tests for the Universal Output Channel System.

Covers:
- BaseOutputChannel contract and helpers
- ChannelPayload construction and from_dict conversion
- DeliveryResult structure
- ChannelRegistry (type/instance management, delivery, retry, fallback)
- Built-in channels: LLMNotificationChannel, WebhookChannel, LogChannel
- Schema: ChannelInstanceConfig
- Compiler: channel block compilation
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from digitorn.core.app.channels.base import (
    BaseOutputChannel,
    ChannelCapabilities,
    ChannelHealth,
    ChannelMeta,
    ChannelPayload,
    DeliveryResult,
    PayloadAttachment,
    RetryPolicy,
)
from digitorn.core.app.channels.llm import LLMNotificationChannel
from digitorn.core.app.channels.log import LogChannel
from digitorn.core.app.channels.registry import ChannelRegistry
from digitorn.core.app.channels.webhook import WebhookChannel


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class DummyChannel(BaseOutputChannel):
    """Minimal test channel."""

    CHANNEL_ID = "dummy"
    CHANNEL_NAME = "Dummy Test"
    CHANNEL_VERSION = "1.0.0"
    CHANNEL_DESCRIPTION = "For testing"

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.deliveries: list[tuple[str, ChannelPayload, dict]] = []
        self.fail_count = 0
        self.max_fails = 0

    async def deliver(
        self, app_id: str, payload: ChannelPayload, config: dict[str, Any]
    ) -> DeliveryResult:
        self.deliveries.append((app_id, payload, config))
        if self.fail_count < self.max_fails:
            self.fail_count += 1
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="transient failure",
                retryable=True,
            )
        return DeliveryResult(
            success=True,
            channel_id=self.CHANNEL_ID,
            delivery_id="dummy-123",
        )


class FailingChannel(BaseOutputChannel):
    """Channel that always raises exceptions."""

    CHANNEL_ID = "failing"
    CHANNEL_NAME = "Failing"
    CHANNEL_VERSION = "1.0.0"

    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(max_retries=1, backoff_base=0.01)

    async def deliver(
        self, app_id: str, payload: ChannelPayload, config: dict[str, Any]
    ) -> DeliveryResult:
        raise ConnectionError("Network down")


# ---------------------------------------------------------------------------
# ChannelPayload tests
# ---------------------------------------------------------------------------


class TestChannelPayload:
    """Test ChannelPayload construction and conversion."""

    def test_basic_construction(self):
        p = ChannelPayload(message="Hello world")
        assert p.message == "Hello world"
        assert p.priority == "normal"
        assert p.tags == []
        assert p.attachments == []

    def test_full_construction(self):
        p = ChannelPayload(
            message="Server down",
            title="Alert",
            rich_message="<b>Server down</b>",
            structured_data={"status": 500},
            priority="critical",
            tags=["production", "urgent"],
            thread_id="thread-123",
            metadata={"job_id": "abc"},
        )
        assert p.title == "Alert"
        assert p.priority == "critical"
        assert p.thread_id == "thread-123"
        assert p.structured_data["status"] == 500

    def test_from_dict_with_message(self):
        p = ChannelPayload.from_dict({
            "message": "Direct message",
            "title": "Test",
            "priority": "high",
        })
        assert p.message == "Direct message"
        assert p.title == "Test"
        assert p.priority == "high"

    def test_from_dict_scheduler_payload(self):
        """Legacy scheduler payload format."""
        p = ChannelPayload.from_dict({
            "type": "scheduled_job",
            "job_id": "abc123",
            "label": "Health Check",
            "action_type": "tool_call",
            "result": {"status": "ok"},
            "run_count": 5,
        })
        assert "Health Check" in p.message
        assert p.title == "Health Check"
        assert p.metadata["job_id"] == "abc123"
        assert p.metadata["run_count"] == 5

    def test_from_dict_error_payload(self):
        p = ChannelPayload.from_dict({
            "type": "scheduled_job",
            "label": "Failed Job",
            "error": "Connection refused",
        })
        assert "Error" in p.message
        assert "Connection refused" in p.message

    def test_from_dict_prompt_payload(self):
        p = ChannelPayload.from_dict({
            "type": "scheduled_job",
            "label": "Reminder",
            "prompt": "Check the deployment",
        })
        assert "Check the deployment" in p.message

    def test_from_dict_empty(self):
        p = ChannelPayload.from_dict({
            "type": "notification",
        })
        assert "fired" in p.message.lower() or "notification" in p.message.lower()

    def test_attachment(self):
        att = PayloadAttachment(
            filename="report.pdf",
            content_type="application/pdf",
            size_bytes=1024,
        )
        p = ChannelPayload(message="Report ready", attachments=[att])
        assert len(p.attachments) == 1
        assert p.attachments[0].filename == "report.pdf"


# ---------------------------------------------------------------------------
# DeliveryResult tests
# ---------------------------------------------------------------------------


class TestDeliveryResult:
    def test_success(self):
        r = DeliveryResult(success=True, channel_id="slack", delivery_id="msg-1")
        assert r.success
        assert r.delivery_id == "msg-1"
        assert r.error is None
        assert not r.buffered

    def test_failure(self):
        r = DeliveryResult(
            success=False,
            channel_id="webhook",
            error="HTTP 500",
            retryable=True,
        )
        assert not r.success
        assert r.retryable
        assert r.error == "HTTP 500"

    def test_buffered(self):
        r = DeliveryResult(success=False, channel_id="llm", buffered=True)
        assert not r.success
        assert r.buffered

    def test_to_dict(self):
        r = DeliveryResult(success=True, channel_id="test")
        d = r.to_dict()
        assert d["success"] is True
        assert d["channel_id"] == "test"
        assert "delivered_at" in d


# ---------------------------------------------------------------------------
# BaseOutputChannel tests
# ---------------------------------------------------------------------------


class TestBaseOutputChannel:
    def test_channel_meta(self):
        ch = DummyChannel()
        meta = ch.channel_meta()
        assert isinstance(meta, ChannelMeta)
        assert meta.channel_id == "dummy"
        assert meta.name == "Dummy Test"
        assert meta.version == "1.0.0"

    def test_capabilities_default(self):
        ch = DummyChannel()
        caps = ch.capabilities()
        assert isinstance(caps, ChannelCapabilities)
        assert "text" in caps.supported_formats
        assert not caps.supports_rich_text

    def test_config_schema_default(self):
        ch = DummyChannel()
        schema = ch.config_schema()
        assert "required" in schema
        assert "optional" in schema

    def test_retry_policy_default(self):
        ch = DummyChannel()
        policy = ch.retry_policy()
        assert isinstance(policy, RetryPolicy)
        assert policy.max_retries == 3

    def test_format_text(self):
        ch = DummyChannel()
        p = ChannelPayload(message="Hello", title="Alert", tags=["prod"])
        text = ch.format_text(p)
        assert "[Alert]" in text
        assert "Hello" in text
        assert "prod" in text

    def test_format_rich_with_rich_message(self):
        ch = DummyChannel()
        p = ChannelPayload(message="plain", rich_message="<b>rich</b>")
        assert ch.format_rich(p) == "<b>rich</b>"

    def test_format_rich_fallback(self):
        ch = DummyChannel()
        p = ChannelPayload(message="plain", title="Title")
        rich = ch.format_rich(p)
        assert "Title" in rich
        assert "plain" in rich

    @pytest.mark.asyncio
    async def test_validate_config_required_missing(self):
        class StrictChannel(BaseOutputChannel):
            CHANNEL_ID = "strict"
            CHANNEL_NAME = "Strict"
            CHANNEL_VERSION = "1.0.0"

            def config_schema(self):
                return {"required": {"api_key": "API key"}, "optional": {}}

            async def deliver(self, app_id, payload, config):
                return DeliveryResult(success=True, channel_id=self.CHANNEL_ID)

        ch = StrictChannel()
        errors = await ch.validate_config()
        assert len(errors) == 1
        assert "api_key" in errors[0]

    @pytest.mark.asyncio
    async def test_validate_config_ok(self):
        class StrictChannel(BaseOutputChannel):
            CHANNEL_ID = "strict"
            CHANNEL_NAME = "Strict"
            CHANNEL_VERSION = "1.0.0"

            def config_schema(self):
                return {"required": {"api_key": "API key"}, "optional": {}}

            async def deliver(self, app_id, payload, config):
                return DeliveryResult(success=True, channel_id=self.CHANNEL_ID)

        ch = StrictChannel(channel_config={"api_key": "abc"})
        errors = await ch.validate_config()
        assert errors == []

    def test_health_tracking(self):
        ch = DummyChannel()
        ch._record_success(latency_ms=42.0)
        assert ch._health.deliveries_total == 1
        assert ch._health.latency_ms == 42.0
        assert ch._health.status == "ok"

        ch._record_failure("timeout")
        assert ch._health.deliveries_total == 2
        assert ch._health.deliveries_failed == 1
        assert ch._health.last_error == "timeout"

    def test_repr(self):
        ch = DummyChannel()
        assert "dummy" in repr(ch)
        assert "1.0.0" in repr(ch)


# ---------------------------------------------------------------------------
# ChannelRegistry tests
# ---------------------------------------------------------------------------


class TestChannelRegistry:
    def test_register_type(self):
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        assert "dummy" in reg.type_ids()
        assert reg.get_type("dummy") is DummyChannel

    def test_register_type_empty_id(self):
        class BadChannel(BaseOutputChannel):
            CHANNEL_ID = ""
            CHANNEL_NAME = "Bad"
            CHANNEL_VERSION = "1.0.0"

            async def deliver(self, app_id, payload, config):
                pass

        reg = ChannelRegistry()
        with pytest.raises(ValueError, match="empty"):
            reg.register_type(BadChannel)

    def test_list_types(self):
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        reg.register_type(LogChannel)
        metas = reg.list_types()
        assert len(metas) == 2
        ids = {m.channel_id for m in metas}
        assert "dummy" in ids
        assert "log" in ids

    def test_create_instance(self):
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        inst = reg.create_instance("my_dummy", "dummy", {"key": "val"}, app_id="app1")
        assert isinstance(inst, DummyChannel)
        assert inst.channel_config == {"key": "val"}
        assert reg.get_instance("my_dummy") is inst

    def test_create_instance_unknown_type(self):
        reg = ChannelRegistry()
        with pytest.raises(ValueError, match="Unknown"):
            reg.create_instance("x", "nonexistent", {})

    def test_register_instance(self):
        reg = ChannelRegistry()
        ch = DummyChannel()
        reg.register_instance("custom", ch, app_id="app1")
        assert reg.get_instance("custom") is ch

    def test_list_instances(self):
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        reg.create_instance("a", "dummy", {}, app_id="app1")
        reg.create_instance("b", "dummy", {}, app_id="app2")
        instances = reg.list_instances()
        assert instances == {"a": "dummy", "b": "dummy"}

    @pytest.mark.asyncio
    async def test_stop_and_remove_for_app(self):
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        reg.create_instance("a", "dummy", {}, app_id="app1")
        reg.create_instance("b", "dummy", {}, app_id="app1")
        reg.create_instance("c", "dummy", {}, app_id="app2")

        removed = await reg.stop_and_remove_for_app("app1")
        assert removed == 2
        assert reg.get_instance("a") is None
        assert reg.get_instance("b") is None
        assert reg.get_instance("c") is not None

    @pytest.mark.asyncio
    async def test_deliver_by_instance_name(self):
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        reg.create_instance("my_ch", "dummy", {})
        payload = ChannelPayload(message="test")
        result = await reg.deliver("my_ch", "app1", payload)
        assert result.success
        inst = reg.get_instance("my_ch")
        assert len(inst.deliveries) == 1

    @pytest.mark.asyncio
    async def test_deliver_dict_payload(self):
        """deliver() should auto-convert dict to ChannelPayload."""
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        reg.create_instance("my_ch", "dummy", {})
        result = await reg.deliver("my_ch", "app1", {"message": "from dict"})
        assert result.success
        inst = reg.get_instance("my_ch")
        assert inst.deliveries[0][1].message == "from dict"

    @pytest.mark.asyncio
    async def test_deliver_fallback_to_default(self):
        """Unknown channel falls back to default (llm_notification)."""
        reg = ChannelRegistry()
        ch = DummyChannel()
        ch.CHANNEL_ID = "llm_notification"
        reg.register_instance("llm_notification", ch)
        result = await reg.deliver("nonexistent", "app1", ChannelPayload(message="test"))
        assert result.success  # Fell back to default

    @pytest.mark.asyncio
    async def test_deliver_no_fallback(self):
        reg = ChannelRegistry()
        result = await reg.deliver("nonexistent", "app1", ChannelPayload(message="test"))
        assert not result.success
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_deliver_with_retry_success_after_failure(self):
        """Retry on transient failure, then succeed."""
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        inst = reg.create_instance("retry_ch", "dummy", {})
        inst.max_fails = 2  # Fail twice, then succeed

        # Override retry policy for fast test
        inst.retry_policy = lambda: RetryPolicy(max_retries=3, backoff_base=0.01)

        result = await reg.deliver("retry_ch", "app1", ChannelPayload(message="test"))
        assert result.success
        assert len(inst.deliveries) == 3  # 2 failures + 1 success

    @pytest.mark.asyncio
    async def test_deliver_exception_retried(self):
        """Channel that raises exceptions should be retried."""
        reg = ChannelRegistry()
        reg.register_type(FailingChannel)
        reg.create_instance("fail_ch", "failing", {})
        result = await reg.deliver("fail_ch", "app1", ChannelPayload(message="test"))
        assert not result.success
        assert "Network down" in result.error

    @pytest.mark.asyncio
    async def test_deliver_multi(self):
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        reg.create_instance("ch1", "dummy", {})
        reg.create_instance("ch2", "dummy", {})
        results = await reg.deliver_multi(
            ["ch1", "ch2"], "app1", ChannelPayload(message="fanout")
        )
        assert len(results) == 2
        assert all(r.success for r in results)

    @pytest.mark.asyncio
    async def test_health(self):
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        reg.create_instance("h_ch", "dummy", {})
        health = await reg.health("h_ch")
        assert isinstance(health, ChannelHealth)
        assert health.status == "ok"

    @pytest.mark.asyncio
    async def test_health_all(self):
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        reg.create_instance("a", "dummy", {})
        reg.create_instance("b", "dummy", {})
        all_health = await reg.health_all()
        assert len(all_health) == 2
        assert all(h.status == "ok" for h in all_health.values())

    @pytest.mark.asyncio
    async def test_validate_instance(self):
        reg = ChannelRegistry()
        reg.register_type(DummyChannel)
        reg.create_instance("v_ch", "dummy", {})
        errors = await reg.validate_instance("v_ch")
        assert errors == []

    def test_legacy_register(self):
        """Backward compat: register() should work."""
        reg = ChannelRegistry()
        ch = DummyChannel()
        reg.register(ch)
        assert reg.get("dummy") is ch
        assert "dummy" in reg.list_channels()
        assert reg.get_type("dummy") is DummyChannel


# ---------------------------------------------------------------------------
# LLMNotificationChannel tests
# ---------------------------------------------------------------------------


class TestLLMNotificationChannel:
    def test_meta(self):
        ch = LLMNotificationChannel()
        assert ch.CHANNEL_ID == "llm_notification"
        meta = ch.channel_meta()
        assert meta.channel_id == "llm_notification"

    def test_retry_policy_no_retry(self):
        ch = LLMNotificationChannel()
        assert ch.retry_policy().max_retries == 0

    @pytest.mark.asyncio
    async def test_deliver_direct(self):
        """Direct push to context_builder via push_module_notification."""
        ch = LLMNotificationChannel()
        cb = MagicMock()

        ch.register_context_builder("app1", cb)
        payload = ChannelPayload(
            message="Test",
            structured_data={"type": "test", "result": "ok"},
        )
        result = await ch.deliver("app1", payload, {})
        assert result.success
        cb.push_module_notification.assert_called_once()

    @pytest.mark.asyncio
    async def test_deliver_buffered(self):
        """Buffer when no consumer connected."""
        job_store = MagicMock()
        ch = LLMNotificationChannel(job_store=job_store)
        payload = ChannelPayload(message="Buffered", structured_data={"type": "test"})
        result = await ch.deliver("app1", payload, {})
        assert not result.success
        assert result.buffered
        job_store.buffer_notification.assert_called_once()

    @pytest.mark.asyncio
    async def test_deliver_no_store(self):
        """No consumer and no job_store."""
        ch = LLMNotificationChannel()
        payload = ChannelPayload(message="Lost")
        result = await ch.deliver("app1", payload, {})
        assert not result.success
        assert not result.buffered
        assert "No active consumer" in result.error

    def test_register_unregister_context_builder(self):
        ch = LLMNotificationChannel()
        cb = MagicMock()
        ch.register_context_builder("app1", cb)
        assert ch._context_builders["app1"] is cb
        ch.unregister_context_builder("app1")
        assert "app1" not in ch._context_builders


# ---------------------------------------------------------------------------
# LogChannel tests
# ---------------------------------------------------------------------------


class TestLogChannel:
    def test_meta(self):
        ch = LogChannel()
        assert ch.CHANNEL_ID == "log"
        assert "log" in ch.CHANNEL_NAME.lower() or "Log" in ch.CHANNEL_NAME

    @pytest.mark.asyncio
    async def test_deliver_text(self):
        ch = LogChannel(channel_config={"level": "DEBUG"})
        payload = ChannelPayload(message="Test log", title="Test")
        result = await ch.deliver("app1", payload, {})
        assert result.success
        assert result.channel_id == "log"

    @pytest.mark.asyncio
    async def test_deliver_json(self):
        ch = LogChannel(channel_config={"format": "json", "include_data": True})
        payload = ChannelPayload(
            message="Test", structured_data={"key": "value"}
        )
        result = await ch.deliver("app1", payload, {})
        assert result.success


# ---------------------------------------------------------------------------
# WebhookChannel tests
# ---------------------------------------------------------------------------


class TestWebhookChannel:
    def test_meta(self):
        ch = WebhookChannel()
        assert ch.CHANNEL_ID == "webhook"
        caps = ch.capabilities()
        assert caps.supports_rich_text
        assert "json" in caps.supported_formats

    @pytest.mark.asyncio
    async def test_validate_config_missing_url(self):
        ch = WebhookChannel()
        errors = await ch.validate_config()
        assert any("url" in e.lower() for e in errors)

    @pytest.mark.asyncio
    async def test_validate_config_bad_url(self):
        ch = WebhookChannel(channel_config={"url": "not-a-url"})
        errors = await ch.validate_config()
        assert any("Invalid URL" in e for e in errors)

    @pytest.mark.asyncio
    async def test_validate_config_ok(self):
        ch = WebhookChannel(channel_config={"url": "https://example.com/hook"})
        errors = await ch.validate_config()
        assert errors == []

    @pytest.mark.asyncio
    async def test_deliver_no_url(self):
        ch = WebhookChannel()
        payload = ChannelPayload(message="test")
        result = await ch.deliver("app1", payload, {})
        assert not result.success
        assert "No webhook URL" in result.error

    def test_build_body_default(self):
        ch = WebhookChannel()
        payload = ChannelPayload(
            message="Hello",
            title="Alert",
            priority="high",
            tags=["prod"],
            structured_data={"key": "val"},
            metadata={"timestamp": "2024-01-01T00:00:00Z"},
        )
        body = ch._build_body(payload, {})
        assert isinstance(body, dict)
        assert body["message"] == "Hello"
        assert body["title"] == "Alert"
        assert body["priority"] == "high"
        assert body["data"] == {"key": "val"}

    def test_build_body_custom_payload(self):
        ch = WebhookChannel()
        payload = ChannelPayload(message="test")
        body = ch._build_body(payload, {"payload": {"custom": True}})
        assert body == {"custom": True}

    def test_build_body_template(self):
        ch = WebhookChannel(channel_config={
            "payload_template": '{"text": "{{message}}", "channel": "#alerts"}'
        })
        payload = ChannelPayload(message="Server down")
        body = ch._build_body(payload, {})
        assert isinstance(body, str)
        assert "Server down" in body
        assert "#alerts" in body

    def test_retry_policy(self):
        ch = WebhookChannel()
        policy = ch.retry_policy()
        assert policy.max_retries == 3


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestChannelSchema:
    def test_channel_instance_config(self):
        from digitorn.core.app.schema import ChannelInstanceConfig

        cfg = ChannelInstanceConfig(
            type="webhook",
            config={"url": "https://example.com"},
        )
        assert cfg.type == "webhook"
        assert cfg.config["url"] == "https://example.com"

    def test_app_definition_with_channels(self):
        from digitorn.core.app.schema import AppDefinition

        raw = {
            "app": {"app_id": "test", "name": "Test"},
            "channels": {
                "my_hook": {
                    "type": "webhook",
                    "config": {"url": "https://example.com"},
                },
                "my_log": {
                    "type": "log",
                    "config": {"level": "INFO"},
                },
            },
        }
        defn = AppDefinition.model_validate(raw)
        assert "my_hook" in defn.channels
        assert defn.channels["my_hook"].type == "webhook"
        assert "my_log" in defn.channels

    def test_execution_default_channel(self):
        from digitorn.core.app.schema import ExecutionConfig

        cfg = ExecutionConfig()
        assert cfg.default_channel == "llm_notification"

        cfg2 = ExecutionConfig(default_channel="slack_alerts")
        assert cfg2.default_channel == "slack_alerts"


# ---------------------------------------------------------------------------
# Compiler tests
# ---------------------------------------------------------------------------


class TestChannelCompiler:
    def test_compiled_channel_instance(self):
        from digitorn.core.app.compiler import CompiledChannelInstance

        c = CompiledChannelInstance(
            instance_name="my_hook",
            channel_type="webhook",
            config={"url": "https://resolved.com"},
        )
        assert c.instance_name == "my_hook"
        assert c.channel_type == "webhook"

    def test_compiled_app_has_channels(self):
        from digitorn.core.app.compiler import CompiledApp, CompiledChannelInstance
        from digitorn.core.app.schema import AppMeta

        app = CompiledApp(
            meta=AppMeta(app_id="test", name="Test"),
            channels={
                "hook": CompiledChannelInstance(
                    instance_name="hook",
                    channel_type="webhook",
                    config={"url": "https://x.com"},
                ),
            },
        )
        assert "hook" in app.channels
        assert app.channels["hook"].channel_type == "webhook"


# ---------------------------------------------------------------------------
# Backward compatibility tests
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_output_channels_import(self):
        """Old import path should still work."""
        from digitorn.core.app.output_channels import (
            ChannelRegistry,
            LLMNotificationChannel,
        )
        assert ChannelRegistry is not None
        assert LLMNotificationChannel is not None
