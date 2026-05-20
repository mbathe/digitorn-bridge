"""Base classes and data types for the Universal Output Channel System."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeliveryContext:
    """Context available to channels at delivery time."""

    app_id: str
    session_id: str | None = None
    output_config: dict[str, Any] = field(default_factory=dict)


@dataclass
class PayloadAttachment:
    """A file attachment in a notification payload."""

    filename: str
    content_type: str = "application/octet-stream"
    data: bytes | str | None = None
    url: str | None = None
    size_bytes: int = 0


@dataclass
class ChannelPayload:
    """Universal notification payload - the same shape for every channel."""

    message: str
    title: str = ""
    rich_message: str = ""
    structured_data: dict[str, Any] = field(default_factory=dict)
    attachments: list[PayloadAttachment] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    thread_id: str | None = None
    priority: str = "normal"
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChannelPayload:
        """Create a ChannelPayload from a raw notification dict."""
        if "message" in data:
            return cls(
                message=data["message"],
                title=data.get("title", data.get("label", "")),
                rich_message=data.get("rich_message", ""),
                structured_data=data.get("structured_data", data),
                metadata=data.get("metadata", {}),
                thread_id=data.get("thread_id"),
                priority=data.get("priority", "normal"),
                tags=data.get("tags", []),
            )

        msg_parts = []
        label = data.get("label", "")
        if label:
            msg_parts.append(f"[{label}]")

        notif_type = data.get("type", "notification")
        action_type = data.get("action_type", "")

        if "error" in data:
            msg_parts.append(f"Error: {data['error']}")
        elif "prompt" in data:
            msg_parts.append(data["prompt"])
        elif "result" in data:
            result = data["result"]
            if isinstance(result, str):
                msg_parts.append(result)
            elif isinstance(result, dict):
                msg_parts.append(str(result))
            else:
                msg_parts.append(str(result))
        else:
            msg_parts.append(f"{notif_type} fired")

        return cls(
            message=" ".join(msg_parts) if msg_parts else "Notification",
            title=label or f"{notif_type}:{action_type}",
            structured_data=data,
            metadata={
                "job_id": data.get("job_id", ""),
                "trigger_type": notif_type,
                "run_count": data.get("run_count", 0),
            },
            priority=data.get("priority", "normal"),
        )


@dataclass
class DeliveryResult:
    """Structured result of a channel delivery attempt."""

    success: bool
    channel_id: str = ""
    delivery_id: str | None = None
    error: str | None = None
    retryable: bool = False
    buffered: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    delivered_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "channel_id": self.channel_id,
            "delivery_id": self.delivery_id,
            "error": self.error,
            "retryable": self.retryable,
            "buffered": self.buffered,
            "metadata": self.metadata,
            "delivered_at": self.delivered_at,
        }


@dataclass
class ChannelCapabilities:
    """Declares what a channel can handle."""

    supports_rich_text: bool = False
    supports_attachments: bool = False
    supports_threading: bool = False
    supports_batching: bool = False
    max_message_length: int = 0
    max_attachment_bytes: int = 0
    supported_formats: list[str] = field(default_factory=lambda: ["text"])


@dataclass
class ChannelMeta:
    """Static metadata about a channel type."""

    channel_id: str
    name: str
    version: str
    description: str = ""
    author: str = ""
    capabilities: ChannelCapabilities = field(default_factory=ChannelCapabilities)
    config_schema: dict[str, Any] = field(default_factory=dict)
    per_delivery_config_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelHealth:
    """Health status of a channel instance."""

    status: str = "ok"
    latency_ms: float = 0.0
    last_error: str | None = None
    last_success_at: float | None = None
    deliveries_total: int = 0
    deliveries_failed: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetryPolicy:
    """Declarative retry policy for a channel."""

    max_retries: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    backoff_multiplier: float = 2.0


class BaseOutputChannel(ABC):
    """Abstract base class for all output channels."""

    CHANNEL_ID: str = ""
    CHANNEL_NAME: str = ""
    CHANNEL_VERSION: str = "0.0.0"
    CHANNEL_DESCRIPTION: str = ""

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        """Initialize with the resolved global config from YAML."""
        self.channel_config: dict[str, Any] = channel_config or {}
        self._health = ChannelHealth()


    def channel_meta(self) -> ChannelMeta:
        """Return static metadata about this channel type."""
        return ChannelMeta(
            channel_id=self.CHANNEL_ID,
            name=self.CHANNEL_NAME,
            version=self.CHANNEL_VERSION,
            description=self.CHANNEL_DESCRIPTION,
            capabilities=self.capabilities(),
            config_schema=self.config_schema(),
            per_delivery_config_schema=self.per_delivery_config_schema(),
        )

    def capabilities(self) -> ChannelCapabilities:
        """Declare this channel's capabilities."""
        return ChannelCapabilities()


    def config_schema(self) -> dict[str, Any]:
        """Describe the global config fields this channel requires."""
        return {"required": {}, "optional": {}}

    def per_delivery_config_schema(self) -> dict[str, Any]:
        """Describe per-delivery config overrides (from `output_config`)."""
        return {"required": {}, "optional": {}}

    async def validate_config(self) -> list[str]:
        """Deep-validate the channel config beyond schema checks."""
        errors: list[str] = []
        schema = self.config_schema()
        for field_name in schema.get("required", {}):
            if field_name not in self.channel_config:
                errors.append(
                    f"Missing required config field: '{field_name}'"
                )
        return errors


    async def on_start(self) -> None:
        """Initialize connections, pools, OAuth tokens, etc."""

    async def on_stop(self) -> None:
        """Close connections, flush queues, release resources."""


    async def health_check(self) -> ChannelHealth:
        """Return the current health status of this channel instance."""
        return self._health


    def retry_policy(self) -> RetryPolicy:
        """Declare the retry policy for this channel."""
        return RetryPolicy()


    async def resolve_recipient(
        self,
        context: DeliveryContext,
    ) -> dict[str, Any]:
        """Auto-resolve user-specific delivery targets from session context."""
        return context.output_config


    @abstractmethod
    async def deliver(
        self,
        app_id: str,
        payload: ChannelPayload,
        config: dict[str, Any],
    ) -> DeliveryResult:
        """Deliver a notification to this channel."""
        ...


    def format_text(self, payload: ChannelPayload) -> str:
        """Format a payload as plain text."""
        parts: list[str] = []
        if payload.title:
            parts.append(f"[{payload.title}]")
        parts.append(payload.message)
        if payload.tags:
            parts.append(f"Tags: {', '.join(payload.tags)}")
        return " ".join(parts)

    def format_rich(self, payload: ChannelPayload) -> str:
        """Format a payload as rich text (HTML/Markdown)."""
        if payload.rich_message:
            return payload.rich_message
        parts: list[str] = []
        if payload.title:
            parts.append(f"<b>{payload.title}</b>")
        parts.append(payload.message)
        return "<br>".join(parts)


    def _record_success(self, latency_ms: float = 0.0) -> None:
        self._health.deliveries_total += 1
        self._health.last_success_at = time.time()
        self._health.latency_ms = latency_ms
        if self._health.status == "degraded":
            self._health.status = "ok"

    def _record_failure(self, error: str) -> None:
        self._health.deliveries_total += 1
        self._health.deliveries_failed += 1
        self._health.last_error = error
        fail_rate = (
            self._health.deliveries_failed / self._health.deliveries_total
        )
        self._health.status = "degraded" if fail_rate > 0.1 else "ok"

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"id={self.CHANNEL_ID!r} v={self.CHANNEL_VERSION}>"
        )
