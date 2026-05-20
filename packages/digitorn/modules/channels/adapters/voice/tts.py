"""Pluggable TTS providers for voice backends."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str, language: str) -> AsyncIterator[bytes]:
        yield b""

    async def close(self) -> None:
        pass

class ElevenLabsTTS(TTSProvider):
    """ElevenLabs streaming TTS."""

    API_URL = "https://api.elevenlabs.io/v1/text-to-speech"

    def __init__(self, config: dict[str, Any]) -> None:
        self._api_key: str = config.get("api_key", "")
        self._voice_id: str = config.get("voice_id", "21m00Tcm4TlvDq8ikWAM")
        self._model: str = config.get("model", "eleven_flash_v2_5")
        self._session: Any = None

    async def _ensure_session(self) -> Any:
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def synthesize(self, text: str, language: str) -> AsyncIterator[bytes]:
        if not self._api_key:
            logger.error("elevenlabs_no_api_key")
            return
        session = await self._ensure_session()
        async with session.post(
            f"{self.API_URL}/{self._voice_id}/stream",
            headers={"xi-api-key": self._api_key, "Content-Type": "application/json"},
            json={"text": text, "model_id": self._model, "output_format": "mp3_44100_128"},
        ) as resp:
            if resp.status != 200:
                logger.warning("elevenlabs_error status=%d: %s", resp.status, (await resp.text())[:200])
                return
            async for chunk in resp.content.iter_chunked(4096):
                if chunk:
                    yield chunk

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

class OpenAITTS(TTSProvider):
    """OpenAI TTS API (or any compatible endpoint)."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._api_key: str = config.get("api_key", "")
        self._base_url: str = config.get("base_url", "https://api.openai.com/v1")
        self._model: str = config.get("model", "tts-1")
        self._voice: str = config.get("voice", "alloy")
        self._format: str = config.get("format", "mp3")
        self._session: Any = None

    async def _ensure_session(self) -> Any:
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def synthesize(self, text: str, language: str) -> AsyncIterator[bytes]:
        if not self._api_key:
            logger.error("openai_tts_no_api_key")
            return
        session = await self._ensure_session()
        async with session.post(
            f"{self._base_url}/audio/speech",
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            json={"model": self._model, "input": text, "voice": self._voice, "response_format": self._format},
        ) as resp:
            if resp.status != 200:
                logger.warning("openai_tts_error status=%d: %s", resp.status, (await resp.text())[:200])
                return
            async for chunk in resp.content.iter_chunked(4096):
                if chunk:
                    yield chunk

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

class EdgeTTS(TTSProvider):
    """Microsoft Edge TTS - free, high quality, 100+ voices."""

    VOICE_MAP = {
        "fr": "fr-FR-DeniseNeural",
        "en": "en-US-AriaNeural",
        "es": "es-ES-ElviraNeural",
        "de": "de-DE-KatjaNeural",
        "pt": "pt-BR-FranciscaNeural",
        "ar": "ar-SA-ZariyahNeural",
        "zh": "zh-CN-XiaoxiaoNeural",
        "ja": "ja-JP-NanamiNeural",
        "ko": "ko-KR-SunHiNeural",
        "it": "it-IT-ElsaNeural",
        "ru": "ru-RU-SvetlanaNeural",
        "hi": "hi-IN-SwaraNeural",
    }

    def __init__(self, config: dict[str, Any]) -> None:
        self._voice: str = config.get("voice", "")
        self._rate: str = config.get("rate", "+0%")
        self._pitch: str = config.get("pitch", "+0Hz")

    async def synthesize(self, text: str, language: str) -> AsyncIterator[bytes]:
        import edge_tts
        voice = self._voice or self.VOICE_MAP.get(language[:2], "en-US-AriaNeural")
        communicate = edge_tts.Communicate(text, voice, rate=self._rate, pitch=self._pitch)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                yield chunk["data"]

class HttpTTS(TTSProvider):
    """Generic HTTP TTS - call any REST endpoint."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._url: str = config.get("url", "http://localhost:5002/api/tts")
        self._voice: str = config.get("voice", "")
        self._format: str = config.get("format", "mp3")
        self._headers: dict[str, str] = config.get("headers", {})
        self._session: Any = None

    async def _ensure_session(self) -> Any:
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def synthesize(self, text: str, language: str) -> AsyncIterator[bytes]:
        session = await self._ensure_session()
        body: dict[str, Any] = {"text": text, "language": language}
        if self._voice:
            body["voice"] = self._voice
        if self._format:
            body["format"] = self._format

        async with session.post(
            self._url, json=body, headers=self._headers,
        ) as resp:
            if resp.status != 200:
                logger.warning("http_tts_error url=%s status=%d", self._url, resp.status)
                return
            async for chunk in resp.content.iter_chunked(4096):
                if chunk:
                    yield chunk

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

class BrowserTTS(TTSProvider):
    """No-op: TTS handled client-side by browser SpeechSynthesis."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        pass

    async def synthesize(self, text: str, language: str) -> AsyncIterator[bytes]:
        return
        yield  # noqa: unreachable

_PROVIDERS: dict[str, type[TTSProvider]] = {
    "elevenlabs": ElevenLabsTTS,
    "openai": OpenAITTS,
    "edge": EdgeTTS,
    "http": HttpTTS,
    "browser": BrowserTTS,
}

def get_tts_provider(config: dict[str, Any]) -> TTSProvider:
    provider = config.get("provider", "browser")
    cls = _PROVIDERS.get(provider)
    if cls is None:
        logger.warning("unknown_tts_provider=%s available=%s", provider, sorted(_PROVIDERS.keys()))
        return BrowserTTS(config)
    return cls(config)

def list_tts_providers() -> list[str]:
    return sorted(_PROVIDERS.keys())
