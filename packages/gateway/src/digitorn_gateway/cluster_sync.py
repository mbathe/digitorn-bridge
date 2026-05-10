"""Cluster-wide synchronisation primitives.

Three independent surfaces share this module:

  * ``notify(channel, payload)`` - fire-and-forget Postgres NOTIFY,
    used by admin mutations to broadcast "settings changed",
    "credentials reloaded", "block-list updated" to every worker
    in the cluster. The payload is small text; consumers read the
    real state from DB / Redis.

  * ``listen(channel, handler)`` - background task that holds a
    DEDICATED asyncpg connection in LISTEN mode. The pool's regular
    connections cannot be used because LISTEN occupies the
    connection for its lifetime. Reconnects automatically on
    network blip with exponential backoff.

  * ``try_acquire_leader_lock(scope)`` / ``release_leader_lock(scope)``
    - Postgres advisory locks for leader-election. Multiple workers
    call this; exactly one gets the lock. Used to ensure background
    jobs (quota supervisor, partition keeper, OAuth refresh loop)
    run on exactly one worker per cluster.

The module never crashes the gateway. A broken Postgres just means
notifications get dropped + advisory locks return False (workers
fall back to per-process behaviour). The dispatch hot path NEVER
calls into this module.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


# ── Channels ────────────────────────────────────────────────────────
#
# Channel names live here so consumers and producers never disagree
# on the spelling. Postgres caps channel names at 63 chars.

CHANNEL_RUNTIME_SETTINGS = "gateway_runtime_settings_changed"
CHANNEL_CONFIG_CACHE = "gateway_config_cache_changed"
CHANNEL_QUOTA_BLOCKS = "gateway_quota_blocks_changed"


# ── Internal state ──────────────────────────────────────────────────


# {channel -> [handler, ...]}: every worker's listener calls each
# handler in order on a NOTIFY for that channel.
_handlers: dict[str, list[Callable[[str], Awaitable[None]]]] = {}

# Background task running the listener loop. None when not started.
_listener_task: asyncio.Task[None] | None = None

# Cached asyncpg dsn (converted from SQLAlchemy URL). Computed once.
_pg_dsn: str | None = None


# ── DSN conversion ──────────────────────────────────────────────────


def _resolve_dsn() -> str | None:
    """Translate the SQLAlchemy ``database_url`` (asyncpg flavour)
    into a plain Postgres DSN that the asyncpg driver can open
    directly. Returns ``None`` when no DB URL is configured."""
    global _pg_dsn
    if _pg_dsn is not None:
        return _pg_dsn
    try:
        from digitorn_gateway.config import get_settings
        url = get_settings().database_url or ""
    except Exception:
        return None
    if not url:
        return None
    # SQLAlchemy uses ``postgresql+asyncpg://``; asyncpg wants
    # ``postgresql://``. Stripping the driver suffix is the only
    # transform needed; asyncpg handles ``?ssl=require`` natively.
    if url.startswith("postgresql+asyncpg://"):
        url = "postgresql://" + url[len("postgresql+asyncpg://"):]
    _pg_dsn = url
    return _pg_dsn


# ── Notifier (producer side) ────────────────────────────────────────


async def notify(channel: str, payload: str = "") -> None:
    """Fire a Postgres NOTIFY. Never raises - admin endpoints stay
    responsive even when the listener network is flapping.

    The payload is intentionally small (a key name, a row id). The
    listener fetches the actual state from the canonical store
    (``gateway_runtime_settings`` row, the cache reload, etc.). This
    keeps NOTIFY messages well under Postgres's 8000-byte cap and
    avoids race-conditions where the payload disagrees with the row.
    """
    try:
        from digitorn_gateway.db import get_session_factory
        from sqlalchemy import text
        # NOTIFY runs on a regular pool connection; it's a one-shot
        # statement that returns immediately, no locks held.
        # asyncpg ``NOTIFY`` requires a literal channel + literal
        # payload. We escape both via parameter binding... wait,
        # NOTIFY doesn't accept bind params. Use safe channel/payload
        # validation: alphanum + underscore only on channel; payload
        # is single-quoted with escape.
        safe_channel = "".join(
            c for c in channel if c.isalnum() or c == "_"
        )[:63]
        safe_payload = (payload or "").replace("'", "''")[:7000]
        if not safe_channel:
            return
        factory = get_session_factory()
        async with factory() as db:
            await db.execute(
                text(f"NOTIFY {safe_channel}, '{safe_payload}'")
            )
            await db.commit()
    except Exception as exc:
        logger.debug("cluster_sync.notify failed channel=%s err=%s", channel, exc)


# ── Listener (consumer side) ────────────────────────────────────────


def register_handler(
    channel: str, handler: Callable[[str], Awaitable[None]],
) -> None:
    """Register a coroutine that runs when this worker receives a
    NOTIFY on ``channel``. Multiple handlers per channel are allowed
    and execute in registration order (errors in one don't stop the
    next)."""
    _handlers.setdefault(channel, []).append(handler)


async def start_listener() -> None:
    """Spawn the background listener task. Idempotent.

    The task holds a DEDICATED asyncpg connection that issues LISTEN
    for every registered channel and dispatches incoming NOTIFY
    messages to the handlers. On connection drop it reconnects with
    exponential backoff. Never raises in the lifespan path - a
    failed listener just leaves the worker on its local snapshot
    (same as before this module existed).
    """
    global _listener_task
    if _listener_task is not None and not _listener_task.done():
        return
    if not _resolve_dsn():
        logger.info("cluster_sync.listener skipped: no DATABASE_URL")
        return
    if not _handlers:
        logger.info("cluster_sync.listener skipped: no handlers registered")
        return
    _listener_task = asyncio.create_task(
        _listener_loop(), name="cluster_sync.listener",
    )


async def stop_listener() -> None:
    """Cancel the listener at shutdown. Idempotent."""
    global _listener_task
    if _listener_task is None:
        return
    _listener_task.cancel()
    try:
        await _listener_task
    except (asyncio.CancelledError, Exception):
        pass
    _listener_task = None


async def _listener_loop() -> None:
    """Connect, LISTEN every channel, dispatch NOTIFY, reconnect on
    failure. Backoff capped at 30s so a flapping DB doesn't tight-loop
    or stay disconnected forever."""
    backoff = 1.0
    max_backoff = 30.0
    dsn = _resolve_dsn()
    while True:
        conn = None
        try:
            import asyncpg  # type: ignore[import]
            conn = await asyncpg.connect(dsn)
            for channel in list(_handlers.keys()):
                await conn.add_listener(channel, _on_notify)
            logger.info(
                "cluster_sync.listener up channels=%s",
                ",".join(_handlers.keys()),
            )
            backoff = 1.0
            # Keep the coroutine alive; asyncpg dispatches in the
            # background. ``conn.close()`` would tear the listener
            # down so we sleep on a long ping instead.
            while True:
                await asyncio.sleep(15)
                # Cheap roundtrip - confirms the connection is live.
                # On Neon's serverless pool the idle disconnect is
                # ~5min; this keeps us under that.
                try:
                    await conn.fetchval("SELECT 1")
                except Exception:
                    raise  # re-enter outer loop to reconnect
        except asyncio.CancelledError:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
            raise
        except Exception as exc:
            logger.warning(
                "cluster_sync.listener disconnected (%s); retry in %.1fs",
                exc, backoff,
            )
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


def _on_notify(_conn: Any, _pid: int, channel: str, payload: str) -> None:
    """asyncpg dispatches NOTIFY synchronously into this callback.
    We schedule the handlers as tasks so a slow handler can't block
    the listener loop."""
    handlers = _handlers.get(channel, [])
    if not handlers:
        return
    for h in handlers:
        try:
            asyncio.create_task(_safe_invoke(h, channel, payload))
        except Exception as exc:
            logger.warning(
                "cluster_sync handler scheduling failed channel=%s err=%s",
                channel, exc,
            )


async def _safe_invoke(
    h: Callable[[str], Awaitable[None]], channel: str, payload: str,
) -> None:
    try:
        await h(payload)
    except Exception as exc:
        logger.warning(
            "cluster_sync handler raised channel=%s err=%s",
            channel, exc,
        )


# ── Leader election (advisory locks) ────────────────────────────────


# Track which scopes this worker holds so we don't try to re-acquire
# a lock we already own (advisory locks ARE re-entrant per session,
# but we don't need that and the bookkeeping makes shutdown cleaner).
_held_locks: set[str] = set()
# Each scope keeps a dedicated asyncpg connection: advisory locks
# are session-scoped, releasing the connection releases the lock.
# We do NOT use the regular pool because pool connections can be
# returned mid-block.
_lock_connections: dict[str, Any] = {}


def _scope_to_key(scope: str) -> int:
    """Map a string scope to a 64-bit signed int the way Postgres
    advisory locks need. SHA-256 truncated keeps collisions to
    near-zero between scopes you'd actually use."""
    h = hashlib.sha256(scope.encode("utf-8")).digest()
    # Take 8 bytes and convert to signed int64.
    return int.from_bytes(h[:8], "big", signed=True)


