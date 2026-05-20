"""Telegram Bot adapter - long polling inbound + HTTP outbound."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from digitorn.core.app.channels.base import ChannelPayload, DeliveryResult
from digitorn.modules.channels.adapter import (
    AdapterCapabilities,
    BaseChannelAdapter,
    InboundCallback,
    InboundEvent,
    make_event_id,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.telegram.org/bot{token}"

class TelegramAdapter(BaseChannelAdapter):
    """Telegram Bot API adapter - long polling + sendMessage."""

    CHANNEL_ID = "telegram"
    CHANNEL_NAME = "Telegram Bot"
    CHANNEL_VERSION = "1.0.0"
    CHANNEL_DESCRIPTION = "Receive and send messages via Telegram Bot API."
    SUPPORTS_INBOUND = True
    SUPPORTS_OUTBOUND = True

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        super().__init__(channel_config=channel_config)
        cfg = channel_config or {}
        self._token: str = cfg.get("token", "")
        self._poll_timeout: int = cfg.get("poll_timeout", 30)
        self._allowed_chat_ids: set[int] = set(cfg.get("allowed_chat_ids", []))
        self._session: Any = None  # aiohttp.ClientSession
        self._offset: int = 0

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_inbound=True,
            supports_outbound=True,
            supports_rich_text=True,
            max_message_length=4096,
            supported_formats=["text", "markdown"],
        )

    def adapter_capabilities(self) -> AdapterCapabilities:
        return self.capabilities()

    def _api_url(self, method: str) -> str:
        return f"{_BASE_URL.format(token=self._token)}/{method}"

    async def _ensure_session(self) -> Any:
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._poll_timeout + 10),
            )
        return self._session

    async def start_listener(self, callback: InboundCallback) -> None:
        if not self._token:
            logger.error("telegram_no_token - set token in adapter config")
            return

        session = await self._ensure_session()

        # Verify bot token
        try:
            async with session.get(self._api_url("getMe")) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.error("telegram_invalid_token: %s", data.get("description"))
                    return
                bot_name = data["result"].get("username", "unknown")
                logger.info("telegram_adapter_started bot=@%s", bot_name)
        except Exception as exc:
            logger.error("telegram_connect_error: %s", exc)
            return

        try:
            while True:
                try:
                    updates = await self._poll_updates(session)
                    for update in updates:
                        event = self._parse_update(update)
                        if event is None:
                            continue

                        # Filter by allowed chat IDs (if configured)
                        chat_id = update.get("message", {}).get("chat", {}).get("id")
                        if self._allowed_chat_ids and chat_id not in self._allowed_chat_ids:
                            logger.debug(
                                "telegram_chat_filtered chat_id=%s", chat_id,
                            )
                            continue

                        try:
                            await callback(event)
                        except Exception as exc:
                            logger.error(
                                "telegram_callback_error chat_id=%s error=%s",
                                chat_id, exc, exc_info=True,
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("telegram_poll_error: %s", exc)
                    await asyncio.sleep(5)

        except asyncio.CancelledError:
            logger.info("telegram_adapter_stopped")

    async def _poll_updates(self, session: Any) -> list[dict]:
        params: dict[str, Any] = {
            "timeout": self._poll_timeout,
            "allowed_updates": ["message"],
        }
        if self._offset:
            params["offset"] = self._offset

        async with session.get(
            self._api_url("getUpdates"), params=params,
        ) as resp:
            data = await resp.json()

        if not data.get("ok"):
            logger.warning("telegram_updates_error: %s", data.get("description"))
            return []

        updates = data.get("result", [])
        if updates:
            # Advance offset past the last update
            self._offset = updates[-1]["update_id"] + 1

        return updates

    def _parse_update(self, update: dict) -> InboundEvent | None:
        message = update.get("message")
        if not message:
            return None

        text = message.get("text", "")
        if not text:
            # Skip non-text messages (photos, stickers, etc.)
            return None

        chat = message.get("chat", {})
        user = message.get("from", {})
        chat_id = chat.get("id", 0)

        source = str(chat_id)
        username = user.get("username", "")
        first_name = user.get("first_name", "")
        display_name = username or first_name or source

        return InboundEvent(
            event_id=make_event_id(),
            provider_id="",  # Set by module
            adapter_type="telegram",
            source=source,
            message=text,
            payload={
                "chat_id": chat_id,
                "message_id": message.get("message_id", 0),
                "from_id": user.get("id", 0),
                "username": username,
                "first_name": first_name,
                "last_name": user.get("last_name", ""),
                "display_name": display_name,
                "chat_type": chat.get("type", "private"),
                "text": text,
            },
            metadata={
                "update_id": update.get("update_id", 0),
            },
            reply_context={
                "_app_id": "",
                "chat_id": chat_id,
                "reply_to_message_id": message.get("message_id", 0),
            },
        )

    async def stop_listener(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def deliver(
        self,
        app_id: str,
        payload: ChannelPayload,
        config: dict[str, Any],
    ) -> DeliveryResult:
        if not self._token:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="Telegram bot token not configured.",
            )

        chat_id = config.get("chat_id")
        if not chat_id:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="No chat_id specified.",
            )

        text = payload.message
        if not text:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="Empty message.",
            )

        # Telegram message limit is 4096 chars
        if len(text) > 4096:
            text = text[:4090] + "\n[...]"

        session = await self._ensure_session()

        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        reply_to = config.get("reply_to_message_id")
        if reply_to:
            body["reply_to_message_id"] = reply_to

        try:
            async with session.post(
                self._api_url("sendMessage"), json=body,
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    msg_id = data["result"].get("message_id", "")
                    return DeliveryResult(
                        success=True,
                        channel_id=self.CHANNEL_ID,
                        delivery_id=str(msg_id),
                    )
                else:
                    error = data.get("description", "Unknown Telegram error")
                    # Fallback: retry without Markdown if parse error
                    if "parse" in error.lower() or "markdown" in error.lower():
                        body["parse_mode"] = ""
                        async with session.post(
                            self._api_url("sendMessage"), json=body,
                        ) as retry_resp:
                            retry_data = await retry_resp.json()
                            if retry_data.get("ok"):
                                return DeliveryResult(
                                    success=True,
                                    channel_id=self.CHANNEL_ID,
                                    delivery_id=str(retry_data["result"].get("message_id", "")),
                                )
                    return DeliveryResult(
                        success=False,
                        channel_id=self.CHANNEL_ID,
                        error=error[:200],
                    )
        except Exception as exc:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error=str(exc)[:200],
                retryable=True,
            )

    async def send_reply(
        self,
        reply_context: dict[str, Any],
        text: str,
        payload: ChannelPayload | None = None,
    ) -> DeliveryResult:
        effective_payload = payload or ChannelPayload(message=text)
        return await self.deliver(
            app_id=reply_context.get("_app_id", ""),
            payload=effective_payload,
            config=reply_context,
        )
