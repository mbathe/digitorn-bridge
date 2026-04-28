"""Slack adapter - Socket Mode inbound + Web API outbound.

Uses the Slack Socket Mode WebSocket for real-time event reception
and the Web API (chat.postMessage) for sending messages.
Only needs ``aiohttp`` - no slack-sdk dependency.

Setup:
    1. Create app at api.slack.com/apps
    2. Enable Socket Mode → generate App-Level Token (xapp-...)
    3. Event Subscriptions → subscribe to message.channels, message.im
    4. OAuth Permissions → chat:write, channels:history, channels:read
    5. Install to Workspace → copy Bot Token (xoxb-...)

Security:
    - Tokens never exposed in logs or outbound messages.
    - Only responds to human messages (ignores bots, itself, subtypes).
    - Socket Mode uses WSS (encrypted) with server-verified connections.
"""

from __future__ import annotations

import asyncio
import json
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

_API_BASE = "https://slack.com/api"


class SlackAdapter(BaseChannelAdapter):
    """Slack adapter - Socket Mode WebSocket + Web API."""

    CHANNEL_ID = "slack"
    CHANNEL_NAME = "Slack Bot"
    CHANNEL_VERSION = "1.0.0"
    CHANNEL_DESCRIPTION = "Receive and send messages via Slack Socket Mode."
    SUPPORTS_INBOUND = True
    SUPPORTS_OUTBOUND = True

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        super().__init__(channel_config=channel_config)
        cfg = channel_config or {}
        self._bot_token: str = cfg.get("bot_token", "")     # xoxb-...
        self._app_token: str = cfg.get("app_token", "")     # xapp-...
        self._allowed_channel_ids: set[str] = set(cfg.get("allowed_channel_ids", []))
        self._session: Any = None
        self._ws: Any = None
        self._bot_user_id: str = ""

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_inbound=True,
            supports_outbound=True,
            supports_rich_text=True,
            max_message_length=4000,
            supported_formats=["text", "mrkdwn"],
        )

    def adapter_capabilities(self) -> AdapterCapabilities:
        return self.capabilities()

    async def _ensure_session(self) -> Any:
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    # ── Inbound (Socket Mode WebSocket) ──────────────────────────

    async def start_listener(self, callback: InboundCallback) -> None:
        if not self._bot_token or not self._app_token:
            logger.error("slack_missing_tokens - need bot_token (xoxb-) and app_token (xapp-)")
            return

        session = await self._ensure_session()

        # Get bot user ID
        try:
            async with session.post(
                f"{_API_BASE}/auth.test",
                headers={"Authorization": f"Bearer {self._bot_token}"},
            ) as resp:
                data = await resp.json()
                if not data.get("ok"):
                    logger.error("slack_auth_failed: %s", data.get("error"))
                    return
                self._bot_user_id = data.get("user_id", "")
                logger.info(
                    "slack_bot_authenticated user=%s team=%s",
                    data.get("user", "?"), data.get("team", "?"),
                )
        except Exception as exc:
            logger.error("slack_auth_error: %s", exc)
            return

        # Connect via Socket Mode with auto-reconnect
        import aiohttp

        backoff = 1
        while True:
            try:
                ws_url = await self._get_ws_url(session)
                if not ws_url:
                    logger.warning("slack_no_ws_url, retrying in %ds...", backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue

                async with session.ws_connect(ws_url) as ws:
                    self._ws = ws
                    backoff = 1
                    logger.info("slack_adapter_started via Socket Mode")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await self._handle_socket_event(data, ws, callback)
                            except Exception as exc:
                                logger.warning("slack_event_parse_error: %s", exc)

                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning("slack_ws_disconnected, reconnecting in %ds...", backoff)
                            break

            except asyncio.CancelledError:
                logger.info("slack_adapter_stopped")
                return
            except Exception as exc:
                logger.warning("slack_connection_error: %s, reconnecting in %ds...", exc, backoff)

            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None
            session = await self._ensure_session()

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _get_ws_url(self, session: Any) -> str:
        """Get WebSocket URL via apps.connections.open."""
        try:
            async with session.post(
                f"{_API_BASE}/apps.connections.open",
                headers={"Authorization": f"Bearer {self._app_token}"},
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return data["url"]
                else:
                    logger.error("slack_socket_connect_failed: %s", data.get("error"))
                    return ""
        except Exception as exc:
            logger.error("slack_socket_connect_error: %s", exc)
            return ""

    async def _handle_socket_event(
        self, data: dict, ws: Any, callback: InboundCallback,
    ) -> None:
        """Process a Socket Mode envelope."""
        envelope_type = data.get("type")

        # Acknowledge all envelopes immediately
        envelope_id = data.get("envelope_id")
        if envelope_id:
            await ws.send_json({"envelope_id": envelope_id})

        if envelope_type == "hello":
            logger.debug("slack_socket_hello")
            return

        if envelope_type == "disconnect":
            logger.info("slack_socket_disconnect_requested reason=%s", data.get("reason"))
            return

        if envelope_type != "events_api":
            return

        # Extract the actual event
        payload = data.get("payload", {})
        event = payload.get("event", {})
        event_type = event.get("type")

        if event_type == "message":
            await self._handle_message(event, callback)

    async def _handle_message(
        self, event: dict, callback: InboundCallback,
    ) -> None:
        """Process a message event."""
        # Ignore bot messages, subtypes (join, leave, etc.), and our own messages
        if event.get("bot_id"):
            return
        if event.get("subtype"):
            return
        user_id = event.get("user", "")
        if not user_id or user_id == self._bot_user_id:
            return

        text = event.get("text", "")
        if not text:
            return

        channel_id = event.get("channel", "")

        # Filter by allowed channels
        if self._allowed_channel_ids and channel_id not in self._allowed_channel_ids:
            return

        channel_type = event.get("channel_type", "channel")

        inbound = InboundEvent(
            event_id=make_event_id(),
            provider_id="",
            adapter_type="slack",
            source=user_id,
            message=text,
            payload={
                "channel_id": channel_id,
                "user_id": user_id,
                "text": text,
                "ts": event.get("ts", ""),
                "channel_type": channel_type,
                "thread_ts": event.get("thread_ts", ""),
            },
            metadata={
                "ts": event.get("ts", ""),
                "event_ts": event.get("event_ts", ""),
            },
            reply_context={
                "_app_id": "",
                "channel_id": channel_id,
                "thread_ts": event.get("ts", ""),  # Reply in thread
            },
        )

        try:
            await callback(inbound)
        except Exception as exc:
            logger.error(
                "slack_callback_error channel=%s error=%s",
                channel_id, exc, exc_info=True,
            )

    async def stop_listener(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ── Outbound (Web API) ───────────────────────────────────────

    async def deliver(
        self,
        app_id: str,
        payload: ChannelPayload,
        config: dict[str, Any],
    ) -> DeliveryResult:
        if not self._bot_token:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="Slack bot token not configured.",
            )

        channel_id = config.get("channel_id")
        if not channel_id:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="No channel_id specified.",
            )

        text = payload.message
        if not text:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="Empty message.",
            )

        # Slack limit is ~4000 chars for text
        if len(text) > 4000:
            text = text[:3994] + "\n[...]"

        session = await self._ensure_session()

        body: dict[str, Any] = {
            "channel": channel_id,
            "text": text,
        }

        # Thread reply if thread_ts is set
        thread_ts = config.get("thread_ts")
        if thread_ts:
            body["thread_ts"] = thread_ts

        try:
            async with session.post(
                f"{_API_BASE}/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {self._bot_token}",
                    "Content-Type": "application/json",
                },
                json=body,
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return DeliveryResult(
                        success=True,
                        channel_id=self.CHANNEL_ID,
                        delivery_id=data.get("ts", ""),
                    )
                else:
                    return DeliveryResult(
                        success=False,
                        channel_id=self.CHANNEL_ID,
                        error=data.get("error", "Unknown Slack error")[:200],
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

    async def edit_message(
        self,
        reply_context: dict[str, Any],
        message_ts: str,
        text: str,
    ) -> bool:
        """Edit an existing Slack message (for streaming updates)."""
        channel_id = reply_context.get("channel_id")
        if not channel_id or not message_ts:
            return False
        if len(text) > 4000:
            text = text[:3994] + "\n[...]"
        session = await self._ensure_session()
        try:
            async with session.post(
                f"{_API_BASE}/chat.update",
                headers={
                    "Authorization": f"Bearer {self._bot_token}",
                    "Content-Type": "application/json",
                },
                json={"channel": channel_id, "ts": message_ts, "text": text},
            ) as resp:
                data = await resp.json()
                return data.get("ok", False)
        except Exception:
            return False
