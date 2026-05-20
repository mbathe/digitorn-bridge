"""Daemon-level sidecar pool: shared subprocess connections across apps."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from digitorn.core.sidecar import SidecarChannel, spawn_sidecar, NotificationHandler

logger = logging.getLogger(__name__)

HEALTH_INTERVAL = 30.0
MAX_RESTART_ATTEMPTS = 3
RESTART_BACKOFF_BASE = 2.0


@dataclass
class _ChannelRef:
    channel: SidecarChannel
    app_ids: set[str] = field(default_factory=set)
    restart_attempts: int = 0


class DaemonSidecarPool:
    """Shared sidecar connections across deployed apps."""

    MAX_CHANNELS = 50

    def __init__(
        self,
        on_event: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._refs: dict[str, _ChannelRef] = {}
        self._lock = asyncio.Lock()
        self._health_task: asyncio.Task[None] | None = None
        self._on_event = on_event
        self._running = False

    async def start(self) -> None:
        """Start the pool and health monitoring."""
        self._running = True
        self._health_task = asyncio.create_task(
            self._health_loop(), name="sidecar-pool-health",
        )
        logger.info("sidecar_pool_started")

    async def stop(self) -> None:
        """Stop all channels and health monitoring."""
        self._running = False

        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            for name, ref in list(self._refs.items()):
                try:
                    await ref.channel.close()
                except Exception:
                    logger.debug("sidecar_pool_stop_error name=%s", name, exc_info=True)
            self._refs.clear()

        logger.info("sidecar_pool_stopped")

    async def acquire(
        self,
        name: str,
        app_id: str,
        command: list[str],
        protocol: str = "jsonrpc",
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        on_notification: NotificationHandler | None = None,
    ) -> SidecarChannel:
        """Get or create a sidecar channel. Increments ref count."""
        async with self._lock:
            ref = self._refs.get(name)

            if ref is not None and ref.channel.status == "connected":
                ref.app_ids.add(app_id)
                logger.debug("sidecar_pool_reuse name=%s app=%s refs=%d", name, app_id, len(ref.app_ids))
                return ref.channel

            if len(self._refs) >= self.MAX_CHANNELS:
                raise RuntimeError(
                    f"Sidecar pool exhausted ({self.MAX_CHANNELS} channels). "
                    "Release unused channels or increase MAX_CHANNELS."
                )

            if ref is not None:
                try:
                    await ref.channel.close()
                except Exception:
                    logger.debug("failed to close stale sidecar channel", exc_info=True)

            channel = await spawn_sidecar(
                name=name,
                command=command,
                protocol=protocol,
                cwd=cwd,
                env=env,
                on_notification=on_notification,
            )

            self._refs[name] = _ChannelRef(
                channel=channel,
                app_ids={app_id},
            )

            logger.info("sidecar_pool_acquired name=%s app=%s protocol=%s", name, app_id, protocol)
            return channel

    async def release(self, name: str, app_id: str) -> None:
        """Release an app's reference to a channel."""
        async with self._lock:
            ref = self._refs.get(name)
            if ref is None:
                return

            ref.app_ids.discard(app_id)
            logger.debug("sidecar_pool_release name=%s app=%s remaining=%d", name, app_id, len(ref.app_ids))

            if not ref.app_ids:
                try:
                    await ref.channel.close()
                except Exception:
                    logger.debug("sidecar_pool_close_error name=%s", name, exc_info=True)
                del self._refs[name]
                logger.info("sidecar_pool_removed name=%s (no more refs)", name)

    def get(self, name: str) -> SidecarChannel | None:
        """Get a channel by name (without acquiring)."""
        ref = self._refs.get(name)
        return ref.channel if ref else None

    def list_channels(self) -> list[dict[str, Any]]:
        """List all channels with their status and ref counts."""
        return [
            {
                "name": name,
                "status": ref.channel.status,
                "protocol": ref.channel.protocol,
                "command": " ".join(ref.channel.command),
                "app_ids": sorted(ref.app_ids),
                "ref_count": len(ref.app_ids),
                "pid": ref.channel._process.pid if ref.channel._process else None,
            }
            for name, ref in self._refs.items()
        ]

    async def _health_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(HEALTH_INTERVAL)
            except asyncio.CancelledError:
                return

            async with self._lock:
                for name, ref in list(self._refs.items()):
                    channel = ref.channel

                    if channel.status == "error" and ref.app_ids:
                        if ref.restart_attempts >= MAX_RESTART_ATTEMPTS:
                            logger.error(
                                "sidecar_health_evict name=%s after=%d attempts",
                                name, ref.restart_attempts,
                            )
                            try:
                                await channel.stop()
                            except Exception as exc:
                                logger.debug("sidecar_pool best-effort block failed: %s", exc)
                            del self._refs[name]
                            continue

                        delay = RESTART_BACKOFF_BASE * (2 ** ref.restart_attempts)
                        ref.restart_attempts += 1
                        logger.warning(
                            "sidecar_health_restart name=%s attempt=%d delay=%.1fs",
                            name, ref.restart_attempts, delay,
                        )
                        await asyncio.sleep(delay)
                        try:
                            await channel.restart()
                            ref.restart_attempts = 0
                            logger.info("sidecar_health_restarted name=%s", name)
                        except Exception as exc:
                            logger.warning("sidecar_health_restart_failed name=%s: %s", name, exc)

                    elif channel.status == "connected":
                        proc = channel._process
                        if proc and proc.returncode is not None:
                            channel.status = "error"
                            logger.warning("sidecar_health_dead name=%s rc=%d", name, proc.returncode)
