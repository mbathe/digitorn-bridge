"""Pluggable STT providers for voice backends.

Each provider transcribes audio bytes to text. Adding a new provider:
1. Subclass STTProvider
2. Register in get_stt_provider()
3. Configure in YAML: backend_config.stt.provider: your_provider

For local/self-hosted models (Whisper, faster-whisper, Vosk, etc.),
use the 'http' provider pointing at any endpoint that accepts audio
and returns text.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class STTProvider(ABC):
    @abstractmethod
    async def transcribe(self, audio: bytes, language: str) -> str:
        """Transcribe audio bytes to text. Returns empty string on failure."""
        return ""

    async def close(self) -> None:
        pass


class DeepgramSTT(STTProvider):
    """Deepgram Nova STT via REST API."""

    API_URL = "https://api.deepgram.com/v1/listen"

    def __init__(self, config: dict[str, Any]) -> None:
        self._api_key: str = config.get("api_key", "")
        self._model: str = config.get("model", "nova-3")
        self._session: Any = None

    async def _ensure_session(self) -> Any:
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def transcribe(self, audio: bytes, language: str) -> str:
        if not self._api_key:
            logger.error("deepgram_no_api_key")
            return ""
        session = await self._ensure_session()
        params = {"model": self._model, "language": language[:2]}
        async with session.post(
            self.API_URL,
            params=params,
            headers={"Authorization": f"Token {self._api_key}", "Content-Type": "audio/wav"},
            data=audio,
        ) as resp:
            if resp.status != 200:
                logger.warning("deepgram_error status=%d", resp.status)
                return ""
            data = await resp.json()
            try:
                return data["results"]["channels"][0]["alternatives"][0]["transcript"]
            except (KeyError, IndexError):
                return ""

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class OpenAIWhisperSTT(STTProvider):
    """OpenAI Whisper API (or any compatible endpoint)."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._api_key: str = config.get("api_key", "")
        self._base_url: str = config.get("base_url", "https://api.openai.com/v1")
        self._model: str = config.get("model", "whisper-1")
        self._session: Any = None

    async def _ensure_session(self) -> Any:
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def transcribe(self, audio: bytes, language: str) -> str:
        if not self._api_key:
            logger.error("openai_whisper_no_api_key")
            return ""
        import aiohttp
        session = await self._ensure_session()
        data = aiohttp.FormData()
        data.add_field("file", audio, filename="audio.wav", content_type="audio/wav")
        data.add_field("model", self._model)
        data.add_field("language", language[:2])

        async with session.post(
            f"{self._base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            data=data,
        ) as resp:
            if resp.status != 200:
                logger.warning("openai_whisper_error status=%d", resp.status)
                return ""
            result = await resp.json()
            return result.get("text", "")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class HttpSTT(STTProvider):
    """Generic HTTP STT — call any REST endpoint.

    Works with local models (faster-whisper, Vosk, Kaldi, etc.)
    via a simple HTTP server.

    Expected endpoint: POST audio bytes → {"text": "transcription"}
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._url: str = config.get("url", "http://localhost:9000/asr")
        self._headers: dict[str, str] = config.get("headers", {})
        self._session: Any = None

    async def _ensure_session(self) -> Any:
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    async def transcribe(self, audio: bytes, language: str) -> str:
        session = await self._ensure_session()
        async with session.post(
            self._url,
            params={"language": language[:2]},
            headers={**self._headers, "Content-Type": "audio/wav"},
            data=audio,
        ) as resp:
            if resp.status != 200:
                logger.warning("http_stt_error url=%s status=%d", self._url, resp.status)
                return ""
            data = await resp.json()
            return data.get("text", "")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class BrowserSTT(STTProvider):
    """No-op: STT handled client-side by browser Web Speech API."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        pass

    async def transcribe(self, audio: bytes, language: str) -> str:
        return ""


_PROVIDERS: dict[str, type[STTProvider]] = {
    "deepgram": DeepgramSTT,
    "openai": OpenAIWhisperSTT,
    "http": HttpSTT,
    "browser": BrowserSTT,
}


def get_stt_provider(config: dict[str, Any]) -> STTProvider:
    provider = config.get("provider", "browser")
    cls = _PROVIDERS.get(provider)
    if cls is None:
        logger.warning("unknown_stt_provider=%s available=%s", provider, sorted(_PROVIDERS.keys()))
        return BrowserSTT(config)
    return cls(config)


def list_stt_providers() -> list[str]:
    return sorted(_PROVIDERS.keys())
