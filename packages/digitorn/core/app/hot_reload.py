"""BundleHotReloader - watches an app's prompts/skills/assets dirs"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


# Folders we watch for mtime changes - everything else requires a
# manual redeploy because it might alter the app structure.
_WATCHED_SUBDIRS: tuple[str, ...] = ("prompts", "skills", "assets")

# Poll interval in seconds
_POLL_INTERVAL = 1.0

# Debounce - how long to wait after the last change before firing
_DEBOUNCE_SECONDS = 0.5


class BundleHotReloader:
    """Watch a single app's bundle directory and trigger a"""

    def __init__(
        self,
        *,
        app_id: str,
        bundle_dir: Path,
        on_change: Callable[[], Any],
    ) -> None:
        self._app_id = app_id
        self._bundle_dir = Path(bundle_dir).resolve()
        self._on_change = on_change
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._snapshot: dict[str, float] = {}

    async def start(self) -> None:
        if self._task is not None:
            return
        if not self._bundle_dir.is_dir():
            logger.warning(
                "hot_reload: bundle dir %s missing for app=%s, skipping",
                self._bundle_dir, self._app_id,
            )
            return
        self._stopping.clear()
        self._snapshot = self._take_snapshot()
        self._task = asyncio.create_task(
            self._poll_loop(),
            name=f"hot-reload-{self._app_id}",
        )
        logger.info(
            "hot_reload_started app=%s dir=%s files=%d",
            self._app_id, self._bundle_dir, len(self._snapshot),
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        logger.info("hot_reload_stopped app=%s", self._app_id)

    async def _poll_loop(self) -> None:
        last_change_at: float = 0.0
        try:
            while not self._stopping.is_set():
                await asyncio.sleep(_POLL_INTERVAL)
                try:
                    current = self._take_snapshot()
                except Exception as exc:
                    logger.debug(
                        "hot_reload: snapshot failed for %s: %s",
                        self._app_id, exc,
                    )
                    continue

                if current != self._snapshot:
                    changed = sorted(
                        set(current.keys()) | set(self._snapshot.keys())
                    )
                    n_changed = sum(
                        1 for k in changed
                        if current.get(k) != self._snapshot.get(k)
                    )
                    self._snapshot = current
                    last_change_at = time.time()
                    logger.debug(
                        "hot_reload_change_detected app=%s files_changed=%d",
                        self._app_id, n_changed,
                    )

                # Debounce: fire only when enough quiet time has
                # elapsed since the last detected change.
                if last_change_at and (
                    time.time() - last_change_at >= _DEBOUNCE_SECONDS
                ):
                    last_change_at = 0.0
                    await self._fire_reload()
        except asyncio.CancelledError:
            pass

    def _take_snapshot(self) -> dict[str, float]:
        """Record mtimes of every file under the watched subdirs."""
        out: dict[str, float] = {}
        for sub in _WATCHED_SUBDIRS:
            base = self._bundle_dir / sub
            if not base.is_dir():
                continue
            try:
                for path in base.rglob("*"):
                    if not path.is_file():
                        continue
                    rel = path.relative_to(self._bundle_dir).as_posix()
                    try:
                        out[rel] = path.stat().st_mtime
                    except OSError:
                        continue
            except OSError:
                continue
        return out

    async def _fire_reload(self) -> None:
        logger.info("hot_reload_firing app=%s", self._app_id)
        try:
            result = self._on_change()
            if asyncio.iscoroutine(result):
                await result
            logger.info("hot_reload_complete app=%s", self._app_id)
        except Exception as exc:
            logger.warning(
                "hot_reload_failed app=%s: %s",
                self._app_id, exc, exc_info=True,
            )
