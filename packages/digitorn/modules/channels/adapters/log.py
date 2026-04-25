"""Log adapter — outbound only.

Writes messages to Python logging. Useful for debugging, audit trails,
and as a notification sink during development.
"""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.app.channels.base import ChannelPayload, DeliveryResult
from digitorn.modules.channels.adapter import (
    AdapterCapabilities,
    BaseChannelAdapter,
)

logger = logging.getLogger(__name__)


class LogAdapter(BaseChannelAdapter):
    """Log output adapter — outbound only."""

    CHANNEL_ID = "log"
    CHANNEL_NAME = "Log Output"
    CHANNEL_VERSION = "1.0.0"
    CHANNEL_DESCRIPTION = "Write messages to Python logging."
    SUPPORTS_INBOUND = False
    SUPPORTS_OUTBOUND = True

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        super().__init__(channel_config=channel_config)
        cfg = channel_config or {}
        self._level: str = cfg.get("level", "info").upper()
        self._logger_name: str = cfg.get("logger", "digitorn.channels.output")

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_inbound=False,
            supports_outbound=True,
            supported_formats=["text"],
        )

    def adapter_capabilities(self) -> AdapterCapabilities:
        return self.capabilities()

    async def deliver(
        self,
        app_id: str,
        payload: ChannelPayload,
        config: dict[str, Any],
    ) -> DeliveryResult:
        out_logger = logging.getLogger(self._logger_name)
        level = getattr(logging, self._level, logging.INFO)

        text = self.format_text(payload)
        out_logger.log(level, "channel_output app=%s: %s", app_id, text)

        return DeliveryResult(
            success=True,
            channel_id=self.CHANNEL_ID,
        )
