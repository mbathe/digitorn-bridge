"""Discord Bot adapter - WebSocket Gateway inbound + REST outbound.

Uses the Discord Gateway API (WebSocket) for real-time message reception
and the REST API for sending messages. Only needs ``aiohttp``.

Setup:
    1. Create app at discord.com/developers/applications
    2. Bot tab → Reset Token → copy token
    3. Bot tab → enable "Message Content Intent"
    4. OAuth2 → URL Generator → scope 'bot' → permissions: Send Messages, Read Message History
    5. Invite bot to your server via the generated URL

Security:
    - Bot token never exposed in logs or outbound messages.
    - Only responds to messages in allowed channels/guilds (if configured).
    - Ignores messages from other bots (including itself).
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
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

_API_BASE = "https://discord.com/api/v10"
_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"


class DiscordAdapter(BaseChannelAdapter):
    """Discord Bot adapter - Gateway WebSocket + REST API."""

    CHANNEL_ID = "discord"
    CHANNEL_NAME = "Discord Bot"
    CHANNEL_VERSION = "1.0.0"
    CHANNEL_DESCRIPTION = "Receive and send messages via Discord Bot API."
    SUPPORTS_INBOUND = True
    SUPPORTS_OUTBOUND = True

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        super().__init__(channel_config=channel_config)
        cfg = channel_config or {}
        self._token: str = cfg.get("token", "")
        self._allowed_channel_ids: set[str] = set(
            str(c) for c in cfg.get("allowed_channel_ids", [])
        )
        self._allowed_guild_ids: set[str] = set(
            str(g) for g in cfg.get("allowed_guild_ids", [])
        )
        self._session: Any = None
        self._ws: Any = None
        self._heartbeat_interval: float = 41.25
        self._sequence: int | None = None
        self._bot_user_id: str = ""

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_inbound=True,
            supports_outbound=True,
            supports_rich_text=True,
            max_message_length=2000,
            supported_formats=["text", "markdown"],
        )

    def adapter_capabilities(self) -> AdapterCapabilities:
        return self.capabilities()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bot {self._token}",
            "Content-Type": "application/json",
            "User-Agent": f"DiscordBot (digitorn, 1.0) Python/{platform.python_version()}",
        }

    async def _ensure_session(self) -> Any:
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession(headers=self._headers())
        return self._session

    # ── Inbound (Gateway WebSocket) ──────────────────────────────

    async def start_listener(self, callback: InboundCallback) -> None:
        if not self._token:
            logger.error("discord_no_token - set token in adapter config")
            return

        import aiohttp

        backoff = 1
        while True:
            try:
                session = await self._ensure_session()
                async with session.ws_connect(_GATEWAY_URL) as ws:
                    self._ws = ws
                    backoff = 1  # Reset on successful connect

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await self._handle_gateway_event(data, ws, callback)
                            except Exception as exc:
                                logger.warning("discord_gateway_parse_error: %s", exc)

                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning("discord_ws_disconnected, reconnecting in %ds...", backoff)
                            break

            except asyncio.CancelledError:
                logger.info("discord_adapter_stopped")
                return
            except Exception as exc:
                logger.warning("discord_connection_error: %s, reconnecting in %ds...", exc, backoff)

            # Close stale session before reconnect
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)  # Exponential backoff, max 60s

    async def _handle_gateway_event(
        self, data: dict, ws: Any, callback: InboundCallback,
    ) -> None:
        """Process a single Gateway event."""
        op = data.get("op")
        event_type = data.get("t")
        payload = data.get("d", {})

        # Track sequence for heartbeats
        if data.get("s") is not None:
            self._sequence = data["s"]

        # Op 10: Hello - start heartbeat + identify
        if op == 10:
            self._heartbeat_interval = payload.get("heartbeat_interval", 41250) / 1000
            asyncio.create_task(self._heartbeat_loop(ws))
            await self._identify(ws)

        # Op 1: Heartbeat request
        elif op == 1:
            await ws.send_json({"op": 1, "d": self._sequence})

        # Op 11: Heartbeat ACK (ignore)
        elif op == 11:
            pass

        # Op 0: Dispatch
        elif op == 0:
            if event_type == "READY":
                user = payload.get("user", {})
                self._bot_user_id = str(user.get("id", ""))
                logger.info(
                    "discord_adapter_started bot=%s#%s",
                    user.get("username", "?"), user.get("discriminator", "0"),
                )

            elif event_type == "MESSAGE_CREATE":
                await self._handle_message(payload, callback)

    async def _identify(self, ws: Any) -> None:
        """Send IDENTIFY payload to authenticate."""
        await ws.send_json({
            "op": 2,
            "d": {
                "token": self._token,
                "intents": 33281,  # GUILDS | GUILD_MESSAGES | MESSAGE_CONTENT
                "properties": {
                    "os": platform.system().lower(),
                    "browser": "digitorn",
                    "device": "digitorn",
                },
            },
        })

    async def _heartbeat_loop(self, ws: Any) -> None:
        """Send periodic heartbeats to keep the connection alive."""
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                await ws.send_json({"op": 1, "d": self._sequence})
        except (asyncio.CancelledError, Exception):
            pass

    async def _handle_message(
        self, payload: dict, callback: InboundCallback,
    ) -> None:
        """Process a MESSAGE_CREATE event."""
        author = payload.get("author", {})
        author_id = str(author.get("id", ""))

        # Ignore messages from bots (including ourselves)
        if author.get("bot", False):
            return

        # Ignore our own messages
        if author_id == self._bot_user_id:
            return

        channel_id = str(payload.get("channel_id", ""))
        guild_id = str(payload.get("guild_id", ""))
        text = payload.get("content", "")

        if not text:
            return

        # Filter by allowed channels/guilds
        if self._allowed_channel_ids and channel_id not in self._allowed_channel_ids:
            return
        if self._allowed_guild_ids and guild_id not in self._allowed_guild_ids:
            return

        username = author.get("username", "")
        display_name = author.get("global_name", username)

        event = InboundEvent(
            event_id=make_event_id(),
            provider_id="",
            adapter_type="discord",
            source=author_id,
            message=text,
            payload={
                "channel_id": channel_id,
                "guild_id": guild_id,
                "message_id": payload.get("id", ""),
                "author_id": author_id,
                "username": username,
                "display_name": display_name,
                "discriminator": author.get("discriminator", "0"),
                "text": text,
            },
            metadata={
                "message_id": payload.get("id", ""),
            },
            reply_context={
                "_app_id": "",
                "channel_id": channel_id,
            },
        )

        try:
            await callback(event)
        except Exception as exc:
            logger.error(
                "discord_callback_error channel=%s error=%s",
                channel_id, exc, exc_info=True,
            )

    async def stop_listener(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ── Outbound (REST API) ──────────────────────────────────────

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
                error="Discord bot token not configured.",
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

        # Discord limit is 2000 chars
        if len(text) > 2000:
            text = text[:1994] + "\n[...]"

        session = await self._ensure_session()
        url = f"{_API_BASE}/channels/{channel_id}/messages"

        try:
            async with session.post(url, json={"content": text}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return DeliveryResult(
                        success=True,
                        channel_id=self.CHANNEL_ID,
                        delivery_id=data.get("id", ""),
                    )
                else:
                    error_text = await resp.text()
                    return DeliveryResult(
                        success=False,
                        channel_id=self.CHANNEL_ID,
                        error=f"HTTP {resp.status}: {error_text[:200]}",
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
        message_id: str,
        text: str,
    ) -> bool:
        """Edit an existing Discord message (for streaming updates)."""
        channel_id = reply_context.get("channel_id")
        if not channel_id or not message_id:
            return False
        if len(text) > 2000:
            text = text[:1994] + "\n[...]"
        session = await self._ensure_session()
        try:
            async with session.patch(
                f"{_API_BASE}/channels/{channel_id}/messages/{message_id}",
                json={"content": text},
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
