"""Log Output Channel — structured logging delivery.

Writes notifications to the Python logging system. Useful for:
- Development and debugging
- Audit trails
- Integration with log aggregation (ELK, Loki, Datadog, etc.)
- Fallback/shadow channel alongside a primary channel

YAML example::

    channels:
      audit_log:
        type: log
        config:
          logger_name: "digitorn.notifications"
          level: "INFO"
          include_data: true
"""

from __future__ import annotations

import json
import logging
from typing import Any

from digitorn.core.app.channels.base import (
    BaseOutputChannel,
    ChannelCapabilities,
    ChannelPayload,
    DeliveryResult,
    RetryPolicy,
)


class LogChannel(BaseOutputChannel):
    """Write notifications to Python's logging system.

    Zero external dependencies. Works everywhere. Useful for debugging,
    audit trails, and integration with log aggregation systems.
    """

    CHANNEL_ID = "log"
    CHANNEL_NAME = "Structured Log"
    CHANNEL_VERSION = "1.0.0"
    CHANNEL_DESCRIPTION = (
        "Write notifications to Python logging. "
        "Useful for debugging, audit trails, and log aggregation."
    )

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_rich_text=False,
            supports_attachments=False,
            supported_formats=["text", "json"],
        )

    def config_schema(self) -> dict[str, Any]:
        return {
            "required": {},
            "optional": {
                "logger_name": "Python logger name (default: 'digitorn.channels.log')",
                "level": "Log level: DEBUG, INFO, WARNING, ERROR (default: INFO)",
                "include_data": "Include structured_data in log output (default: false)",
                "format": "Output format: 'text' or 'json' (default: text)",
            },
        }

    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(max_retries=0)

    async def deliver(
        self,
        app_id: str,
        payload: ChannelPayload,
        config: dict[str, Any],
    ) -> DeliveryResult:
        """Write notification to the configured logger."""
        logger_name = self.channel_config.get(
            "logger_name", "digitorn.channels.log"
        )
        level_str = self.channel_config.get("level", "INFO").upper()
        include_data = self.channel_config.get("include_data", False)
        fmt = self.channel_config.get("format", "text")

        level = getattr(logging, level_str, logging.INFO)
        log = logging.getLogger(logger_name)

        if fmt == "json":
            entry: dict[str, Any] = {
                "app_id": app_id,
                "channel": "log",
                "title": payload.title,
                "message": payload.message,
                "priority": payload.priority,
                "tags": payload.tags,
                "metadata": payload.metadata,
            }
            if include_data and payload.structured_data:
                entry["data"] = payload.structured_data
            log.log(level, "%s", json.dumps(entry, default=str))
        else:
            text = self.format_text(payload)
            extra = ""
            if include_data and payload.structured_data:
                extra = f" | data={json.dumps(payload.structured_data, default=str)[:500]}"
            log.log(
                level,
                "NOTIFY app=%s priority=%s %s%s",
                app_id, payload.priority, text, extra,
            )

        return DeliveryResult(
            success=True,
            channel_id=self.CHANNEL_ID,
        )
