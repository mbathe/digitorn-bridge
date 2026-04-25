"""Tests for the voice adapter, backends, TTS and STT providers."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.modules.channels.adapters.voice import VoiceAdapter
from digitorn.modules.channels.adapters.voice.base import (
    VoiceBackend,
    VoiceCall,
    VoiceCallbacks,
    get_voice_backend,
)
from digitorn.modules.channels.adapters.voice.tts import (
    BrowserTTS,
    EdgeTTS,
    ElevenLabsTTS,
    HttpTTS,
    OpenAITTS,
    get_tts_provider,
    list_tts_providers,
)
from digitorn.modules.channels.adapters.voice.stt import (
    BrowserSTT,
    DeepgramSTT,
    HttpSTT,
    OpenAIWhisperSTT,
    get_stt_provider,
    list_stt_providers,
)


# ── Provider Registries ─────────────────────────────────────────────


class TestProviderRegistries:
    def test_tts_providers_listed(self):
        providers = list_tts_providers()
        assert "edge" in providers
        assert "elevenlabs" in providers
        assert "openai" in providers
        assert "http" in providers
        assert "browser" in providers

    def test_stt_providers_listed(self):
        providers = list_stt_providers()
        assert "deepgram" in providers
        assert "openai" in providers
        assert "http" in providers
        assert "browser" in providers

    def test_tts_factory_returns_correct_types(self):
        assert isinstance(get_tts_provider({"provider": "browser"}), BrowserTTS)
        assert isinstance(get_tts_provider({"provider": "edge"}), EdgeTTS)
        assert isinstance(get_tts_provider({"provider": "elevenlabs"}), ElevenLabsTTS)
        assert isinstance(get_tts_provider({"provider": "openai"}), OpenAITTS)
        assert isinstance(get_tts_provider({"provider": "http"}), HttpTTS)

    def test_stt_factory_returns_correct_types(self):
        assert isinstance(get_stt_provider({"provider": "browser"}), BrowserSTT)
        assert isinstance(get_stt_provider({"provider": "deepgram"}), DeepgramSTT)
        assert isinstance(get_stt_provider({"provider": "openai"}), OpenAIWhisperSTT)
        assert isinstance(get_stt_provider({"provider": "http"}), HttpSTT)

    def test_unknown_tts_falls_back_to_browser(self):
        assert isinstance(get_tts_provider({"provider": "unknown"}), BrowserTTS)

    def test_unknown_stt_falls_back_to_browser(self):
        assert isinstance(get_stt_provider({"provider": "unknown"}), BrowserSTT)

    def test_empty_config_defaults_to_browser(self):
        assert isinstance(get_tts_provider({}), BrowserTTS)
        assert isinstance(get_stt_provider({}), BrowserSTT)


# ── Voice Backend Registry ──────────────────────────────────────────


class TestVoiceBackendRegistry:
    def test_websocket_backend_available(self):
        backend = get_voice_backend("websocket", {"port": 19999})
        assert isinstance(backend, VoiceBackend)

    def test_twilio_cr_backend_available(self):
        backend = get_voice_backend("twilio_cr", {"port": 19998})
        assert isinstance(backend, VoiceBackend)

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown voice backend"):
            get_voice_backend("nonexistent", {})


# ── Voice Adapter ───────────────────────────────────────────────────


class TestVoiceAdapter:
    def test_capabilities(self):
        adapter = VoiceAdapter(channel_config={"backend": "websocket", "backend_config": {}})
        caps = adapter.adapter_capabilities()
        assert caps.supports_inbound is True
        assert caps.supports_outbound is True

    def test_channel_id(self):
        adapter = VoiceAdapter()
        assert adapter.CHANNEL_ID == "voice"

    def test_no_backend_config(self):
        adapter = VoiceAdapter(channel_config={})
        assert adapter._backend_type == ""

    def test_config_parsing(self):
        adapter = VoiceAdapter(channel_config={
            "backend": "websocket",
            "language": "fr",
            "welcome": "Salut",
            "backend_config": {"port": 5555, "tts": {"provider": "edge"}},
        })
        assert adapter._backend_type == "websocket"
        assert adapter._language == "fr"
        assert adapter._welcome == "Salut"
        assert adapter._backend_config["port"] == 5555


# ── Edge TTS ────────────────────────────────────────────────────────


class TestEdgeTTS:
    def test_voice_map(self):
        tts = EdgeTTS({})
        assert "fr" in tts.VOICE_MAP
        assert "en" in tts.VOICE_MAP
        assert tts.VOICE_MAP["fr"] == "fr-FR-DeniseNeural"

    def test_custom_voice(self):
        tts = EdgeTTS({"voice": "fr-FR-HenriNeural"})
        assert tts._voice == "fr-FR-HenriNeural"

    def test_rate_and_pitch(self):
        tts = EdgeTTS({"rate": "+10%", "pitch": "-5Hz"})
        assert tts._rate == "+10%"
        assert tts._pitch == "-5Hz"

    @pytest.mark.asyncio
    async def test_synthesize_produces_audio(self):
        tts = EdgeTTS({"voice": "fr-FR-DeniseNeural"})
        chunks = []
        async for chunk in tts.synthesize("Bonjour", "fr"):
            chunks.append(chunk)
        total = sum(len(c) for c in chunks)
        assert total > 1000, f"Expected audio data, got {total} bytes"


# ── Browser TTS/STT (no-op) ────────────────────────────────────────


class TestBrowserProviders:
    @pytest.mark.asyncio
    async def test_browser_tts_yields_nothing(self):
        tts = BrowserTTS({})
        chunks = [c async for c in tts.synthesize("test", "en")]
        assert chunks == []

    @pytest.mark.asyncio
    async def test_browser_stt_returns_empty(self):
        stt = BrowserSTT({})
        result = await stt.transcribe(b"audio", "en")
        assert result == ""


# ── HTTP Providers (mock server) ────────────────────────────────────


class TestHttpTTS:
    def test_config(self):
        tts = HttpTTS({"url": "http://myserver:5002/tts", "voice": "custom1"})
        assert tts._url == "http://myserver:5002/tts"
        assert tts._voice == "custom1"

    def test_default_url(self):
        tts = HttpTTS({})
        assert "localhost" in tts._url


class TestHttpSTT:
    def test_config(self):
        stt = HttpSTT({"url": "http://myserver:9000/asr"})
        assert stt._url == "http://myserver:9000/asr"


# ── ElevenLabs Config ──────────────────────────────────────────────


class TestElevenLabsConfig:
    def test_defaults(self):
        tts = ElevenLabsTTS({})
        assert tts._model == "eleven_flash_v2_5"
        assert tts._voice_id == "21m00Tcm4TlvDq8ikWAM"

    def test_custom_config(self):
        tts = ElevenLabsTTS({
            "api_key": "sk-test",
            "voice_id": "custom",
            "model": "eleven_turbo_v2",
        })
        assert tts._api_key == "sk-test"
        assert tts._voice_id == "custom"
        assert tts._model == "eleven_turbo_v2"


# ── OpenAI TTS Config ─────────────────────────────────────────────


class TestOpenAITTSConfig:
    def test_defaults(self):
        tts = OpenAITTS({})
        assert tts._model == "tts-1"
        assert tts._voice == "alloy"
        assert "openai.com" in tts._base_url

    def test_custom_base_url(self):
        tts = OpenAITTS({"base_url": "http://localhost:8080/v1", "api_key": "test"})
        assert tts._base_url == "http://localhost:8080/v1"


# ── OpenAI Whisper STT Config ─────────────────────────────────────


class TestOpenAIWhisperConfig:
    def test_defaults(self):
        stt = OpenAIWhisperSTT({})
        assert stt._model == "whisper-1"

    def test_custom_base_url_for_local(self):
        stt = OpenAIWhisperSTT({"base_url": "http://localhost:8080/v1", "api_key": "test"})
        assert stt._base_url == "http://localhost:8080/v1"


# ── Deepgram STT Config ───────────────────────────────────────────


class TestDeepgramConfig:
    def test_defaults(self):
        stt = DeepgramSTT({})
        assert stt._model == "nova-3"

    def test_custom_model(self):
        stt = DeepgramSTT({"model": "nova-2", "api_key": "test"})
        assert stt._model == "nova-2"


# ── WebSocket Backend Integration ──────────────────────────────────


class TestWebSocketBackendIntegration:
    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        from digitorn.modules.channels.adapters.voice.websocket_backend import WebSocketVoiceBackend
        backend = WebSocketVoiceBackend({"port": 18900, "tts": {"provider": "browser"}})
        callbacks = VoiceCallbacks()
        await backend.start(callbacks)
        assert backend._runner is not None
        await backend.stop()
        assert backend._runner is None

    @pytest.mark.asyncio
    async def test_full_call_flow(self):
        """Connect, send transcript, receive response via WebSocket."""
        import aiohttp

        from digitorn.modules.channels.adapters.voice.websocket_backend import WebSocketVoiceBackend
        backend = WebSocketVoiceBackend({"port": 18901, "welcome": "Hello!", "tts": {"provider": "browser"}})

        received_transcripts = []

        async def on_transcript(call_id, text, meta):
            received_transcripts.append(text)
            await backend.speak(call_id, f"Echo: {text}")

        callbacks = VoiceCallbacks(on_transcript=on_transcript)
        await backend.start(callbacks)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect("ws://localhost:18901/voice") as ws:
                    connected = await ws.receive_json()
                    assert connected["type"] == "connected"
                    call_id = connected["call_id"]

                    welcome = await ws.receive_json()
                    assert welcome["type"] == "speak"
                    assert welcome["text"] == "Hello!"

                    await ws.send_json({"type": "transcript", "text": "Bonjour"})
                    response = await asyncio.wait_for(ws.receive_json(), timeout=5)
                    assert response["type"] == "speak"
                    assert "Echo: Bonjour" in response["text"]

            assert received_transcripts == ["Bonjour"]
            assert len(backend.active_calls) == 0  # Call ended after disconnect
        finally:
            await backend.stop()

    @pytest.mark.asyncio
    async def test_edge_tts_audio_streaming(self):
        """WebSocket backend with Edge TTS streams binary audio."""
        import aiohttp

        from digitorn.modules.channels.adapters.voice.websocket_backend import WebSocketVoiceBackend
        backend = WebSocketVoiceBackend({
            "port": 18902,
            "tts": {"provider": "edge", "voice": "fr-FR-DeniseNeural"},
        })

        async def on_transcript(call_id, text, meta):
            await backend.speak(call_id, "Salut")

        callbacks = VoiceCallbacks(on_transcript=on_transcript)
        await backend.start(callbacks)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect("ws://localhost:18902/voice") as ws:
                    connected = await ws.receive_json()
                    call_id = connected["call_id"]

                    await ws.send_json({"type": "transcript", "text": "Bonjour"})

                    speak_start = await asyncio.wait_for(ws.receive_json(), timeout=10)
                    assert speak_start["type"] == "speak_start"

                    audio_bytes = 0
                    while True:
                        msg = await asyncio.wait_for(ws.receive(), timeout=10)
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            audio_bytes += len(msg.data)
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data["type"] == "speak_end":
                                break

                    assert audio_bytes > 1000, f"Expected audio, got {audio_bytes} bytes"
        finally:
            await backend.stop()
