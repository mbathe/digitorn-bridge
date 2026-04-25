"""RSS/Atom feed adapter — inbound only.

Polls RSS/Atom feeds for new entries. Deduplicates by entry ID/link.
"""

from __future__ import annotations

import asyncio
import logging
import time
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


class RssAdapter(BaseChannelAdapter):
    """RSS/Atom feed polling trigger — inbound only."""

    CHANNEL_ID = "rss"
    CHANNEL_NAME = "RSS/Atom Feed"
    CHANNEL_VERSION = "1.0.0"
    CHANNEL_DESCRIPTION = "Poll RSS/Atom feeds for new entries."
    SUPPORTS_INBOUND = True
    SUPPORTS_OUTBOUND = False

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        super().__init__(channel_config=channel_config)
        cfg = channel_config or {}
        self._feed_url: str = cfg.get("feed_url", "")
        self._poll_interval: float = cfg.get("poll_interval", 300.0)
        self._message_template: str = cfg.get("message", "")
        self._max_entries: int = cfg.get("max_entries_per_poll", 10)

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(supports_inbound=True, supports_outbound=False)

    def adapter_capabilities(self) -> AdapterCapabilities:
        return self.capabilities()

    async def start_listener(self, callback: InboundCallback) -> None:
        if not self._feed_url:
            logger.error("rss_adapter_no_feed_url")
            return

        try:
            import feedparser
        except ImportError:
            logger.error("rss_adapter_missing_feedparser — pip install feedparser")
            return

        seen_ids: set[str] = set()
        logger.info("rss_adapter_started url=%s", self._feed_url)

        # Seed with existing entries on first poll
        first_poll = True

        try:
            while True:
                try:
                    feed = await asyncio.to_thread(feedparser.parse, self._feed_url)
                    count = 0

                    for entry in feed.entries[:self._max_entries]:
                        entry_id = entry.get("id", entry.get("link", ""))
                        if not entry_id or entry_id in seen_ids:
                            continue
                        seen_ids.add(entry_id)

                        # Cap seen set
                        if len(seen_ids) > 10_000:
                            oldest = next(iter(seen_ids))
                            seen_ids.discard(oldest)

                        # Skip existing entries on first poll
                        if first_poll:
                            continue

                        event = InboundEvent(
                            event_id=make_event_id(),
                            provider_id="",
                            adapter_type="rss",
                            source=self._feed_url,
                            message=self._message_template or entry.get("title", ""),
                            payload={
                                "title": entry.get("title", ""),
                                "link": entry.get("link", ""),
                                "summary": entry.get("summary", "")[:2000],
                                "published": entry.get("published", ""),
                                "author": entry.get("author", ""),
                                "entry_id": entry_id,
                            },
                        )
                        await callback(event)
                        count += 1

                    if count and not first_poll:
                        logger.debug("rss_new_entries url=%s count=%d", self._feed_url, count)

                except Exception as exc:
                    logger.warning("rss_poll_error url=%s error=%s", self._feed_url, exc)

                first_poll = False
                await asyncio.sleep(self._poll_interval)

        except asyncio.CancelledError:
            logger.info("rss_adapter_stopped url=%s", self._feed_url)

    async def stop_listener(self) -> None:
        pass

    async def deliver(
        self, app_id: str, payload: ChannelPayload, config: dict[str, Any]
    ) -> DeliveryResult:
        return DeliveryResult(
            success=False,
            channel_id=self.CHANNEL_ID,
            error="RSS adapter is inbound-only.",
        )
