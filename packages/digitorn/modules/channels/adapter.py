"""Base channel adapter - bidirectional I/O contract."""

from __future__ import annotations

import time
import uuid
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from digitorn.core.app.channels.base import (
    BaseOutputChannel,
    ChannelCapabilities,
    ChannelPayload,
    DeliveryResult,
)

@dataclass(frozen=True)
class InboundEvent:
    """Normalized inbound event from any channel adapter."""

    event_id: str
    provider_id: str
    adapter_type: str
    source: str
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    reply_context: dict[str, Any] = field(default_factory=dict)

def make_event_id() -> str:
    """Generate a unique, URL-safe event ID."""
    return f"evt-{uuid.uuid4().hex[:16]}"

@dataclass
class AdapterCapabilities(ChannelCapabilities):
    """Extended capabilities declaring inbound + outbound support."""

    supports_inbound: bool = False
    supports_outbound: bool = True
    supports_streaming: bool = False
    supports_reactions: bool = False

InboundCallback = Callable[[InboundEvent], Awaitable[None]]

class BaseChannelAdapter(BaseOutputChannel):
    """Bidirectional channel adapter."""

    # Override in subclasses
    SUPPORTS_INBOUND: bool = False
    SUPPORTS_OUTBOUND: bool = True

    async def start_listener(self, callback: InboundCallback) -> None:
        """Start listening for inbound events."""

    async def stop_listener(self) -> None:
        """Stop the inbound listener and release resources."""

    async def validate_inbound(
        self,
        raw_body: bytes,
        headers: dict[str, str],
    ) -> tuple[bool, str]:
        """Validate an inbound request BEFORE parsing."""
        return True, ""

    def max_inbound_payload_bytes(self) -> int:
        """Hard limit on inbound payload size in bytes."""
        return 1_048_576  # 1 MB

    async def send_reply(
        self,
        reply_context: dict[str, Any],
        text: str,
        payload: ChannelPayload | None = None,
    ) -> DeliveryResult:
        """Send a reply using the context from an inbound event."""
        effective_payload = payload or ChannelPayload(message=text)
        config = dict(reply_context)
        return await self.deliver(
            app_id=config.pop("_app_id", ""),
            payload=effective_payload,
            config=config,
        )

    def adapter_capabilities(self) -> AdapterCapabilities:
        """Return full adapter capabilities (inbound + outbound)."""
        base = self.capabilities()
        return AdapterCapabilities(
            supports_inbound=self.SUPPORTS_INBOUND,
            supports_outbound=self.SUPPORTS_OUTBOUND,
            supports_rich_text=base.supports_rich_text,
            supports_attachments=base.supports_attachments,
            supports_threading=base.supports_threading,
            supports_batching=base.supports_batching,
            max_message_length=base.max_message_length,
            max_attachment_bytes=base.max_attachment_bytes,
            supported_formats=base.supported_formats,
        )
