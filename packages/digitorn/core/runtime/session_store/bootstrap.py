"""Daemon-side wiring for the SessionStore subsystem."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

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


def _resolve_index_path(sessions_root: Path) -> Path | None:
    """Resolve the SQLite session index path."""
    raw = os.environ.get("DIGITORN_SESSION_INDEX_PATH")
    if raw is None:
        # New default: enable + place inside the sessions root so the
        # whole subsystem stays self-contained under one directory.
        return sessions_root / ".digitorn-index.db"
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
    durability_mode: str | None = None,
    num_shards: int | None = None,
    index: SessionIndex | None = None,
    on_internal_seq_alloc: Any = None,
) -> InMemorySessionStore | None:
    """Initialise the process-wide SessionStore + Bridge."""
    resolved_mode = mode if mode is not None else resolve_mode_from_env()
    if resolved_mode == BridgeMode.OFF:
        # `off` is no longer a valid runtime mode -- the daemon needs
        # the SessionStore. Log loudly and upgrade to `primary`.
        logger.warning(
            "session_store_mode_off_promoted_to_primary -- legacy KV "
            "session store removed; running in primary mode",
        )
        resolved_mode = BridgeMode.PRIMARY

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
    resolved_durability = (
        durability_mode if durability_mode is not None
        else os.environ.get("DIGITORN_SESSION_STORE_DURABILITY", "strict")
    ).lower().strip()
    resolved_num_shards = (
        num_shards if num_shards is not None
        else _resolve_int("DIGITORN_SESSION_STORE_NUM_SHARDS", 32)
    )

    resolved_index = index
    if resolved_index is None:
        idx_path = _resolve_index_path(resolved_root)
        if idx_path is not None:
            resolved_index = SqliteSessionIndex(db_path=idx_path)
            asyncio.create_task(
                _reconcile_index_from_disk(resolved_root, resolved_index),
                name="session-index-boot-reconcile",
            )

    store = InMemorySessionStore(
        root=resolved_root,
        flush_interval_ms=resolved_flush_ms,
        max_sessions_in_memory=resolved_max_sessions,
        max_bytes_in_memory=resolved_max_bytes,
        index=resolved_index,
        durability_mode=resolved_durability,
        num_shards=resolved_num_shards,
        on_internal_seq_alloc=on_internal_seq_alloc,
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
    """Shutdown helper: drains the disk flusher, stops background"""
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


async def _reconcile_index_from_disk(
    sessions_root: Path, index: "SqliteSessionIndex",
) -> None:
    """Walk every `meta.json` under `sessions_root` and upsert it"""
    from digitorn.core.runtime.session_store.session_index import (
        SessionSummary,
    )
    if not sessions_root.exists():
        return
    inserted = 0
    skipped = 0
    try:
        # Walk in a worker thread so glob+stat doesn't block the
        # event loop on a sessions tree with thousands of dirs.
        meta_paths = await asyncio.to_thread(
            lambda: list(sessions_root.rglob("meta.json")),
        )
    except Exception as exc:
        logger.warning("session_index_reconcile_walk_failed err=%s", exc)
        return
    for meta_path in meta_paths:
        # Skip index DB siblings and hidden dirs.
        if meta_path.parent.name.startswith("."):
            continue
        try:
            raw = await asyncio.to_thread(
                lambda p=meta_path: p.read_text(encoding="utf-8"),
            )
            meta = json.loads(raw)
        except Exception:
            skipped += 1
            continue
        sid = meta.get("session_id")
        aid = meta.get("app_id")
        uid = meta.get("user_id")
        if not sid or not aid or not uid:
            skipped += 1
            continue
        try:
            await index.upsert(SessionSummary(
                session_id=str(sid),
                app_id=str(aid),
                user_id=str(uid),
                started_at=str(meta.get("started_at", "") or ""),
                ended_at=meta.get("ended_at"),
                closed=bool(meta.get("closed", False)),
                last_seq=int(meta.get("last_seq", 0) or 0),
                event_count=int(meta.get("event_count", 0) or 0),
                cost_total=float(meta.get("cost_total", 0.0) or 0.0),
                tokens_in=int(meta.get("tokens_in", 0) or 0),
                tokens_out=int(meta.get("tokens_out", 0) or 0),
                title=meta.get("title"),
            ))
            inserted += 1
        except Exception:
            skipped += 1
    logger.info(
        "session_index_reconciled inserted=%d skipped=%d root=%s",
        inserted, skipped, sessions_root,
    )
