"""Tests for individual channel adapters.

Tests adapter-specific behavior: cron scheduling, file watching,
webhook validation, email parsing, RSS dedup.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from digitorn.core.app.channels.base import ChannelPayload, DeliveryResult
from digitorn.modules.channels.adapter import InboundEvent


# ── Cron Adapter ─────────────────────────────────────────────────────


class TestCronAdapter:
    def test_inbound_only(self):
        from digitorn.modules.channels.adapters.cron import CronAdapter

        adapter = CronAdapter(channel_config={"schedule": "*/5 * * * *"})
        caps = adapter.adapter_capabilities()
        assert caps.supports_inbound is True
        assert caps.supports_outbound is False

    def test_deliver_returns_error(self):
        from digitorn.modules.channels.adapters.cron import CronAdapter

        adapter = CronAdapter(channel_config={"schedule": "0 * * * *"})
        result = asyncio.get_event_loop().run_until_complete(
            adapter.deliver("app", ChannelPayload(message="test"), {})
        )
        assert result.success is False
        assert "inbound-only" in result.error

    def test_seconds_until_next(self):
        from digitorn.modules.channels.adapters.cron import _seconds_until_next

        # Should return a positive integer for valid cron
        result = _seconds_until_next("* * * * *")  # Every minute
        assert result is not None
        assert 1 <= result <= 61

    def test_invalid_schedule(self):
        from digitorn.modules.channels.adapters.cron import _seconds_until_next

        result = _seconds_until_next("invalid")
        assert result is None


# ── File Watcher Adapter ─────────────────────────────────────────────


class TestFileWatcherAdapter:
    def test_inbound_only(self):
        from digitorn.modules.channels.adapters.file_watcher import FileWatcherAdapter

        adapter = FileWatcherAdapter(channel_config={"paths": ["./inbox/*.csv"]})
        caps = adapter.adapter_capabilities()
        assert caps.supports_inbound is True
        assert caps.supports_outbound is False

    def test_config_parsing(self):
        from digitorn.modules.channels.adapters.file_watcher import FileWatcherAdapter

        adapter = FileWatcherAdapter(channel_config={
            "paths": ["/tmp/*.txt", "/data/*.csv"],
            "poll_interval": 10.0,
            "message": "New file: {{event.data.path}}",
        })
        assert adapter._paths == ["/tmp/*.txt", "/data/*.csv"]
        assert adapter._poll_interval == 10.0


# ── Webhook Adapter ──────────────────────────────────────────────────


class TestWebhookAdapter:
    def test_bidirectional(self):
        from digitorn.modules.channels.adapters.webhook import WebhookAdapter

        adapter = WebhookAdapter(channel_config={
            "inbound_path": "/hook/test",
            "url": "https://example.com/webhook",
        })
        caps = adapter.adapter_capabilities()
        assert caps.supports_inbound is True
        assert caps.supports_outbound is True

    def test_inbound_only(self):
        from digitorn.modules.channels.adapters.webhook import WebhookAdapter

        adapter = WebhookAdapter(channel_config={"inbound_path": "/hook/test"})
        caps = adapter.adapter_capabilities()
        assert caps.supports_inbound is True
        assert caps.supports_outbound is False

    def test_outbound_only(self):
        from digitorn.modules.channels.adapters.webhook import WebhookAdapter

        adapter = WebhookAdapter(channel_config={"url": "https://example.com"})
        caps = adapter.adapter_capabilities()
        assert caps.supports_inbound is False
        assert caps.supports_outbound is True

    @pytest.mark.asyncio
    async def test_validate_inbound_size(self):
        from digitorn.modules.channels.adapters.webhook import WebhookAdapter

        adapter = WebhookAdapter(channel_config={
            "inbound_path": "/hook/test",
            "max_payload_bytes": 100,
        })
        ok, err = await adapter.validate_inbound(b"x" * 200, {"content-type": "application/json"})
        assert ok is False
        assert "too large" in err.lower()

    @pytest.mark.asyncio
    async def test_validate_inbound_content_type(self):
        from digitorn.modules.channels.adapters.webhook import WebhookAdapter

        adapter = WebhookAdapter(channel_config={"inbound_path": "/hook/test"})
        ok, err = await adapter.validate_inbound(b"{}", {"content-type": "text/xml"})
        assert ok is False
        assert "Unsupported" in err

    @pytest.mark.asyncio
    async def test_validate_inbound_hmac(self):
        from digitorn.modules.channels.adapters.webhook import WebhookAdapter

        secret = "test_webhook_secret"
        body = b'{"event": "push"}'
        sig = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()

        adapter = WebhookAdapter(channel_config={
            "inbound_path": "/hook/test",
            "auth": "signature",
            "signature_secret": secret,
            "signature_header": "X-Signature-256",
        })

        # Valid signature
        ok, err = await adapter.validate_inbound(body, {
            "content-type": "application/json",
            "x-signature-256": sig,
        })
        assert ok is True

        # Invalid signature
        ok, err = await adapter.validate_inbound(body, {
            "content-type": "application/json",
            "x-signature-256": "invalid",
        })
        assert ok is False
        assert "signature" in err.lower()

    @pytest.mark.asyncio
    async def test_validate_inbound_api_key(self):
        from digitorn.modules.channels.adapters.webhook import WebhookAdapter

        adapter = WebhookAdapter(channel_config={
            "inbound_path": "/hook/test",
            "auth": "api_key",
            "api_key": "my_secret_key_12345",
        })

        ok, _ = await adapter.validate_inbound(b"{}", {
            "content-type": "application/json",
            "x-api-key": "my_secret_key_12345",
        })
        assert ok is True

        ok, _ = await adapter.validate_inbound(b"{}", {
            "content-type": "application/json",
            "x-api-key": "wrong_key",
        })
        assert ok is False

    @pytest.mark.asyncio
    async def test_handle_webhook_request(self):
        from digitorn.modules.channels.adapters.webhook import WebhookAdapter

        events: list[InboundEvent] = []

        adapter = WebhookAdapter(channel_config={"inbound_path": "/hook/test"})
        await adapter.start_listener(lambda evt: events.append(evt) or asyncio.sleep(0))

        body = json.dumps({"action": "created", "id": 42}).encode()
        ok, err = await adapter.handle_webhook_request(
            body, {"content-type": "application/json"}, source_ip="1.2.3.4",
        )
        assert ok is True
        assert len(events) == 1
        assert events[0].payload["action"] == "created"
        assert events[0].payload["id"] == 42

    @pytest.mark.asyncio
    async def test_handle_webhook_sanitizes_payload(self):
        from digitorn.modules.channels.adapters.webhook import WebhookAdapter

        events: list[InboundEvent] = []
        adapter = WebhookAdapter(channel_config={"inbound_path": "/hook/test"})
        await adapter.start_listener(lambda evt: events.append(evt) or asyncio.sleep(0))

        body = json.dumps({"__proto__": "bad", "name": "good"}).encode()
        ok, _ = await adapter.handle_webhook_request(
            body, {"content-type": "application/json"},
        )
        assert ok is True
        assert "__proto__" not in events[0].payload
        assert events[0].payload["name"] == "good"

    @pytest.mark.asyncio
    async def test_handle_webhook_strips_sensitive_headers(self):
        from digitorn.modules.channels.adapters.webhook import WebhookAdapter

        events: list[InboundEvent] = []
        adapter = WebhookAdapter(channel_config={"inbound_path": "/hook/test"})
        await adapter.start_listener(lambda evt: events.append(evt) or asyncio.sleep(0))

        await adapter.handle_webhook_request(
            b'{"ok": true}',
            {
                "content-type": "application/json",
                "authorization": "Bearer secret_token",
                "x-api-key": "my_key",
                "x-custom": "visible",
            },
        )
        headers = events[0].metadata.get("headers", {})
        assert "authorization" not in headers
        assert "x-api-key" not in headers
        assert headers.get("x-custom") == "visible"


# ── Log Adapter ──────────────────────────────────────────────────────


class TestLogAdapter:
    def test_outbound_only(self):
        from digitorn.modules.channels.adapters.log import LogAdapter

        adapter = LogAdapter()
        caps = adapter.adapter_capabilities()
        assert caps.supports_inbound is False
        assert caps.supports_outbound is True

    @pytest.mark.asyncio
    async def test_deliver(self):
        from digitorn.modules.channels.adapters.log import LogAdapter

        adapter = LogAdapter(channel_config={"level": "info"})
        result = await adapter.deliver("app1", ChannelPayload(message="test alert"), {})
        assert result.success is True


# ── RSS Adapter ──────────────────────────────────────────────────────


class TestRssAdapter:
    def test_inbound_only(self):
        from digitorn.modules.channels.adapters.rss import RssAdapter

        adapter = RssAdapter(channel_config={"feed_url": "https://example.com/rss"})
        caps = adapter.adapter_capabilities()
        assert caps.supports_inbound is True
        assert caps.supports_outbound is False

    def test_deliver_returns_error(self):
        from digitorn.modules.channels.adapters.rss import RssAdapter

        adapter = RssAdapter(channel_config={"feed_url": "https://example.com/rss"})
        result = asyncio.get_event_loop().run_until_complete(
            adapter.deliver("app", ChannelPayload(message="test"), {})
        )
        assert result.success is False


# ── Email Adapter ────────────────────────────────────────────────────


class TestEmailAdapter:
    def test_capabilities(self):
        from digitorn.modules.channels.adapters.email import EmailAdapter

        adapter = EmailAdapter(channel_config={
            "imap": {"host": "imap.example.com", "user": "test@example.com", "password": "secret"},
            "smtp": {"host": "smtp.example.com"},
        })
        caps = adapter.adapter_capabilities()
        assert caps.supports_inbound is True
        assert caps.supports_outbound is True
        assert caps.supports_threading is True

    def test_no_imap_no_inbound(self):
        from digitorn.modules.channels.adapters.email import EmailAdapter

        adapter = EmailAdapter(channel_config={
            "smtp": {"host": "smtp.example.com"},
        })
        caps = adapter.adapter_capabilities()
        assert caps.supports_inbound is False
        assert caps.supports_outbound is True
