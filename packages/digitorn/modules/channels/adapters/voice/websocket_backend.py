"""Generic WebSocket voice backend - full-duplex streaming.

Supports two modes:
- Turn-based: client sends transcript, server responds with complete audio
- Full-duplex: server streams TTS audio sentence-by-sentence as LLM generates
  tokens. Client can interrupt (barge-in) at any time.

Protocol:
    Client → Server: {"type": "transcript", "text": "..."}
    Client → Server: {"type": "interrupt"}  (barge-in: stop current speech)
    Server → Client: {"type": "speak_start", "call_id": "..."}
    Server → Client: binary audio chunks (mp3)
    Server → Client: {"type": "speak_end", "call_id": "..."}
    Server → Client: {"type": "speak", "text": "..."} (browser TTS fallback)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any

from digitorn.modules.channels.adapters.voice.base import (
    VoiceBackend,
    VoiceCall,
    VoiceCallbacks,
)

logger = logging.getLogger(__name__)

_SENTENCE_SPLIT = re.compile(r'(?<=[.!?…])\s+|(?<=\n)\s*')


class WebSocketVoiceBackend(VoiceBackend):
    """Generic WebSocket backend - full-duplex voice streaming."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._host: str = config.get("host", "0.0.0.0")
        self._port: int = config.get("port", 8766)
        self._language: str = config.get("language", "en")
        self._welcome: str = config.get("welcome", "")
        self._tts_config: dict[str, Any] = config.get("tts", {})

        self._callbacks: VoiceCallbacks | None = None
        self._calls: dict[str, VoiceCall] = {}
        self._ws_connections: dict[str, Any] = {}
        self._runner: Any = None
        self._tts: Any = None
        # Per-call interruption flags
        self._interrupted: dict[str, asyncio.Event] = {}

    @property
    def active_calls(self) -> dict[str, VoiceCall]:
        return self._calls

    async def start(self, callbacks: VoiceCallbacks) -> None:
        import aiohttp.web

        from digitorn.modules.channels.adapters.voice.tts import get_tts_provider
        self._tts = get_tts_provider(self._tts_config)

        self._callbacks = callbacks

        app = aiohttp.web.Application()
        app.router.add_get("/voice", self._handle_websocket)

        self._runner = aiohttp.web.AppRunner(app)
        await self._runner.setup()
        site = aiohttp.web.TCPSite(self._runner, self._host, self._port)
        await site.start()

        tts_name = self._tts_config.get("provider", "browser")
        logger.info("ws_voice_started port=%d tts=%s mode=full-duplex", self._port, tts_name)

    async def stop(self) -> None:
        for call_id in list(self._ws_connections):
            await self.hangup(call_id)
        if self._tts:
            await self._tts.close()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("ws_voice_stopped")

    async def speak(self, call_id: str, text: str) -> bool:
        """Send complete response - used by the pipeline's reply:auto."""
        ws = self._ws_connections.get(call_id)
        if not ws or ws.closed:
            return False

        # Reset interrupt flag for this call
        self._interrupted.setdefault(call_id, asyncio.Event()).clear()

        try:
            from digitorn.modules.channels.adapters.voice.tts import BrowserTTS
            if self._tts and not isinstance(self._tts, BrowserTTS):
                return await self._speak_streaming(call_id, ws, text)
            else:
                await ws.send_json({"type": "speak", "text": text, "call_id": call_id})
                return True
        except Exception as exc:
            logger.warning("ws_voice_speak_error call=%s: %s", call_id, exc)
            return False

    async def _speak_streaming(self, call_id: str, ws: Any, full_text: str) -> bool:
        """Stream TTS audio sentence-by-sentence for low latency.

        Splits the response into sentences, synthesizes each one immediately,
        and streams audio chunks to the client. Supports barge-in: if the
        client sends an "interrupt" message, TTS stops mid-sentence.
        """
        interrupt_event = self._interrupted.setdefault(call_id, asyncio.Event())

        sentences = _split_sentences(full_text)
        if not sentences:
            return True

        await ws.send_json({"type": "speak_start", "call_id": call_id, "text": full_text})

        for sentence in sentences:
            if interrupt_event.is_set():
                logger.debug("voice_barge_in call=%s", call_id)
                break

            if not sentence.strip():
                continue

            try:
                async for chunk in self._tts.synthesize(sentence, self._language):
                    if interrupt_event.is_set():
                        break
                    if ws.closed:
                        return False
                    await ws.send_bytes(chunk)
            except Exception as exc:
                logger.warning("voice_tts_sentence_error: %s", exc)

        await ws.send_json({"type": "speak_end", "call_id": call_id, "interrupted": interrupt_event.is_set()})
        return True

    async def hangup(self, call_id: str) -> None:
        self._interrupted.pop(call_id, None)
        ws = self._ws_connections.pop(call_id, None)
        if ws and not ws.closed:
            try:
                await ws.send_json({"type": "end", "call_id": call_id})
                await ws.close()
            except Exception:
                pass
        self._calls.pop(call_id, None)

    async def _handle_websocket(self, request: Any) -> Any:
        import aiohttp.web

        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)

        call_id = f"call-{uuid.uuid4().hex[:12]}"
        self._calls[call_id] = VoiceCall(
            call_id=call_id, direction="inbound", language=self._language,
        )
        self._ws_connections[call_id] = ws
        self._interrupted[call_id] = asyncio.Event()

        await ws.send_json({"type": "connected", "call_id": call_id})

        if self._welcome:
            await self.speak(call_id, self._welcome)

        logger.info("ws_voice_call_started call_id=%s", call_id)

        try:
            async for msg in ws:
                if msg.type == aiohttp.web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_message(call_id, data)
                    except json.JSONDecodeError:
                        pass
                elif msg.type == aiohttp.web.WSMsgType.ERROR:
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self._interrupted.pop(call_id, None)
            self._ws_connections.pop(call_id, None)
            self._calls.pop(call_id, None)
            logger.info("ws_voice_call_ended call_id=%s", call_id)
            if self._callbacks and self._callbacks.on_call_end:
                await self._callbacks.on_call_end(call_id)

        return ws

    async def _handle_message(self, call_id: str, data: dict) -> None:
        msg_type = data.get("type", "")

        if msg_type == "transcript":
            # User spoke - interrupt any current TTS playback (barge-in)
            interrupt = self._interrupted.get(call_id)
            if interrupt:
                interrupt.set()

            text = data.get("text", "")
            if text and self._callbacks and self._callbacks.on_transcript:
                await self._callbacks.on_transcript(call_id, text, {})

        elif msg_type == "interrupt":
            # Explicit barge-in request
            interrupt = self._interrupted.get(call_id)
            if interrupt:
                interrupt.set()
                logger.debug("voice_explicit_interrupt call=%s", call_id)

        elif msg_type == "start":
            call = self._calls.get(call_id)
            if call:
                call.caller = data.get("caller", "")

        elif msg_type == "end":
            await self.hangup(call_id)


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences for progressive TTS."""
    if not text:
        return []
    parts = _SENTENCE_SPLIT.split(text)
    sentences = []
    for part in parts:
        stripped = part.strip()
        if stripped:
            sentences.append(stripped)
    if not sentences and text.strip():
        sentences = [text.strip()]
    return sentences
