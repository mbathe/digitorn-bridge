"""LLM Notification Channel - the default, always-available channel."""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.app.channels.base import (
    BaseOutputChannel,
    ChannelCapabilities,
    ChannelPayload,
    DeliveryResult,
    RetryPolicy,
)

logger = logging.getLogger(__name__)


class LLMNotificationChannel(BaseOutputChannel):
    """Deliver notifications to the LLM agent via bg_notifications queue."""

    CHANNEL_ID = "llm_notification"
    CHANNEL_NAME = "LLM Agent Notification"
    CHANNEL_VERSION = "2.0.0"
    CHANNEL_DESCRIPTION = (
        "Deliver notifications directly to the LLM agent's conversation. "
        "Buffered in KV when no consumer is connected."
    )

    def __init__(self, job_store: Any = None, **kwargs: Any) -> None:
        super().__init__(channel_config=kwargs.get("channel_config"))
        self._job_store = job_store
        self._context_builders: dict[str, Any] = {}

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_rich_text=False,
            supports_attachments=False,
            supports_threading=False,
            supported_formats=["text", "json"],
        )

    def config_schema(self) -> dict[str, Any]:
        return {"required": {}, "optional": {}}

    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(max_retries=0)


    def register_context_builder(self, app_id: str, cb: Any) -> None:
        """Register a context_builder for direct delivery."""
        self._context_builders[app_id] = cb

    def unregister_context_builder(self, app_id: str) -> None:
        """Remove a context_builder (app undeployed)."""
        self._context_builders.pop(app_id, None)


    async def deliver(
        self,
        app_id: str,
        payload: ChannelPayload,
        config: dict[str, Any],
    ) -> DeliveryResult:
        """Push notification to LLM or buffer it."""
        cb = self._context_builders.get(app_id)

        if cb is not None and hasattr(cb, "push_module_notification"):
            try:
                raw_payload = payload.structured_data or {
                    "message": payload.message,
                    "title": payload.title,
                    "type": payload.metadata.get("trigger_type", "notification"),
                }
                cb.push_module_notification(raw_payload)
                return DeliveryResult(
                    success=True,
                    channel_id=self.CHANNEL_ID,
                )
            except Exception:
                logger.warning(
                    "llm_channel_queue_full app=%s, buffering", app_id,
                )

        if self._job_store is not None:
            raw_payload = payload.structured_data or {
                "message": payload.message,
                "title": payload.title,
                "type": payload.metadata.get("trigger_type", "notification"),
            }
            self._job_store.buffer_notification(app_id, raw_payload)
            logger.debug(
                "llm_channel_buffered app=%s type=%s",
                app_id, payload.metadata.get("trigger_type", "?"),
            )
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                buffered=True,
            )

        return DeliveryResult(
            success=False,
            channel_id=self.CHANNEL_ID,
            error="No active consumer and no job_store for buffering",
        )
