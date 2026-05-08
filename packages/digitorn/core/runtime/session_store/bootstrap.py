"""Daemon-side wiring for the SessionStore subsystem.

One-line activation in the daemon's lifespan:

    from digitorn.core.runtime.session_store.bootstrap import (
        init_session_store, shutdown_session_store,
    )

    @asynccontextmanager
    async def lifespan(app):
        store = await init_session_store()
        try:
            yield
        finally:
            await shutdown_session_store(store)

The bootstrap reads three env vars:

  * ``DIGITORN_SESSION_STORE_MODE``  -- ``off`` | ``shadow`` | ``primary``
  * ``DIGITORN_SESSION_STORE_ROOT``  -- defaults to ``~/.digitorn/sessions``
  * ``DIGITORN_SESSION_STORE_MAX_BYTES`` -- LRU cap (default 4 GB)

When MODE != off, the bootstrap installs a process-wide
``SessionStoreBridge`` that ``history.record()`` fans out to. Existing
call sites are not modified; the bridge is the integration seam.

When MODE == off (default), the bootstrap is a no-op and the daemon
runs on the legacy Postgres path exactly as before.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from digitorn.core.runtime.session_store.bridge import (
    BridgeMode, SessionStoreBridge,
    resolve_mode_from_env, set_default_bridge,
)
from digitorn.core.runtime.session_store.session_index import (
    SessionIndex, SqliteSessionIndex,
)
from digitorn.core.runtime.session_store.store import InMemorySessionStore

logger = logging.getLogger(__name__)


_DEFAULT_ROOT = Path.home() / ".digitorn" / "sessions"
_DEFAULT_MAX_BYTES = 4 * 1024 * 1024 * 1024
_DEFAULT_MAX_SESSIONS = 1000
_DEFAULT_FLUSH_MS = 50


def _resolve_root() -> Path:
    raw = os.environ.get("DIGITORN_SESSION_STORE_ROOT")
    if raw:
        return Path(raw).expanduser()
    return _DEFAULT_ROOT


def _resolve_int(env: str, default: int) -> int:
    raw = os.environ.get(env)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using default %d", env, raw, default,
        )
        return default


def _resolve_index_path() -> Path | None:
    """Default to ``<sessions_root>/.digitorn-index.db`` next to the
    sessions tree when ``DIGITORN_SESSION_INDEX_PATH`` is unset.
    Returns None when the operator explicitly opts out via setting
    the env var to ``off`` / ``disabled`` / empty."""
    raw = os.environ.get("DIGITORN_SESSION_INDEX_PATH")
    if raw is None:
        return None
    raw = raw.strip()
    if raw.lower() in ("", "off", "disabled", "none"):
        return None
    return Path(raw).expanduser()


async def init_session_store(
    *,
    root: Path | None = None,
    mode: BridgeMode | None = None,
    max_bytes: int | None = None,
    max_sessions: int | None = None,
    flush_interval_ms: int | None = None,
    index: SessionIndex | None = None,
) -> InMemorySessionStore | None:
    """Initialise the process-wide SessionStore + Bridge.

    Returns the store on success, ``None`` if MODE is ``off`` (the
    default; daemon falls through to legacy behavior).

    Idempotent-friendly: a second call with no kwargs returns whatever
    bridge is already registered without re-creating it. To replace,
    call ``shutdown_session_store(store)`` first.
    """
    resolved_mode = mode if mode is not None else resolve_mode_from_env()
    if resolved_mode == BridgeMode.OFF:
        logger.info(
            "session_store_disabled mode=off "
            "(set DIGITORN_SESSION_STORE_MODE=shadow|primary to enable)",
        )
        return None

    resolved_root = root if root is not None else _resolve_root()
    resolved_max_bytes = (
        max_bytes if max_bytes is not None
        else _resolve_int(
            "DIGITORN_SESSION_STORE_MAX_BYTES", _DEFAULT_MAX_BYTES,
        )
    )
    resolved_max_sessions = (
        max_sessions if max_sessions is not None
        else _resolve_int(
            "DIGITORN_SESSION_STORE_MAX_SESSIONS", _DEFAULT_MAX_SESSIONS,
        )
    )
    resolved_flush_ms = (
        flush_interval_ms if flush_interval_ms is not None
        else _resolve_int(
            "DIGITORN_SESSION_STORE_FLUSH_MS", _DEFAULT_FLUSH_MS,
        )
    )

    resolved_index = index
    if resolved_index is None:
        idx_path = _resolve_index_path()
        if idx_path is not None:
            resolved_index = SqliteSessionIndex(db_path=idx_path)

    store = InMemorySessionStore(
        root=resolved_root,
        flush_interval_ms=resolved_flush_ms,
        max_sessions_in_memory=resolved_max_sessions,
        max_bytes_in_memory=resolved_max_bytes,
        index=resolved_index,
    )
    await store.start()

    bridge = SessionStoreBridge(store, mode=resolved_mode)
    set_default_bridge(bridge)

    logger.info(
        "session_store_started mode=%s root=%s max_bytes=%d "
        "max_sessions=%d flush_ms=%d index=%s",
        resolved_mode.value, resolved_root, resolved_max_bytes,
        resolved_max_sessions, resolved_flush_ms,
        getattr(resolved_index, "db_path", None) if resolved_index else "none",
    )
    return store


async def shutdown_session_store(
    store: InMemorySessionStore | None,
) -> None:
    """Shutdown helper: drains the disk flusher, stops background
    tasks, removes the bridge. Safe to call with ``None`` (no-op)."""
    if store is None:
        return
    set_default_bridge(None)
    try:
        await store.stop()
    except Exception as exc:
        logger.warning(
            "session_store_shutdown_failed err=%s "
            "(some sessions may have unflushed events)", exc,
        )
