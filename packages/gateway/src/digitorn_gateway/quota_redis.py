"""Cross-worker quota coordination via Redis.

The hot path of every gateway request is **strictly in-memory** -
``QuotaEngine.is_blocked()`` reads a Python dict and returns in
sub-microseconds, with zero await, zero lock, zero I/O. That guarantee
must NEVER be compromised. Redis lives strictly OFF the hot path and
is consulted only after the response has been generated, by the
post-call accounting task that already runs as a FastAPI
``BackgroundTask``.

Why we need Redis at all:

  * The gateway scales horizontally (``--workers N`` on a single host
    or multiple Hetzner pods behind Caddy). Without a shared counter,
    each worker runs an independent in-memory tally and a user can
    blow past their limit by ``N×`` before any single worker notices.
  * Sticky blocks must propagate ACROSS workers in real time:
    when worker A flips user X to blocked, every concurrent request
    on worker B for X must observe the block immediately, not after
    the next 10-second Postgres flush.

Two coordination primitives:

  1. **INCR + EXPIRE** atomic increment per ``(user, metric, window,
     bucket_start)`` key. The Redis-returned value is the
     cluster-wide cumulative usage in that bucket - the single source
     of truth for limit comparison. Bucket TTL = window seconds + a
     few seconds of leeway so the auto-eviction matches the bucket
     reset.
  2. **Pub/Sub** on a single channel ``quota:blocks`` for block
     propagation. When any worker decides a user crossed a limit, it
     PUBLISHes the block. Every worker subscribes at boot and
     populates its local ``_blocks`` dict on receipt. Net effect:
     local block lookup remains in-memory + sub-µs while the SOURCE
     of truth for "who is blocked" is Redis.

Failure modes:

  * Redis unreachable at boot: the engine logs and falls back to
    legacy in-memory mode. The gateway keeps serving traffic;
    multi-worker coordination just doesn't fire until Redis recovers.
  * Redis hiccup mid-request: ``increment_and_check`` swallows the
    error and returns ``(local_estimate, False)`` so the request
    isn't blocked by a transient infra issue. The post-call usage
    event still lands in Postgres for audit.
  * Pub/Sub message loss (network blip): irrelevant for correctness
    because the next user-affected INCR on any worker will surface
    the same overflow and re-publish.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Awaitable, Callable

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from redis.asyncio import Redis as _Redis  # noqa: F401


# Channel name for cross-worker block broadcasts. Named generically so
# multiple gateway versions on the same Redis can coexist.
_BLOCKS_CHANNEL = "digitorn:gateway:quota:blocks"

# How long after the bucket end we let the Redis key linger. Absorbs
# clock skew across workers + the small lag between the worker that
# wrote the last increment and the bucket actually rolling over.
_BUCKET_TTL_LEEWAY_S = 30


class RedisCoordinator:
    """Cross-worker quota coordinator. Optional: when None, the
    QuotaEngine runs in legacy single-process mode."""

    def __init__(self, redis_url: str) -> None:
        self._url = redis_url
        # Two clients: one for short-lived ops (INCR / GET / PUBLISH)
        # with a tight socket_timeout to fail fast on Redis hiccups,
        # and one with NO timeout dedicated to ``pubsub.listen()``
        # which idles between messages and would otherwise hit the
        # short timeout repeatedly. Sharing a connection between fast
        # commands and a long-running listen() is what produced the
        # ``Timeout reading from localhost:6379`` log spam in the
        # first version.
        self._redis: Any | None = None
        self._pubsub_redis: Any | None = None
        self._pubsub_task: asyncio.Task | None = None
        self._block_listener: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._closed = False

    async def start(
        self,
        *,
        on_remote_block: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> bool:
        """Connect, ping, and start the Pub/Sub subscriber. Returns
        True on success, False if Redis is unreachable - the caller
        should treat the coordinator as inert in that case (legacy
        in-memory only)."""
        try:
            from redis.asyncio import Redis
        except Exception as exc:
            logger.warning("redis_quota_unavailable: %s", exc)
            return False
        try:
            self._redis = Redis.from_url(
                self._url,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
            )
            await self._redis.ping()
            # Separate connection for the Pub/Sub listener.
            #
            # We poll with ``pubsub.get_message(timeout=1.0)`` rather
            # than ``listen()``. ``get_message`` ALWAYS uses its own
            # short timeout for the read, so the underlying
            # ``socket_timeout`` value just bounds the worst-case
            # connection-level wait. We set it to a generous 30 s -
            # well past the 1 s polling window, so a healthy Redis
            # never trips it. ``health_check_interval=0`` disables
            # redis-py's internal heartbeat pings which were the
            # source of the spurious ``Timeout reading`` log spam in
            # the previous incarnation.
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
                "redis_quota_connect_failed url=%s err=%s "
                "(falling back to in-memory only)", self._url, exc,
            )
            self._redis = None
            self._pubsub_redis = None
            return False

        self._block_listener = on_remote_block
        self._pubsub_task = asyncio.create_task(
            self._pubsub_loop(), name="quota-redis-pubsub",
        )
        logger.info("redis_quota_started url=%s", self._url)
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

    # ── Counters ───────────────────────────────────────────────────

    async def increment(
        self,
        *,
        user_id: str,
        metric: str,
        window: str,
        bucket_start_epoch: int,
        delta: float,
        ttl_seconds: int,
    ) -> float | None:
        """Atomically add ``delta`` to the cluster-wide bucket counter
        and return the new total. ``None`` on Redis failure - the
        caller proceeds with its local estimate.

        Sub-millisecond on a healthy local Redis. Called only from
        the post-call background task, NEVER from the request hot
        path.
        """
        if self._redis is None:
            return None
        key = self._counter_key(user_id, metric, window, bucket_start_epoch)
        # The INCR is the load-bearing call. We MUST return the
        # increment's outcome to the caller even if the subsequent
        # EXPIRE fails (worst case: the key never gets a TTL and lives
        # forever - cosmetic, the next bucket creates a new key
        # anyway). Wrap each call separately so a transient EXPIRE
        # error doesn't shadow a successful INCR.
        try:
            new_val = await self._redis.incrbyfloat(key, delta)
        except Exception as exc:
            logger.debug(
                "redis_incr_failed user=%s metric=%s err=%s",
                user_id, metric, exc,
            )
            return None
        try:
            await self._redis.expire(
                key, ttl_seconds + _BUCKET_TTL_LEEWAY_S, nx=True,
            )
        except TypeError:
            try:
                await self._redis.expire(
                    key, ttl_seconds + _BUCKET_TTL_LEEWAY_S,
                )
            except Exception:
                pass
        except Exception as exc:
            logger.debug(
                "redis_expire_failed user=%s metric=%s err=%s",
                user_id, metric, exc,
            )
        try:
            return float(new_val)
        except (TypeError, ValueError):
            return None

    async def get_current(
        self,
        *,
        user_id: str,
        metric: str,
        window: str,
        bucket_start_epoch: int,
    ) -> float | None:
        """Read the cluster-wide bucket value WITHOUT incrementing.
        Used by ``snapshot()`` to surface multi-worker truth on the
        ``/v1/quota/me`` endpoint."""
        if self._redis is None:
            return None
        key = self._counter_key(user_id, metric, window, bucket_start_epoch)
        try:
            v = await self._redis.get(key)
            return float(v) if v is not None else None
        except Exception:
            return None

    @staticmethod
    def _counter_key(
        user_id: str, metric: str, window: str, bucket_start_epoch: int,
    ) -> str:
        # Versioned prefix so a future schema change doesn't collide
        # with old keys still in flight.
        return (
            f"digitorn:gateway:quota:v1:counter:"
            f"{user_id}:{metric}:{window}:{bucket_start_epoch}"
        )

    # ── Blocks ─────────────────────────────────────────────────────

    async def publish_block(self, info: Any) -> None:
        """Broadcast a sticky block to every gateway worker. The
        published payload is the JSON-friendly form of ``BlockInfo``;
        subscribers reconstruct it locally and stash on their
        in-memory dict so future ``is_blocked()`` calls return True
        instantly."""
        if self._redis is None:
            return
        try:
            payload = self._block_to_json(info)
            await self._redis.publish(_BLOCKS_CHANNEL, payload)
        except Exception as exc:
            logger.debug(
                "redis_block_publish_failed user=%s err=%s",
                getattr(info, "user_id", "?"), exc,
            )

    async def _pubsub_loop(self) -> None:
        """Long-running subscriber. Reconnects automatically on
        connection drops so a Redis restart doesn't permanently
        deafen this worker.
        """
        while not self._closed:
            pubsub = None
            try:
                if self._pubsub_redis is None:
                    await asyncio.sleep(1.0)
                    continue
                pubsub = self._pubsub_redis.pubsub(ignore_subscribe_messages=True)
                await pubsub.subscribe(_BLOCKS_CHANNEL)
                logger.info("redis_quota_pubsub_subscribed channel=%s", _BLOCKS_CHANNEL)
                # Poll with a short timeout instead of ``listen()``.
                # ``listen()`` blocks on the underlying socket and
                # interacts badly with redis-py's connection-level
                # socket_timeout (raises spurious TimeoutErrors when
                # idle). ``get_message(timeout=...)`` is the documented
                # idle-friendly read primitive and lets us check the
                # ``self._closed`` flag between polls for clean
                # shutdown.
                # Redis timeout errors are EXPECTED while idling
                # (no peer published a block in the last polling
                # window). Catch them inside the inner loop so we
                # silently retry instead of restarting the whole
                # subscription dance.
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
                    if data is None or self._block_listener is None:
                        continue
                    try:
                        payload = json.loads(data)
                    except Exception:
                        continue
                    try:
                        await self._block_listener(payload)
                    except Exception as exc:
                        logger.warning(
                            "redis_quota_block_listener_error err=%s", exc,
                        )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                # Redis idle-timeouts on the OUTER subscribe path are
                # not errors. redis-py raises a TimeoutError when an
                # idle socket gets recycled by the OS. Fall back on
                # message-substring detection because the exception
                # class can be ``redis.exceptions.TimeoutError``,
                # ``builtins.TimeoutError``, ``OSError``, or even an
                # ``ssl.SSLError`` depending on the deployment.
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
                        "redis_quota_pubsub_idle_reconnect %s: %s",
                        exc_name, exc,
                    )
                else:
                    logger.warning(
                        "redis_quota_pubsub_loop_error %s err=%s (retry in 2s)",
                        exc_name, exc,
                    )
                await asyncio.sleep(2.0)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.close()
                    except Exception:
                        pass

    @staticmethod
    def _block_to_json(info: Any) -> str:
        # Convert datetime + monotonic times to wall-clock so a
        # subscriber on another worker can rebuild a fresh BlockInfo
        # against its own ``time.monotonic()`` clock.
        try:
            d = asdict(info)
        except TypeError:
            d = dict(getattr(info, "__dict__", {}))
        bu = d.get("blocked_until_dt")
        if bu is not None and hasattr(bu, "isoformat"):
            d["blocked_until_dt_iso"] = bu.isoformat()
        d.pop("blocked_until_dt", None)
        d.pop("blocked_until", None)  # monotonic, worker-specific
        return json.dumps(d)
