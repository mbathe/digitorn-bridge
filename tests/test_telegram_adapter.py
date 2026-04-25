"""Tests for the Telegram adapter — unit tests (no real API calls)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.modules.channels.adapter import InboundEvent
from digitorn.modules.channels.adapters.telegram import TelegramAdapter
from digitorn.core.app.channels.base import ChannelPayload


# ── Helpers ─────────────────────────────────────────────────────────


def _make_adapter(
    token: str = "123:ABC",
    allowed_chat_ids: list[int] | None = None,
) -> TelegramAdapter:
    cfg: dict[str, Any] = {"token": token, "poll_timeout": 1}
    if allowed_chat_ids:
        cfg["allowed_chat_ids"] = allowed_chat_ids
    return TelegramAdapter(channel_config=cfg)


def _telegram_update(
    text: str = "Hello bot",
    chat_id: int = 42,
    username: str = "testuser",
    first_name: str = "Test",
    update_id: int = 100,
    message_id: int = 1,
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {
                "id": chat_id,
                "is_bot": False,
                "first_name": first_name,
                "username": username,
            },
            "chat": {
                "id": chat_id,
                "type": "private",
            },
            "text": text,
        },
    }


# ── Capabilities ────────────────────────────────────────────────────


class TestTelegramCapabilities:
    def test_supports_inbound_and_outbound(self):
        adapter = _make_adapter()
        caps = adapter.adapter_capabilities()
        assert caps.supports_inbound is True
        assert caps.supports_outbound is True
        assert caps.max_message_length == 4096

    def test_channel_id(self):
        adapter = _make_adapter()
        assert adapter.CHANNEL_ID == "telegram"


# ── Parse Update ────────────────────────────────────────────────────


class TestParseUpdate:
    def test_text_message_parsed(self):
        adapter = _make_adapter()
        update = _telegram_update(text="Bonjour", chat_id=99, username="paul")
        event = adapter._parse_update(update)

        assert event is not None
        assert event.adapter_type == "telegram"
        assert event.source == "99"
        assert event.message == "Bonjour"
        assert event.payload["chat_id"] == 99
        assert event.payload["username"] == "paul"
        assert event.payload["chat_type"] == "private"
        assert event.reply_context["chat_id"] == 99

    def test_no_text_returns_none(self):
        adapter = _make_adapter()
        update = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from": {"id": 1},
                "chat": {"id": 1, "type": "private"},
                # no "text" key — photo, sticker, etc.
            },
        }
        assert adapter._parse_update(update) is None

    def test_no_message_returns_none(self):
        adapter = _make_adapter()
        update = {"update_id": 1}  # edited_message, callback_query, etc.
        assert adapter._parse_update(update) is None

    def test_display_name_fallback(self):
        adapter = _make_adapter()
        # No username → falls back to first_name
        update = _telegram_update(username="", first_name="Alice")
        event = adapter._parse_update(update)
        assert event.payload["display_name"] == "Alice"

        # No username, no first_name → falls back to chat_id
        update2 = _telegram_update(username="", first_name="")
        event2 = adapter._parse_update(update2)
        assert event2.payload["display_name"] == str(update2["message"]["chat"]["id"])

    def test_offset_advances(self):
        adapter = _make_adapter()
        assert adapter._offset == 0

        update1 = _telegram_update(update_id=100)
        update2 = _telegram_update(update_id=101)

        # Simulate what _poll_updates does with the offset
        updates = [update1, update2]
        adapter._offset = updates[-1]["update_id"] + 1
        assert adapter._offset == 102


# ── Deliver (sendMessage) ───────────────────────────────────────────


class TestTelegramDeliver:
    @pytest.mark.asyncio
    async def test_send_message_success(self):
        adapter = _make_adapter()

        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={
            "ok": True,
            "result": {"message_id": 42},
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=mock_resp)
        adapter._session = mock_session

        result = await adapter.deliver(
            app_id="test",
            payload=ChannelPayload(message="Hello from Digitorn!"),
            config={"chat_id": 99},
        )
        assert result.success is True
        assert result.delivery_id == "42"

    @pytest.mark.asyncio
    async def test_send_no_token(self):
        adapter = TelegramAdapter(channel_config={"token": ""})
        result = await adapter.deliver(
            app_id="test",
            payload=ChannelPayload(message="test"),
            config={"chat_id": 99},
        )
        assert result.success is False
        assert "token" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_no_chat_id(self):
        adapter = _make_adapter()
        result = await adapter.deliver(
            app_id="test",
            payload=ChannelPayload(message="test"),
            config={},  # no chat_id
        )
        assert result.success is False
        assert "chat_id" in result.error.lower()

    @pytest.mark.asyncio
    async def test_long_message_truncated(self):
        adapter = _make_adapter()

        sent_body = {}

        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"ok": True, "result": {"message_id": 1}})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        def capture_post(url, json=None):
            sent_body.update(json or {})
            return mock_resp

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = capture_post
        adapter._session = mock_session

        long_text = "x" * 5000
        await adapter.deliver(
            app_id="test",
            payload=ChannelPayload(message=long_text),
            config={"chat_id": 1},
        )
        assert len(sent_body["text"]) <= 4096

    @pytest.mark.asyncio
    async def test_markdown_fallback_on_parse_error(self):
        adapter = _make_adapter()

        call_count = 0

        async def mock_json():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"ok": False, "description": "Bad Request: can't parse entities: markdown"}
            return {"ok": True, "result": {"message_id": 2}}

        mock_resp = AsyncMock()
        mock_resp.json = mock_json
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=mock_resp)
        adapter._session = mock_session

        result = await adapter.deliver(
            app_id="test",
            payload=ChannelPayload(message="*broken markdown"),
            config={"chat_id": 1},
        )
        assert result.success is True
        assert call_count == 2  # First with Markdown, retry without


# ── Reply ───────────────────────────────────────────────────────────


class TestTelegramReply:
    @pytest.mark.asyncio
    async def test_send_reply_uses_reply_context(self):
        adapter = _make_adapter()

        sent_body = {}

        mock_resp = AsyncMock()
        mock_resp.json = AsyncMock(return_value={"ok": True, "result": {"message_id": 5}})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        def capture_post(url, json=None):
            sent_body.update(json or {})
            return mock_resp

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = capture_post
        adapter._session = mock_session

        result = await adapter.send_reply(
            reply_context={"_app_id": "test", "chat_id": 42, "reply_to_message_id": 10},
            text="Got it!",
        )
        assert result.success is True
        assert sent_body["chat_id"] == 42
        assert sent_body["reply_to_message_id"] == 10


# ── Chat filtering ──────────────────────────────────────────────────


class TestChatFiltering:
    def test_allowed_chat_ids_stored(self):
        adapter = _make_adapter(allowed_chat_ids=[100, 200])
        assert adapter._allowed_chat_ids == {100, 200}

    def test_empty_allowed_means_no_filter(self):
        adapter = _make_adapter()
        assert adapter._allowed_chat_ids == set()
