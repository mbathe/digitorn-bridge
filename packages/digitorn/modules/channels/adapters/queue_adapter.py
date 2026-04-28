"""Queue adapter - bridge to QueueModule.

Inbound:  Subscribes to a queue and fires events on new messages.
Outbound: Publishes messages to a queue.
Uses the ServiceBus to call the queue module's actions.
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

logger = logging.getLogger(__name__)


class QueueAdapter(BaseChannelAdapter):
    """Queue bridge adapter - delegates to QueueModule via ServiceBus."""

    CHANNEL_ID = "queue"
    CHANNEL_NAME = "Message Queue"
    CHANNEL_VERSION = "1.0.0"
    CHANNEL_DESCRIPTION = "Bridge to QueueModule for event-driven messaging."
    SUPPORTS_INBOUND = True
    SUPPORTS_OUTBOUND = True

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        super().__init__(channel_config=channel_config)
        cfg = channel_config or {}
        self._queue_name: str = cfg.get("queue", "")
        self._topics: list[str] = cfg.get("topics", [])
        self._poll_interval: float = cfg.get("poll_interval", 5.0)
        self._service_bus: Any = None  # Injected by module

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_inbound=bool(self._queue_name),
            supports_outbound=bool(self._queue_name),
            supported_formats=["json"],
        )

    def adapter_capabilities(self) -> AdapterCapabilities:
        return self.capabilities()

    async def start_listener(self, callback: InboundCallback) -> None:
        if not self._queue_name:
            logger.warning("queue_adapter_no_queue_name")
            return

        logger.info("queue_adapter_started queue=%s topics=%s", self._queue_name, self._topics)

        try:
            while True:
                try:
                    if self._service_bus:
                        result = await self._service_bus.call(
                            "queue", "receive",
                            {"queue": self._queue_name, "batch_size": 10, "timeout": self._poll_interval},
                        )
                        messages = []
                        if hasattr(result, "data") and result.data:
                            messages = result.data.get("messages", [])

                        for msg in messages:
                            topic = msg.get("topic", "")
                            if self._topics and topic not in self._topics:
                                continue

                            event = InboundEvent(
                                event_id=make_event_id(),
                                provider_id="",
                                adapter_type="queue",
                                source=f"{self._queue_name}:{topic}",
                                message=msg.get("body", ""),
                                payload=msg.get("data", msg),
                                metadata={"topic": topic, "queue": self._queue_name},
                            )
                            await callback(event)
                    else:
                        await asyncio.sleep(self._poll_interval)

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("queue_adapter_poll_error: %s", exc)
                    await asyncio.sleep(self._poll_interval)

        except asyncio.CancelledError:
            logger.info("queue_adapter_stopped queue=%s", self._queue_name)

    async def stop_listener(self) -> None:
        pass

    async def deliver(
        self,
        app_id: str,
        payload: ChannelPayload,
        config: dict[str, Any],
    ) -> DeliveryResult:
        if not self._service_bus or not self._queue_name:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="Queue not configured or service bus unavailable.",
            )

        try:
            result = await self._service_bus.call(
                "queue", "publish",
                {
                    "queue": config.get("queue", self._queue_name),
                    "message": payload.message,
                    "data": payload.structured_data,
                    "topic": config.get("topic", ""),
                },
            )
            success = hasattr(result, "success") and result.success
            return DeliveryResult(
                success=success,
                channel_id=self.CHANNEL_ID,
                error=getattr(result, "error", None),
            )
        except Exception as exc:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error=str(exc)[:200],
                retryable=True,
            )
