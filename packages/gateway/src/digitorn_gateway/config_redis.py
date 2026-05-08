"""Cross-worker ``ConfigCache`` invalidation via Redis Pub/Sub.

The gateway scales horizontally with ``--workers N`` (single host) or
multiple Hetzner pods. Each worker holds its own ``ConfigCache``
populated at boot from the DB and refreshed every 30s. Without a
shared invalidation channel, a write-through mutation from worker A
(``upsert_route``, ``upsert_credential``, ...) leaves worker B's cache
stale until B's next periodic refresh - up to 30 s of wrong dispatch
identity, mismatched credentials, or routes that don't exist yet.

This module mirrors the pattern in ``quota_redis.py``: a Pub/Sub
channel everyone subscribes to, plus a tiny publisher API the admin
endpoints call after committing to Postgres. On receipt every worker
schedules a ``reload_from_db()`` so its dicts re-read the current
truth in < 200 ms.

Failure modes:

  * Redis unreachable at boot: the coordinator logs and stays inert.
    Each worker continues with its local cache + 30 s periodic
    refresh - same correctness guarantee as before this module
    existed, just no instant cross-worker convergence.
  * Redis hiccup mid-publish: the publisher swallows the error and
    logs at debug level. The next periodic refresh on each worker
    will pick the change up; we don't block the admin write on
    Redis health.
  * Pub/Sub message loss (network blip): irrelevant for correctness
    because the next mutation re-publishes and the periodic refresh
    catches anything missed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


# Channel name. Versioned so a future schema change doesn't get
# confused by stale messages from older deploys.
_INVALIDATE_CHANNEL = "digitorn:gateway:config:invalidate:v1"


class ConfigCoordinator:
    """Cross-worker config-cache coordinator. Optional: when None, the
    gateway runs in legacy single-process mode (caches converge only
    via the 30 s periodic refresh)."""

    def __init__(self, redis_url: str) -> None:
        self._url = redis_url
        # Two clients for the same reasons as ``quota_redis.py``: a
        # short-timeout one for the publish hot path, a longer-timeout
        # one for the always-listening subscriber.
        self._redis: Any | None = None
        self._pubsub_redis: Any | None = None
        self._pubsub_task: asyncio.Task | None = None
        self._on_invalidate: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._closed = False

    async def start(
        self,
        *,
        on_invalidate: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> bool:
        """Connect, ping, start the subscriber. Returns True on success,
        False if Redis is unreachable - in which case the caller should
        carry on without cross-worker invalidation (legacy 30 s
        periodic refresh still works)."""
        try:
            from redis.asyncio import Redis
        except Exception as exc:
            logger.warning("redis_config_unavailable: %s", exc)
            return False
        try:
            self._redis = Redis.from_url(
                self._url,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
            )
            await self._redis.ping()
            self._pubsub_redis = Redis.from_url(
                self._url,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=30.0,
                socket_keepalive=True,
                health_check_interval=0,
            )
            await self._pubsub_redis.ping()
        except Exception as exc:
            logger.warning(
                "redis_config_connect_failed url=%s err=%s "
                "(falling back to periodic-only refresh)", self._url, exc,
            )
            self._redis = None
            self._pubsub_redis = None
            return False

        self._on_invalidate = on_invalidate
        self._pubsub_task = asyncio.create_task(
            self._pubsub_loop(), name="config-redis-pubsub",
        )
        logger.info("redis_config_started url=%s", self._url)
        return True

    async def stop(self) -> None:
        self._closed = True
        if self._pubsub_task is not None:
            self._pubsub_task.cancel()
            try:
                await self._pubsub_task
            except asyncio.CancelledError:
                pass
            self._pubsub_task = None
        if self._pubsub_redis is not None:
            try:
                await self._pubsub_redis.aclose()
            except Exception:
                pass
            self._pubsub_redis = None
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None

    @property
    def alive(self) -> bool:
        return self._redis is not None

    # ── Publish ────────────────────────────────────────────────────

    async def publish(self, kind: str, *, key: str = "") -> None:
        """Broadcast a ``(kind, key)`` invalidation to every gateway
        worker. ``kind`` is the entity type (``provider``, ``credential``,
        ``model``, ``route``); ``key`` is the optional id / alias /
        slug. Subscribers always do a full ``reload_from_db()`` today
        - the kind / key are carried for future granular invalidation
        + audit visibility."""
        if self._redis is None:
            return
        try:
            payload = json.dumps({"kind": kind, "key": key})
            await self._redis.publish(_INVALIDATE_CHANNEL, payload)
        except Exception as exc:
            logger.debug(
                "redis_config_publish_failed kind=%s key=%s err=%s",
                kind, key, exc,
            )

    # ── Subscribe ──────────────────────────────────────────────────

    async def _pubsub_loop(self) -> None:
        """Long-running subscriber. Reconnects automatically on
        connection drops so a Redis restart doesn't permanently
        deafen this worker."""
        while not self._closed:
            pubsub = None
            try:
                if self._pubsub_redis is None:
                    await asyncio.sleep(1.0)
                    continue
                pubsub = self._pubsub_redis.pubsub(
                    ignore_subscribe_messages=True,
                )
                await pubsub.subscribe(_INVALIDATE_CHANNEL)
                logger.info(
                    "redis_config_pubsub_subscribed channel=%s",
                    _INVALIDATE_CHANNEL,
                )
                try:
                    from redis.exceptions import TimeoutError as _RTimeout
                except Exception:
                    _RTimeout = TimeoutError  # noqa: N806
                while not self._closed:
                    try:
                        msg = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=1.0,
                        )
                    except _RTimeout:
                        continue
                    except asyncio.TimeoutError:
                        continue
                    if msg is None:
                        continue
                    data = msg.get("data") if isinstance(msg, dict) else None
                    if data is None or self._on_invalidate is None:
                        continue
                    try:
                        payload = json.loads(data)
                    except Exception:
                        continue
                    try:
                        await self._on_invalidate(payload)
                    except Exception as exc:
                        logger.warning(
                            "redis_config_invalidate_listener_error err=%s",
                            exc,
                        )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                msg = str(exc).lower()
                exc_name = type(exc).__name__
                is_idle = (
                    exc_name in ("TimeoutError", "ConnectionError")
                    or "timeout" in msg
                    or "connection closed" in msg
                    or "broken pipe" in msg
                )
                if is_idle:
                    logger.debug(
                        "redis_config_pubsub_idle_reconnect %s: %s",
                        exc_name, exc,
                    )
                else:
                    logger.warning(
                        "redis_config_pubsub_loop_error %s err=%s "
                        "(retry in 2s)", exc_name, exc,
                    )
                await asyncio.sleep(2.0)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.close()
                    except Exception:
                        pass


# Module-level singleton, set by main.lifespan when Redis is configured.
_coordinator: ConfigCoordinator | None = None


def get_coordinator() -> ConfigCoordinator | None:
    return _coordinator


def set_coordinator(c: ConfigCoordinator | None) -> None:
    global _coordinator
    _coordinator = c


async def publish_invalidation(kind: str, *, key: str = "") -> None:
    """Publish helper used by the admin write paths. Safe to call when
    no coordinator exists - it's a no-op."""
    c = get_coordinator()
    if c is not None:
        await c.publish(kind, key=key)
