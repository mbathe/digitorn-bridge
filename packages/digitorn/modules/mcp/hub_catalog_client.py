"""In-process cache of the Hub's curated MCP catalog.

The Hub is the source of truth for which MCP servers are officially
supported (see ``packages/hub/src/digitorn_hub/routers/mcp_featured.py``).
This module fetches that list once at daemon startup, refreshes every
5 minutes in the background, and exposes a **synchronous** lookup
helper so existing call sites (install pipeline, route handlers) keep
working without becoming async.

Failure modes:

* Hub unreachable on first fetch → cache stays empty; ``get`` returns
  ``None``. The legacy ``catalog.CATALOG`` dict is used as a last-resort
  fallback by callers of ``get_catalog_entry``.
* Hub goes down mid-life → cache keeps the last known good list.
  Refresh attempts log a warning but never wipe the cache.

The fallback design matches the App Store / iPhone analogy: each
per-user daemon is an installed app and the Hub is the canonical
distribution point. Daemons can stay usable for hours during a Hub
outage as long as they cached the catalog at some point.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .catalog import CatalogEntry

logger = logging.getLogger(__name__)


_DEFAULT_TTL_S = 300.0  # 5 min — matches the daemon registry proxy cache
_FETCH_TIMEOUT_S = 10.0
_REFRESH_INTERVAL_S = 300.0


def _row_to_entry(row: dict[str, Any]) -> CatalogEntry | None:
    """Translate a Hub row (JSON shape) to a ``CatalogEntry`` dataclass.

    Returns ``None`` if the row is malformed. Tuple-typed dataclass
    fields are reconstructed from lists; missing optional fields
    default to the dataclass defaults.
    """
    sid = row.get("server_id")
    name = row.get("display_name")
    if not sid or not name:
        return None
    try:
        return CatalogEntry(
            server_id=sid,
            display_name=name,
            description=row.get("description") or "",
            transport=row.get("transport") or "stdio",
            command=row.get("command") or "",
            args=tuple(row.get("args") or ()),
            runtime=row.get("runtime") or "npm",
            package=row.get("package") or "",
            env_mapping=dict(row.get("env_mapping") or {}),
            key_descriptions=dict(row.get("key_descriptions") or {}),
            default_env=dict(row.get("default_env") or {}),
            oauth_provider=row.get("oauth_provider"),
            oauth_env_token_var=row.get("oauth_env_token_var") or "",
            oauth_scopes=tuple(row.get("oauth_scopes") or ()),
            oauth_style=row.get("oauth_style") or "",
            oauth_keyfile_env=row.get("oauth_keyfile_env") or "",
            oauth_credentials_env=row.get("oauth_credentials_env") or "",
            oauth_credentials_filename=row.get("oauth_credentials_filename") or "",
            binary_name=row.get("binary_name") or "",
            smithery_slug=row.get("smithery_slug") or "",
            timeout=float(row.get("timeout") or 30.0),
            icon=row.get("icon") or "",
            category=row.get("category") or "",
            # App Store classification (migration 0009)
            personal_keys=tuple(row.get("personal_keys") or ()),
            digitorn_provided=dict(row.get("digitorn_provided") or {}),
            hosted_url=row.get("hosted_url") or "",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("hub_catalog_row_invalid id=%s: %s", sid, exc)
        return None


class HubCatalogCache:
    """Async fetcher + sync reader for the Hub-curated MCP catalog.

    Designed to be plugged into the daemon lifespan: ``await cache.refresh()``
    once at boot, ``asyncio.create_task(cache.run_refresh_loop())`` for
    the background heartbeat. Call sites read via ``get_sync`` /
    ``all_sync`` — those never await, so existing sync code paths
    (notably ``mcp_store.install_server``) stay unchanged.
    """

    def __init__(self, hub_url: str, ttl: float = _DEFAULT_TTL_S) -> None:
        self._hub_url = hub_url.rstrip("/") if hub_url else ""
        self._ttl = ttl
        self._cache: dict[str, CatalogEntry] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()

    @property
    def hub_url(self) -> str:
        return self._hub_url

    @property
    def enabled(self) -> bool:
        return bool(self._hub_url)

    @property
    def fresh(self) -> bool:
        if self._fetched_at == 0.0:
            return False
        return (time.monotonic() - self._fetched_at) < self._ttl

    @property
    def size(self) -> int:
        return len(self._cache)

    async def refresh(self) -> bool:
        """Fetch the full featured list and replace the in-memory cache.

        Returns ``True`` on success, ``False`` on any failure (cache
        is **not** wiped on failure — last-known-good is preserved).
        """
        if not self.enabled:
            return False
        async with self._lock:
            url = f"{self._hub_url}/api/v1/mcp/featured?limit=500"
            try:
                async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S) as client:
                    r = await client.get(url)
                    r.raise_for_status()
                    payload = r.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning("hub_catalog_fetch_failed url=%s: %s", url, exc)
                return False

            rows: list[dict[str, Any]] = payload.get("entries") or []
            new_cache: dict[str, CatalogEntry] = {}
            for row in rows:
                entry = _row_to_entry(row)
                if entry is not None:
                    new_cache[entry.server_id] = entry

            if not new_cache:
                logger.warning("hub_catalog_empty_response url=%s", url)
                return False

            self._cache = new_cache
            self._fetched_at = time.monotonic()
            logger.info("hub_catalog_refreshed count=%d", len(new_cache))
            return True

    async def run_refresh_loop(self) -> None:
        """Background task — refreshes every ``_REFRESH_INTERVAL_S`` seconds.

        Cancellable via ``stop()`` from the lifespan teardown.
        """
        if not self.enabled:
            return
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=_REFRESH_INTERVAL_S,
                )
                return  # stopped
            except asyncio.TimeoutError:
                pass
            try:
                await self.refresh()
            except Exception as exc:  # noqa: BLE001
                logger.warning("hub_catalog_refresh_loop_error: %s", exc)

    def stop(self) -> None:
        self._stop.set()

    def get_sync(self, server_id: str) -> CatalogEntry | None:
        return self._cache.get(server_id)

    def all_sync(self) -> dict[str, CatalogEntry]:
        return dict(self._cache)


# ── Module-level singleton ──────────────────────────────────────


_INSTANCE: HubCatalogCache | None = None


def init(hub_url: str) -> HubCatalogCache:
    """Initialise the module-level singleton (idempotent on the same URL)."""
    global _INSTANCE
    if _INSTANCE is not None and _INSTANCE.hub_url == hub_url.rstrip("/"):
        return _INSTANCE
    _INSTANCE = HubCatalogCache(hub_url)
    return _INSTANCE


def get() -> HubCatalogCache | None:
    """Return the singleton, or ``None`` if ``init`` was never called."""
    return _INSTANCE


def reset() -> None:
    """Test hook — drops the singleton."""
    global _INSTANCE
    if _INSTANCE is not None:
        _INSTANCE.stop()
    _INSTANCE = None
