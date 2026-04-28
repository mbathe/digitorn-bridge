"""Cron adapter - schedule-based trigger.

Inbound-only. Fires events on a cron schedule via croniter.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from croniter import croniter

from digitorn.core.app.channels.base import ChannelPayload, DeliveryResult
from digitorn.modules.channels.adapter import (
    AdapterCapabilities,
    BaseChannelAdapter,
    InboundCallback,
    InboundEvent,
    make_event_id,
)

logger = logging.getLogger(__name__)


class CronAdapter(BaseChannelAdapter):
    """Cron schedule trigger - inbound only."""

    CHANNEL_ID = "cron"
    CHANNEL_NAME = "Cron Schedule"
    CHANNEL_VERSION = "1.0.0"
    CHANNEL_DESCRIPTION = "Fire events on a cron schedule."
    SUPPORTS_INBOUND = True
    SUPPORTS_OUTBOUND = False

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        super().__init__(channel_config=channel_config)
        self._task: asyncio.Task[None] | None = None
        self._schedule: str = (channel_config or {}).get("schedule", "")
        self._message_template: str = (channel_config or {}).get("message", "")

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(supports_inbound=True, supports_outbound=False)

    def adapter_capabilities(self) -> AdapterCapabilities:
        return self.capabilities()

    async def start_listener(self, callback: InboundCallback) -> None:
        if not self._schedule:
            logger.error("cron_adapter_no_schedule")
            return

        try:
            cron = croniter(self._schedule, datetime.now(tz=timezone.utc))
        except (ValueError, KeyError) as exc:
            logger.error(
                "cron_adapter_bad_schedule schedule='%s' error=%s",
                self._schedule, exc,
            )
            return

        logger.info("cron_adapter_started schedule='%s'", self._schedule)

        try:
            while True:
                nxt = cron.get_next(datetime)
                if nxt.tzinfo is None:
                    nxt = nxt.replace(tzinfo=timezone.utc)
                delay = max(1.0, (nxt - datetime.now(tz=timezone.utc)).total_seconds())
                logger.debug("cron_adapter_sleeping secs=%.0f", delay)
                await asyncio.sleep(delay)

                event = InboundEvent(
                    event_id=make_event_id(),
                    provider_id="",  # Set by module
                    adapter_type="cron",
                    source=self._schedule,
                    message=self._message_template,
                    payload={
                        "schedule": self._schedule,
                        "fired_at": datetime.now(tz=timezone.utc).isoformat(),
                    },
                )
                try:
                    await callback(event)
                except Exception as exc:
                    logger.error(
                        "cron_callback_error schedule=%s error=%s",
                        self._schedule, exc, exc_info=True,
                    )

        except asyncio.CancelledError:
            logger.info("cron_adapter_stopped schedule='%s'", self._schedule)

    async def stop_listener(self) -> None:
        pass  # Task cancellation handles cleanup

    async def deliver(
        self, app_id: str, payload: ChannelPayload, config: dict[str, Any]
    ) -> DeliveryResult:
        return DeliveryResult(
            success=False,
            channel_id=self.CHANNEL_ID,
            error="Cron adapter is inbound-only.",
        )
