"""Twilio ConversationRelay backend.

Twilio handles STT and TTS internally. This backend only exchanges
text over WebSocket - no audio processing needed.

Flow:
    1. Incoming call → Twilio hits our TwiML webhook
    2. We return <Connect><ConversationRelay> pointing to our WS server
    3. Twilio opens a WebSocket to us
    4. We receive transcripts (text), we send responses (text)
    5. Twilio synthesizes speech and plays it to the caller

Requires: aiohttp (already a project dependency)
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from digitorn.modules.channels.adapters.voice.base import (
    VoiceBackend,
    VoiceCall,
    VoiceCallbacks,
)

logger = logging.getLogger(__name__)


class TwilioCRBackend(VoiceBackend):
    """Twilio ConversationRelay - hosted STT+TTS, text-only WebSocket."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._host: str = config.get("host", "0.0.0.0")
        self._port: int = config.get("port", 8765)
        self._public_url: str = config.get("public_url", "")
        self._language: str = config.get("language", "en")
        self._welcome: str = config.get("welcome", "")
        self._tts_provider: str = config.get("tts_provider", "google")
        self._stt_provider: str = config.get("stt_provider", "deepgram")
        self._voice: str = config.get("voice", "")
        self._interruptible: str = config.get("interruptible", "speech")

        self._callbacks: VoiceCallbacks | None = None
        self._calls: dict[str, VoiceCall] = {}
        self._ws_connections: dict[str, Any] = {}
        self._runner: Any = None
        self._site: Any = None

    @property
    def active_calls(self) -> dict[str, VoiceCall]:
        return self._calls

    async def start(self, callbacks: VoiceCallbacks) -> None:
        import aiohttp.web

        self._callbacks = callbacks

        app = aiohttp.web.Application()
        app.router.add_post("/voice/incoming", self._handle_twiml)
        app.router.add_get("/voice/ws", self._handle_websocket)
        app.router.add_get("/voice/status", self._handle_status)

        self._runner = aiohttp.web.AppRunner(app)
        await self._runner.setup()
        self._site = aiohttp.web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

        ws_url = self._public_url or f"ws://{self._host}:{self._port}"
        logger.info(
            "twilio_cr_started port=%d twiml=%s/voice/incoming ws=%s/voice/ws",
            self._port,
            self._public_url or f"http://{self._host}:{self._port}",
            ws_url,
        )

    async def stop(self) -> None:
        for call_id in list(self._ws_connections):
            await self.hangup(call_id)
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("twilio_cr_stopped")

    async def speak(self, call_id: str, text: str) -> bool:
        ws = self._ws_connections.get(call_id)
        if not ws or ws.closed:
            return False

        try:
            await ws.send_json({
                "type": "text",
                "token": text,
                "last": True,
            })
            return True
        except Exception as exc:
            logger.warning("twilio_cr_speak_error call=%s: %s", call_id, exc)
            return False

    async def hangup(self, call_id: str) -> None:
        ws = self._ws_connections.pop(call_id, None)
        if ws and not ws.closed:
            try:
                await ws.close()
            except Exception:
                pass
        self._calls.pop(call_id, None)

    async def _handle_twiml(self, request: Any) -> Any:
        import aiohttp.web

        ws_url = self._public_url or f"ws://localhost:{self._port}"
        ws_url = ws_url.replace("http://", "ws://").replace("https://", "wss://")

        cr_attrs = [
            f'url="{ws_url}/voice/ws"',
            f'interruptible="{self._interruptible}"',
        ]
        if self._tts_provider:
            cr_attrs.append(f'ttsProvider="{self._tts_provider}"')
        if self._stt_provider:
            cr_attrs.append(f'transcriptionProvider="{self._stt_provider}"')
        if self._voice:
            cr_attrs.append(f'voice="{self._voice}"')
        if self._language:
            cr_attrs.append(f'language="{self._language}"')
        if self._welcome:
            cr_attrs.append(f'welcomeGreeting="{self._welcome}"')

        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            "<Connect>"
            f'<ConversationRelay {" ".join(cr_attrs)} />'
            "</Connect>"
            "</Response>"
        )

        logger.info("twilio_cr_twiml_served")
        return aiohttp.web.Response(
            text=twiml,
            content_type="application/xml",
        )

    async def _handle_websocket(self, request: Any) -> Any:
        import aiohttp.web

        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)

        call_id = f"call-{uuid.uuid4().hex[:12]}"
        self._calls[call_id] = VoiceCall(
            call_id=call_id,
            caller="",
            direction="inbound",
            language=self._language,
        )
        self._ws_connections[call_id] = ws

        logger.info("twilio_cr_call_started call_id=%s", call_id)

        try:
            async for msg in ws:
                if msg.type == aiohttp.web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_cr_message(call_id, data)
                    except json.JSONDecodeError:
                        pass
                elif msg.type == aiohttp.web.WSMsgType.ERROR:
                    logger.warning(
                        "twilio_cr_ws_error call=%s: %s",
                        call_id, ws.exception(),
                    )
        except asyncio.CancelledError:
            pass
        finally:
            self._ws_connections.pop(call_id, None)
            call = self._calls.pop(call_id, None)
            logger.info("twilio_cr_call_ended call_id=%s", call_id)
            if self._callbacks and self._callbacks.on_call_end:
                await self._callbacks.on_call_end(call_id)

        return ws

    async def _handle_cr_message(self, call_id: str, data: dict) -> None:
        msg_type = data.get("type", "")

        if msg_type == "setup":
            caller = data.get("from", "")
            call = self._calls.get(call_id)
            if call:
                call.caller = caller
                call.metadata = {
                    "twilio_call_sid": data.get("callSid", ""),
                    "to": data.get("to", ""),
                }
            logger.info(
                "twilio_cr_setup call=%s from=%s to=%s",
                call_id, caller, data.get("to", ""),
            )

        elif msg_type == "prompt":
            transcript = data.get("voicePrompt", "")
            if transcript and self._callbacks and self._callbacks.on_transcript:
                await self._callbacks.on_transcript(
                    call_id,
                    transcript,
                    {"confidence": data.get("confidence", 0)},
                )

        elif msg_type == "interrupt":
            logger.debug("twilio_cr_interrupted call=%s", call_id)

        elif msg_type == "dtmf":
            digit = data.get("digit", "")
            logger.debug("twilio_cr_dtmf call=%s digit=%s", call_id, digit)

        elif msg_type == "error":
            logger.warning(
                "twilio_cr_error call=%s desc=%s",
                call_id, data.get("description", ""),
            )

    async def _handle_status(self, request: Any) -> Any:
        import aiohttp.web

        return aiohttp.web.json_response({
            "backend": "twilio_cr",
            "active_calls": len(self._calls),
            "calls": [
                {"call_id": c.call_id, "caller": c.caller, "direction": c.direction}
                for c in self._calls.values()
            ],
        })
