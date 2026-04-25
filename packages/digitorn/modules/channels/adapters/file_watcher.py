"""File watcher adapter — trigger on new files.

Inbound-only. Polls glob patterns for new files. Ported from
``core/runtime/modes/background.py._watch_loop``.
"""

from __future__ import annotations

import asyncio
import glob as glob_module
import logging
from pathlib import Path
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


class FileWatcherAdapter(BaseChannelAdapter):
    """File watcher trigger — inbound only."""

    CHANNEL_ID = "file_watcher"
    CHANNEL_NAME = "File Watcher"
    CHANNEL_VERSION = "1.0.0"
    CHANNEL_DESCRIPTION = "Trigger on new files matching glob patterns."
    SUPPORTS_INBOUND = True
    SUPPORTS_OUTBOUND = False

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        super().__init__(channel_config=channel_config)
        cfg = channel_config or {}
        self._paths: list[str] = cfg.get("paths", [])
        self._poll_interval: float = cfg.get("poll_interval", 5.0)
        self._message_template: str = cfg.get("message", "")
        self._seen: set[str] = set()

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(supports_inbound=True, supports_outbound=False)

    def adapter_capabilities(self) -> AdapterCapabilities:
        return self.capabilities()

    async def start_listener(self, callback: InboundCallback) -> None:
        if not self._paths:
            logger.error("file_watcher_no_paths")
            return

        # BUG-108: compute the resolved root of each watched pattern
        # up front so the polling loop can reject symlinks whose
        # target escapes that root. Without this, a symlink dropped
        # into a watched dir triggered an activation with the agent's
        # message carrying arbitrary file content (e.g. /etc/passwd),
        # effectively turning file_watcher into a data-exfiltration
        # channel. We resolve ``pattern`` down to its literal prefix
        # (everything before the first glob metachar) and enforce
        # ``resolved.is_relative_to(prefix_resolved)``.
        import re as _re

        def _pattern_prefix(pat: str) -> Path:
            # Split on the first glob metacharacter.
            m = _re.search(r"[\*\?\[]", pat)
            head = pat[:m.start()] if m else pat
            # Strip trailing separator for clean resolution.
            p = Path(head).expanduser()
            if not head.rstrip("/\\"):
                p = Path.cwd()
            return p.resolve()

        self._pattern_roots = [(p, _pattern_prefix(p)) for p in self._paths]

        def _is_safe(match_path: str) -> tuple[bool, str]:
            """Return (ok, reason). Rejects symlinks leaving the root."""
            try:
                target = Path(match_path)
                if target.is_symlink():
                    resolved = target.resolve()
                else:
                    resolved = target.resolve()
                for _pat, root in self._pattern_roots:
                    try:
                        resolved.relative_to(root)
                        return True, ""
                    except ValueError:
                        continue
                return False, f"escapes watched root ({resolved})"
            except (OSError, RuntimeError) as exc:
                return False, f"resolve failed ({exc})"

        # Seed with existing files to avoid triggering on startup
        for pattern in self._paths:
            for match in glob_module.glob(pattern):
                self._seen.add(str(Path(match).resolve()))

        logger.info("file_watcher_started paths=%s seen=%d", self._paths, len(self._seen))

        try:
            while True:
                await asyncio.sleep(self._poll_interval)

                for pattern in self._paths:
                    for match in glob_module.glob(pattern):
                        ok, reason = _is_safe(match)
                        if not ok:
                            logger.warning(
                                "file_watcher_symlink_rejected path=%s reason=%s",
                                match, reason,
                            )
                            self._seen.add(str(Path(match).resolve()))
                            continue
                        resolved = str(Path(match).resolve())
                        if resolved not in self._seen:
                            self._seen.add(resolved)
                            # Cap seen set size
                            if len(self._seen) > 10_000:
                                oldest = next(iter(self._seen))
                                self._seen.discard(oldest)

                            stat = Path(match).stat()
                            event = InboundEvent(
                                event_id=make_event_id(),
                                provider_id="",
                                adapter_type="file_watcher",
                                source=match,
                                message=self._message_template,
                                payload={
                                    "path": match,
                                    "resolved_path": resolved,
                                    "filename": Path(match).name,
                                    "size": stat.st_size,
                                    "modified": stat.st_mtime,
                                },
                            )
                            try:
                                await callback(event)
                            except Exception as exc:
                                logger.error(
                                    "file_watcher_callback_error path=%s error=%s",
                                    match, exc, exc_info=True,
                                )

        except asyncio.CancelledError:
            logger.info("file_watcher_stopped paths=%s", self._paths)

    async def stop_listener(self) -> None:
        pass

    async def deliver(
        self, app_id: str, payload: ChannelPayload, config: dict[str, Any]
    ) -> DeliveryResult:
        return DeliveryResult(
            success=False,
            channel_id=self.CHANNEL_ID,
            error="File watcher adapter is inbound-only.",
        )