async def try_acquire_leader_lock(scope: str) -> bool:
    """Return True iff this worker becomes the leader for ``scope``.

    Multiple workers call this with the same string; exactly one
    gets True, the others False. The successful worker is expected
    to call ``release_leader_lock(scope)`` at shutdown. If the
    process crashes the connection drops and another worker can
    take over on its next attempt.

    Returns False on ANY error (no DB, network, locks unavailable):
    the caller falls back to per-worker behaviour, which is always
    safe (just less efficient at cluster scale).
    """
    if scope in _held_locks:
        return True
    if not _resolve_dsn():
        return False
    try:
        import asyncpg  # type: ignore[import]
        conn = await asyncpg.connect(_resolve_dsn())
        key = _scope_to_key(scope)
        # pg_try_advisory_lock: non-blocking, returns true on success.
        got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", key)
        if not got:
            await conn.close()
            return False
        _held_locks.add(scope)
        _lock_connections[scope] = conn
        logger.info("cluster_sync.leader acquired scope=%s", scope)
        return True
    except Exception as exc:
        logger.debug(
            "cluster_sync.leader acquire failed scope=%s err=%s", scope, exc,
        )
        return False


async def release_leader_lock(scope: str) -> None:
    """Drop the advisory lock so another worker can take over.
    Closing the asyncpg connection releases the lock implicitly;
    we still call pg_advisory_unlock for symmetry on graceful
    shutdown."""
    conn = _lock_connections.pop(scope, None)
    _held_locks.discard(scope)
    if conn is None:
        return
    try:
        await conn.fetchval(
            "SELECT pg_advisory_unlock($1)", _scope_to_key(scope),
        )
    except Exception:
        pass
    try:
        await conn.close()
    except Exception:
        pass
    logger.info("cluster_sync.leader released scope=%s", scope)


