"""Voice adapter - bidirectional voice calls via pluggable backends.

The voice adapter is a standard channel adapter that handles voice calls.
Audio transport (STT, TTS, VAD, codecs) is delegated to a backend.
The adapter only sees text: transcripts in, responses out.

Backends:
    twilio_cr   - Twilio ConversationRelay (hosted STT+TTS)
    websocket   - Generic WebSocket (bring your own STT/TTS)
    livekit     - LiveKit Agents (open-source, self-hosted)
    pipecat     - Pipecat/Daily (open-source)
"""

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
from digitorn.modules.channels.adapters.voice.base import (
    VoiceBackend,
    VoiceCallbacks,
    get_voice_backend,
)

logger = logging.getLogger(__name__)


class VoiceAdapter(BaseChannelAdapter):
    """Voice channel adapter - text interface to voice backends."""

    CHANNEL_ID = "voice"
    CHANNEL_NAME = "Voice Call"
    CHANNEL_VERSION = "1.0.0"
    CHANNEL_DESCRIPTION = "Bidirectional voice calls (phone, WebRTC, SIP)."
    SUPPORTS_INBOUND = True
    SUPPORTS_OUTBOUND = True

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        super().__init__(channel_config=channel_config)
        cfg = channel_config or {}
        self._backend_type: str = cfg.get("backend", "")
        self._backend_config: dict[str, Any] = cfg.get("backend_config", {})
        self._language: str = cfg.get("language", "en")
        self._welcome: str = cfg.get("welcome", "")
        self._backend: VoiceBackend | None = None
        self._callback: InboundCallback | None = None

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_inbound=True,
            supports_outbound=True,
            max_message_length=4000,
            supported_formats=["text"],
        )

    def adapter_capabilities(self) -> AdapterCapabilities:
        return self.capabilities()

    async def start_listener(self, callback: InboundCallback) -> None:
        if not self._backend_type:
            logger.error("voice_no_backend - set backend in adapter config")
            return

        self._callback = callback

        try:
            self._backend = get_voice_backend(
                self._backend_type,
                {
                    **self._backend_config,
                    "language": self._language,
                    "welcome": self._welcome,
                },
            )
        except ValueError as exc:
            logger.error("voice_backend_init_error: %s", exc)
            return

        callbacks = VoiceCallbacks(
            on_transcript=self._on_transcript,
            on_call_end=self._on_call_end,
        )

        try:
            await self._backend.start(callbacks)
            logger.info(
                "voice_adapter_started backend=%s language=%s",
                self._backend_type, self._language,
            )
        except Exception as exc:
            logger.error("voice_backend_start_error: %s", exc)

    async def _on_transcript(
        self, call_id: str, text: str, metadata: dict[str, Any],
    ) -> None:
        if not text.strip() or not self._callback:
            return

        call_info = {}
        if self._backend:
            call = self._backend.active_calls.get(call_id)
            if call:
                call_info = {
                    "caller": call.caller,
                    "direction": call.direction,
                    "language": call.language,
                }

        event = InboundEvent(
            event_id=make_event_id(),
            provider_id="",
            adapter_type="voice",
            source=call_info.get("caller", call_id),
            message=text,
            payload={
                "call_id": call_id,
                "transcript": text,
                "caller": call_info.get("caller", ""),
                "direction": call_info.get("direction", "inbound"),
                "language": call_info.get("language", self._language),
                **metadata,
            },
            reply_context={
                "_app_id": "",
                "call_id": call_id,
            },
        )

        try:
            await self._callback(event)
        except Exception as exc:
            logger.error(
                "voice_callback_error call_id=%s error=%s",
                call_id, exc, exc_info=True,
            )

    async def _on_call_end(self, call_id: str) -> None:
        logger.info("voice_call_ended call_id=%s", call_id)

    async def stop_listener(self) -> None:
        if self._backend:
            await self._backend.stop()
            self._backend = None
        logger.info("voice_adapter_stopped backend=%s", self._backend_type)

    async def deliver(
        self,
        app_id: str,
        payload: ChannelPayload,
        config: dict[str, Any],
    ) -> DeliveryResult:
        if not self._backend:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="Voice backend not started.",
            )

        call_id = config.get("call_id")
        if not call_id:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="No call_id specified.",
            )

        text = payload.message
        if not text:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="Empty message.",
            )

        success = await self._backend.speak(call_id, text)
        return DeliveryResult(
            success=success,
            channel_id=self.CHANNEL_ID,
            delivery_id=call_id,
            error="" if success else "Failed to speak in call.",
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
