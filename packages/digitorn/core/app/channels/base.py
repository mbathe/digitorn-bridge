"""Base classes and data types for the Universal Output Channel System.

This module defines the contract that every output channel must implement.
Whether it's Slack, Gmail, Kafka, Telegram, SMS, a phone call service,
MQTT for IoT, or a simple webhook — they all implement ``BaseOutputChannel``.

Design principles:

1. **Universal payload** — ``ChannelPayload`` is structured, not a raw dict.
   Every channel receives the same shape. The ``message`` field is always
   present as a universal fallback (even SMS can deliver something useful).

2. **Self-describing** — Channels declare their capabilities (rich text,
   attachments, threading, batching) so the system can adapt formatting.

3. **Lifecycle-aware** — ``on_start()`` / ``on_stop()`` for connection
   pooling, OAuth token refresh, etc.

4. **Config-validated** — Each channel declares its required config fields
   (via ``config_schema()``) so YAML validation catches errors at deploy
   time, not at 3 AM when a job fires.

5. **Retry-declarative** — Channels declare their retry policy; the
   registry handles the retry loop uniformly.

6. **Plugin-friendly** — ``pip install digitorn-channel-slack`` registers
   via Python entry points. Zero core changes needed.

Example — minimal channel implementation::

    class SMSChannel(BaseOutputChannel):
        CHANNEL_ID = "sms"
        CHANNEL_NAME = "SMS (Twilio)"
        CHANNEL_VERSION = "1.0.0"
        CHANNEL_DESCRIPTION = "Send SMS via Twilio API"

        def capabilities(self) -> ChannelCapabilities:
            return ChannelCapabilities(
                max_message_length=1600,
                supported_formats=["text"],
            )

        def config_schema(self) -> dict[str, Any]:
            return {
                "required": {"account_sid": "str", "auth_token": "str", "from_number": "str"},
                "optional": {"max_segments": "int"},
            }

        def per_delivery_config_schema(self) -> dict[str, Any]:
            return {
                "required": {"to_number": "str"},
                "optional": {"priority": "str"},
            }

        async def deliver(self, app_id, payload, config):
            to = config["to_number"]
            text = self.format_text(payload)[:1600]
            return DeliveryResult(
                success=True,
                channel_id=self.CHANNEL_ID,
                delivery_id=twilio_message_sid,
            )
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeliveryContext:
    """Context available to channels at delivery time.

    Carries user identity so channels can auto-resolve delivery targets
    (email, phone, chat_id) without the LLM having to specify them manually.

    Think of it like authentication middleware: the system knows who the user
    is (via ``session_id``), and channels use that to look up where to send
    notifications.

    Attributes:
        app_id: The application that owns this delivery.
        session_id: The session that created the job/watcher (identifies the
            user). None for system-level notifications.
        output_config: Explicit per-delivery overrides (from the LLM or YAML).
            These take precedence over auto-resolved values.
    """

    app_id: str
    session_id: str | None = None
    output_config: dict[str, Any] = field(default_factory=dict)


@dataclass
class PayloadAttachment:
    """A file attachment in a notification payload.

    Attributes:
        filename: Original file name (e.g. "report.pdf").
        content_type: MIME type (e.g. "application/pdf").
        data: Raw bytes or base64-encoded string.
        url: Alternative: URL to fetch the attachment from.
        size_bytes: File size (informational, for channels with limits).
    """

    filename: str
    content_type: str = "application/octet-stream"
    data: bytes | str | None = None
    url: str | None = None
    size_bytes: int = 0


@dataclass
class ChannelPayload:
    """Universal notification payload — the same shape for every channel.

    Channel implementations receive this structured object, not a raw dict.
    The ``message`` field is always present as a universal fallback.

    Attributes:
        message: Plain text message (always present — universal fallback).
            Even the simplest channel (SMS, log) can deliver this.
        title: Optional title/subject (email subject, Slack header, etc.).
        rich_message: HTML or Markdown formatted version of the message.
            Channels that support rich text use this; others fall back to
            ``message``.
        structured_data: Machine-readable JSON payload for programmatic
            channels (Kafka, MQTT, webhook). Contains the raw event data.
        attachments: File attachments (PDFs, images, logs, etc.).
        metadata: Source context — always populated by the system:
            - ``job_id``: Scheduled job that triggered this
            - ``app_id``: Owning application
            - ``trigger_type``: "scheduled_job" | "watcher" | "manual"
            - ``timestamp``: ISO 8601 UTC
            - ``run_count``: How many times this job has fired
        thread_id: For channels that support threading (Slack threads,
            email In-Reply-To). None = new thread.
        priority: Notification priority hint: "low", "normal", "high",
            "critical". Channels may use this for routing (e.g. PagerDuty
            severity, email priority headers, Slack channel selection).
        tags: Arbitrary string tags for filtering/routing (e.g. ["deploy",
            "production"]). Useful for Kafka topic routing, email labels.
    """

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
        """Create a ChannelPayload from a raw notification dict.

        Handles the legacy format (flat dict from scheduler/watchers)
        and converts it to the structured format.
        """
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
    """Structured result of a channel delivery attempt.

    Attributes:
        success: True if the notification was delivered (or accepted).
        channel_id: Which channel handled this delivery.
        delivery_id: Channel-specific message ID (Slack message ts,
            email Message-ID, Kafka offset, etc.). Useful for threading,
            deduplication, and audit trails.
        error: Human-readable error message (set on failure).
        retryable: Whether the error is transient (network timeout,
            rate limit) vs permanent (invalid credentials, bad payload).
            The registry uses this to decide whether to retry.
        buffered: True if the notification was buffered for later delivery
            (e.g. LLM channel with no active consumer).
        metadata: Channel-specific response data (HTTP status, Kafka
            partition, delivery receipt, etc.).
        delivered_at: Unix timestamp of successful delivery.
    """

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
    """Declares what a channel can handle.

    Inspectable at compile time — the system can validate that a channel
    supports the features the YAML configuration requests.

    Attributes:
        supports_rich_text: Can render HTML/Markdown (Slack, email).
        supports_attachments: Can deliver file attachments.
        supports_threading: Can group messages in threads (Slack, email).
        supports_batching: Can batch multiple payloads into one delivery.
        max_message_length: Maximum message length (0 = unlimited).
            SMS: 1600, Slack: 40000, Email: unlimited.
        max_attachment_bytes: Maximum total attachment size (0 = unlimited).
        supported_formats: List of supported content formats.
            Common: "text", "html", "markdown", "json", "blocks" (Slack).
    """

    supports_rich_text: bool = False
    supports_attachments: bool = False
    supports_threading: bool = False
    supports_batching: bool = False
    max_message_length: int = 0
    max_attachment_bytes: int = 0
    supported_formats: list[str] = field(default_factory=lambda: ["text"])


@dataclass
class ChannelMeta:
    """Static metadata about a channel type.

    Returned by ``channel_meta()`` and used for discovery, documentation,
    and the ``digitorn channel list`` CLI command.
    """

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
    """Health status of a channel instance.

    Returned by ``health_check()`` for monitoring dashboards and
    the ``digitorn channel status`` CLI command.

    Attributes:
        status: "ok", "degraded", or "down".
        latency_ms: Last delivery latency in milliseconds (0 = unknown).
        last_error: Most recent error message (None = no errors).
        last_success_at: Timestamp of last successful delivery.
        deliveries_total: Total deliveries since startup.
        deliveries_failed: Total failed deliveries since startup.
        details: Channel-specific health details.
    """

    status: str = "ok"
    latency_ms: float = 0.0
    last_error: str | None = None
    last_success_at: float | None = None
    deliveries_total: int = 0
    deliveries_failed: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetryPolicy:
    """Declarative retry policy for a channel.

    The ``ChannelRegistry`` handles the retry loop — individual channels
    do NOT implement retry logic. They just declare the policy.

    Attributes:
        max_retries: Maximum retry attempts (0 = no retries).
        backoff_base: Base delay between retries in seconds (exponential).
        backoff_max: Maximum delay between retries in seconds.
        backoff_multiplier: Multiplier for exponential backoff.
    """

    max_retries: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    backoff_multiplier: float = 2.0


class BaseOutputChannel(ABC):
    """Abstract base class for all output channels.

    Every output channel — built-in or plugin — must subclass this.

    **Required class attributes** (set on the subclass):

    - ``CHANNEL_ID`` — Unique identifier (e.g. "slack", "gmail", "webhook").
      Used as the ``type:`` in YAML and for registry lookup.
    - ``CHANNEL_NAME`` — Human-readable name for UI/docs.
    - ``CHANNEL_VERSION`` — Semver string.

    **Required methods** (must override):

    - ``deliver()`` — The core delivery method.

    **Optional methods** (override for richer behavior):

    - ``capabilities()`` — Declare what this channel supports.
    - ``config_schema()`` / ``per_delivery_config_schema()`` — Config docs.
    - ``validate_config()`` — Deep config validation (test connectivity).
    - ``on_start()`` / ``on_stop()`` — Lifecycle (connection pools, etc.).
    - ``health_check()`` — Runtime health monitoring.
    - ``retry_policy()`` — Custom retry behavior.
    - ``format_text()`` / ``format_rich()`` — Payload formatting helpers.

    **Instance lifecycle**:

    1. ``__init__(channel_config)`` — Created with resolved global config
    2. ``validate_config()`` — Deep validation (optional)
    3. ``on_start()`` — Initialize connections
    4. ``deliver()`` (N times) — Deliver notifications
    5. ``on_stop()`` — Cleanup connections

    Example::

        class SlackChannel(BaseOutputChannel):
            CHANNEL_ID = "slack"
            CHANNEL_NAME = "Slack"
            CHANNEL_VERSION = "1.0.0"
            CHANNEL_DESCRIPTION = "Deliver notifications via Slack webhooks"

            def capabilities(self) -> ChannelCapabilities:
                return ChannelCapabilities(
                    supports_rich_text=True,
                    supports_threading=True,
                    max_message_length=40000,
                    supported_formats=["text", "markdown", "blocks"],
                )

            def config_schema(self) -> dict[str, Any]:
                return {
                    "required": {
                        "webhook_url": "Slack incoming webhook URL",
                    },
                    "optional": {
                        "default_channel": "Default channel name (e.g. #alerts)",
                        "username": "Bot display name",
                        "icon_emoji": "Bot icon emoji (e.g. :robot_face:)",
                    },
                }

            def per_delivery_config_schema(self) -> dict[str, Any]:
                return {
                    "optional": {
                        "channel": "Override target channel",
                        "thread_ts": "Reply to specific thread",
                    },
                }

            async def deliver(self, app_id, payload, config):
                webhook = self.channel_config["webhook_url"]
                text = self.format_text(payload)
                return DeliveryResult(
                    success=True,
                    channel_id=self.CHANNEL_ID,
                    delivery_id=response_ts,
                )
    """

    CHANNEL_ID: str = ""
    CHANNEL_NAME: str = ""
    CHANNEL_VERSION: str = "0.0.0"
    CHANNEL_DESCRIPTION: str = ""

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        """Initialize with the resolved global config from YAML.

        Args:
            channel_config: The ``config:`` block from the YAML channel
                definition, with all ``{{variables}}`` resolved.
        """
        self.channel_config: dict[str, Any] = channel_config or {}
        self._health = ChannelHealth()


    def channel_meta(self) -> ChannelMeta:
        """Return static metadata about this channel type.

        Used for discovery, documentation, and CLI display.
        """
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
        """Declare this channel's capabilities.

        Override to declare rich text support, attachments, threading, etc.
        Default: text-only, no special capabilities.
        """
        return ChannelCapabilities()


    def config_schema(self) -> dict[str, Any]:
        """Describe the global config fields this channel requires.

        Returns a dict with ``"required"`` and ``"optional"`` keys,
        each mapping field names to descriptions::

            {
                "required": {"webhook_url": "Slack incoming webhook URL"},
                "optional": {"username": "Bot display name"},
            }

        Used by the compiler for YAML validation and by
        ``digitorn channel schema <type>`` for documentation.
        """
        return {"required": {}, "optional": {}}

    def per_delivery_config_schema(self) -> dict[str, Any]:
        """Describe per-delivery config overrides (from ``output_config``).

        These are the fields that can be set per-job or per-watcher
        to customize delivery (e.g. target Slack channel, email recipients).

        Same format as ``config_schema()``.
        """
        return {"required": {}, "optional": {}}

    async def validate_config(self) -> list[str]:
        """Deep-validate the channel config beyond schema checks.

        Called at deploy time after basic schema validation.
        Use this to test connectivity (is the webhook URL reachable?),
        validate credentials (is the API key valid?), etc.

        Returns:
            List of error strings. Empty list = config is valid.
        """
        errors: list[str] = []
        schema = self.config_schema()
        for field_name in schema.get("required", {}):
            if field_name not in self.channel_config:
                errors.append(
                    f"Missing required config field: '{field_name}'"
                )
        return errors


    async def on_start(self) -> None:
        """Initialize connections, pools, OAuth tokens, etc.

        Called once when the channel instance is created (at app deploy).
        Override for channels that need persistent connections.
        """

    async def on_stop(self) -> None:
        """Close connections, flush queues, release resources.

        Called when the app is undeployed or the daemon shuts down.
        """


    async def health_check(self) -> ChannelHealth:
        """Return the current health status of this channel instance.

        Override to add connectivity checks, queue depth monitoring, etc.
        Default: returns the internal health tracker.
        """
        return self._health


    def retry_policy(self) -> RetryPolicy:
        """Declare the retry policy for this channel.

        The ``ChannelRegistry`` handles the retry loop based on this.
        Override to customize retry behavior per channel type.

        Default: 3 retries, exponential backoff 1s → 60s.
        """
        return RetryPolicy()


    async def resolve_recipient(
        self,
        context: DeliveryContext,
    ) -> dict[str, Any]:
        """Auto-resolve user-specific delivery targets from session context.

        Called **before** ``deliver()``. Returns the final per-delivery config
        by merging auto-resolved fields with explicit ``output_config``.

        Override in custom channels to fetch user contact info from a database,
        API, or other data source. The ``context.session_id`` identifies the
        user; ``self.channel_config`` provides connection details configured
        in YAML.

        The default implementation returns ``context.output_config`` unchanged
        (full backward compatibility).

        Example override (SMS channel fetching phone from DB)::

            async def resolve_recipient(self, context):
                if not context.session_id:
                    return context.output_config
                phone = await self._lookup_phone(context.session_id)
                resolved = {"to_number": phone}
                resolved.update(context.output_config)
                return resolved

        Args:
            context: Delivery context with app_id, session_id, and any
                explicit output_config.

        Returns:
            Final per-delivery config dict passed to ``deliver()``.
        """
        return context.output_config


    @abstractmethod
    async def deliver(
        self,
        app_id: str,
        payload: ChannelPayload,
        config: dict[str, Any],
    ) -> DeliveryResult:
        """Deliver a notification to this channel.

        This is the only method that MUST be implemented.

        Args:
            app_id: The app that owns the triggering job/watcher.
            payload: Structured notification payload (never a raw dict).
            config: Per-delivery config overrides (from ``output_config``
                on the ScheduledJob or watcher).

        Returns:
            DeliveryResult indicating success/failure/buffered.
        """
        ...


    def format_text(self, payload: ChannelPayload) -> str:
        """Format a payload as plain text.

        Convenience method for channels that only support text
        (SMS, log, simple webhooks).

        Override for custom text formatting.
        """
        parts: list[str] = []
        if payload.title:
            parts.append(f"[{payload.title}]")
        parts.append(payload.message)
        if payload.tags:
            parts.append(f"Tags: {', '.join(payload.tags)}")
        return " ".join(parts)

    def format_rich(self, payload: ChannelPayload) -> str:
        """Format a payload as rich text (HTML/Markdown).

        Uses ``payload.rich_message`` if available, falls back to
        ``payload.message``.

        Override for channel-specific rich formatting (Slack blocks,
        email HTML templates, etc.).
        """
        if payload.rich_message:
            return payload.rich_message
        parts: list[str] = []
        if payload.title:
            parts.append(f"<b>{payload.title}</b>")
        parts.append(payload.message)
        return "<br>".join(parts)


    def _record_success(self, latency_ms: float = 0.0) -> None:
        """Record a successful delivery (called by registry)."""
        self._health.deliveries_total += 1
        self._health.last_success_at = time.time()
        self._health.latency_ms = latency_ms
        if self._health.status == "degraded":
            self._health.status = "ok"

    def _record_failure(self, error: str) -> None:
        """Record a failed delivery (called by registry)."""
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