def is_leader(scope: str) -> bool:
    """Synchronous check used by background loops to decide whether
    to do work this tick. Cheap (just a set membership)."""
    return scope in _held_locks


# ── Leader takeover loop ────────────────────────────────────────────


# {scope -> (on_become_leader, on_lose_leader)}
_takeover_callbacks: dict[
    str,
    tuple[
        Callable[[], Awaitable[None]] | None,
        Callable[[], Awaitable[None]] | None,
    ],
] = {}
_takeover_task: asyncio.Task[None] | None = None
_TAKEOVER_INTERVAL_S = 30.0


def watch_leader(
    scope: str,
    on_become: Callable[[], Awaitable[None]] | None = None,
    on_lose: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Register callbacks fired when this worker gains / loses the
    leader role for ``scope``. The takeover loop polls every
    ``_TAKEOVER_INTERVAL_S`` (default 30s) and:

      * if we DON'T hold the lock and ``pg_try_advisory_lock`` succeeds,
        ``on_become`` runs (start the supervisor task);
      * if we hold the lock but the dedicated connection broke, the
        loop drops our local state and tries to re-acquire next tick;
      * ``on_lose`` is fired when we voluntarily release on shutdown.

    Initial acquisition happens at lifespan startup via
    ``try_acquire_leader_lock``; this loop only handles HEALING
    (the previous leader died, we should pick up).
    """
    _takeover_callbacks[scope] = (on_become, on_lose)


async def start_takeover_loop() -> None:
    """Spawn the periodic re-acquire loop. Idempotent."""
    global _takeover_task
    if _takeover_task is not None and not _takeover_task.done():
        return
    if not _takeover_callbacks:
        return
    if not _resolve_dsn():
        return
    _takeover_task = asyncio.create_task(
        _takeover_loop(), name="cluster_sync.takeover",
    )


async def stop_takeover_loop() -> None:
    global _takeover_task
    if _takeover_task is None:
        return
    _takeover_task.cancel()
    try:
        await _takeover_task
    except (asyncio.CancelledError, Exception):
        pass
    _takeover_task = None


async def _takeover_loop() -> None:
    """Periodically attempt to acquire any leader lock we don't hold.

    The dedicated lock connection from ``try_acquire_leader_lock``
    can break (TCP idle close on Neon, network blip). We detect this
    by sending a cheap ``SELECT 1`` on our held locks; if it fails we
    drop the entry and re-attempt on the next tick. Other workers
    racing the same lock get their own copy: pg_try_advisory_lock
    returns true to exactly one process.
    """
    while True:
        try:
            await asyncio.sleep(_TAKEOVER_INTERVAL_S)
            # Health-check held locks first.
            for scope in list(_held_locks):
                conn = _lock_connections.get(scope)
                if conn is None:
                    _held_locks.discard(scope)
                    continue
                try:
                    await conn.fetchval("SELECT 1")
                except Exception:
                    logger.warning(
                        "cluster_sync.leader connection lost scope=%s; "
                        "will retry acquire next tick",
                        scope,
                    )
                    on_become, on_lose = _takeover_callbacks.get(
                        scope, (None, None),
                    )
                    _held_locks.discard(scope)
                    _lock_connections.pop(scope, None)
                    if on_lose is not None:
                        try:
                            await on_lose()
                        except Exception:
                            logger.debug(
                                "leader on_lose handler raised", exc_info=True,
                            )
            # Try to acquire what we don't hold.
            for scope, (on_become, _on_lose) in _takeover_callbacks.items():
                if scope in _held_locks:
                    continue
                got = await try_acquire_leader_lock(scope)
                if got and on_become is not None:
                    try:
                        await on_become()
                    except Exception:
                        logger.warning(
                            "leader on_become handler raised scope=%s",
                            scope, exc_info=True,
                        )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("cluster_sync.takeover_loop error: %s", exc)
            await asyncio.sleep(_TAKEOVER_INTERVAL_S)
