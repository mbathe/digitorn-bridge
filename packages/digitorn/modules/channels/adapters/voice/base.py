"""Voice backend protocol — abstract transport for voice calls."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class VoiceCall:
    """Active call state."""

    call_id: str
    caller: str = ""
    direction: str = "inbound"  # inbound | outbound
    language: str = "en"
    metadata: dict[str, Any] = field(default_factory=dict)


VoiceTranscriptCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]
"""(call_id, transcript_text, metadata) -> None"""

VoiceCallEndCallback = Callable[[str], Awaitable[None]]
"""(call_id) -> None"""


@dataclass
class VoiceCallbacks:
    on_transcript: VoiceTranscriptCallback | None = None
    on_call_end: VoiceCallEndCallback | None = None


class VoiceBackend(ABC):
    """Abstract voice backend — handles audio transport, STT, and TTS.

    Implementations manage the raw audio pipeline (codecs, VAD, STT, TTS)
    and expose a text-only interface to the voice adapter.
    """

    @abstractmethod
    async def start(self, callbacks: VoiceCallbacks) -> None:
        """Start accepting calls. Non-blocking."""

    @abstractmethod
    async def stop(self) -> None:
        """Shutdown: hang up all calls, close connections, release ports."""

    @abstractmethod
    async def speak(self, call_id: str, text: str) -> bool:
        """Send text to be spoken in the active call. Returns success."""

    @abstractmethod
    async def hangup(self, call_id: str) -> None:
        """End a specific call."""

    @property
    @abstractmethod
    def active_calls(self) -> dict[str, VoiceCall]:
        """Currently active calls by call_id."""


def get_voice_backend(backend_type: str, config: dict[str, Any]) -> VoiceBackend:
    """Factory: resolve backend type to class and instantiate."""
    backends = _load_backends()
    if backend_type not in backends:
        available = sorted(backends.keys())
        raise ValueError(
            f"Unknown voice backend: '{backend_type}'. "
            f"Available: {', '.join(available)}"
        )
    return backends[backend_type](config)


def _load_backends() -> dict[str, type[VoiceBackend]]:
    """Lazy-load backend registry."""
    registry: dict[str, type[VoiceBackend]] = {}

    try:
        from digitorn.modules.channels.adapters.voice.twilio_cr import TwilioCRBackend
        registry["twilio_cr"] = TwilioCRBackend
    except ImportError:
        pass

    try:
        from digitorn.modules.channels.adapters.voice.websocket_backend import WebSocketVoiceBackend
        registry["websocket"] = WebSocketVoiceBackend
    except ImportError:
        pass

    return registry
