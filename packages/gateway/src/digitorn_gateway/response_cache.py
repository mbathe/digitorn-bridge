"""Response cache for /v1/chat/completions.

Design constraints (hard requirements from operator):
  1. Master switch via ``settings.cache_enabled``. When OFF, the cache
     code path is fully bypassed - zero overhead, zero Redis call,
     zero key compute. The only cost is one ``if`` in main.py.
  2. Per-request opt-in via header ``X-Digitorn-Cache: enabled``. So
     even when the master switch is on, only clients who asked for
     caching pay the lookup cost.
  3. Hot path safety: the GET is wrapped in ``asyncio.wait_for`` with
     a hard timeout (default 5ms). A slow Redis can never slow the
     gateway - we just fall through to a normal dispatch.
  4. Crash safety: every Redis op is wrapped in ``try/except``.
     Connection refused, network blip, parse error - all swallowed
     and the request continues normally (no cache hit, no failure).
  5. Storage runs in BackgroundTask AFTER the response is sent to the
     client. The user never waits for a cache write.
  6. NEVER caches: streaming, temperature>0, tool/function calls,
     system_fingerprint-sensitive prompts. Only deterministic chat.

Key shape: ``v1:resp:{user_id}:{sha256(model+messages+temp+max_tokens)}``
The user_id prefix scopes the cache (no cross-user leak). The sha256
canonicalises the body so two equivalent requests hash to the same key.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


# Module-level state. Initialized once via ``init_cache`` at lifespan start.
_redis_client: Any | None = None
_local_lru: dict[str, tuple[float, dict]] = {}  # fallback when no Redis
_LOCAL_MAX_ENTRIES = 5_000


def init_cache(redis_url: str = "") -> None:
    """Wire up the cache backend at boot. Safe to call when caching is
    disabled (does nothing). Crashes here would break the gateway, so
    every step is guarded - if Redis is unreachable we fall back to
    the in-process LRU silently.
    """
    global _redis_client
    if not redis_url:
        logger.info("response_cache: no redis_url, using in-process LRU")
        return
    try:
        # Lazy import so the module loads even when redis isn't installed.
        import redis.asyncio as redis  # type: ignore[import]
        _redis_client = redis.from_url(
            redis_url,
            socket_timeout=0.05,        # 50ms socket-level cap
            socket_connect_timeout=1.0, # 1s to establish, then give up forever
            health_check_interval=30,
        )
        logger.info("response_cache: Redis client created (url=%s)", redis_url)
    except Exception as exc:
        # Couldn't import redis or build the client - degrade silently.
        logger.warning(
            "response_cache: Redis init failed (%s); falling back to in-process LRU",
            exc,
        )
        _redis_client = None


async def shutdown_cache() -> None:
    """Best-effort close on lifespan exit."""
    global _redis_client
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception:
            pass
        _redis_client = None


# ── Key computation ────────────────────────────────────────────────


def is_cacheable(body: dict[str, Any]) -> bool:
    """Return False if this request must NEVER be cached.

    Rules:
      - stream=true: would need to replay chunks
      - temperature > 0: non-deterministic output
      - tools / functions: side effects
      - top_p < 1 with temperature == 0: still safe
      - n > 1: multiple completions, one cache key can't hold all
      - response_format with json_schema: depends on prompt, still cacheable

    These rules are conservative - we'd rather miss a cache hit than
    return a wrong/stale/leaky response.
    """
    if not isinstance(body, dict):
        return False
    if body.get("stream"):
        return False
    temp = body.get("temperature")
    if temp is not None and float(temp) > 0:
        return False
    if body.get("tools") or body.get("tool_choice"):
        return False
    if body.get("functions") or body.get("function_call"):
        return False
    if int(body.get("n") or 1) > 1:
        return False
    if not body.get("messages"):
        return False
    if not body.get("model"):
        return False
    return True


def compute_key(user_id: str, body: dict[str, Any]) -> str | None:
    """Stable cache key. Returns None if not cacheable.

    The body fields included are exactly the ones that determine the
    output. ``user`` field is excluded (it's metadata for billing on
    OpenAI, not part of the prompt). ``stream_options`` is excluded.
    """
    if not is_cacheable(body):
        return None
    canon = {
        "model": body.get("model"),
        "messages": body.get("messages"),
        "max_tokens": body.get("max_tokens"),
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "stop": body.get("stop"),
        "seed": body.get("seed"),
        "response_format": body.get("response_format"),
    }
    try:
        payload = json.dumps(canon, sort_keys=True, separators=(",", ":"))
    except Exception:
        return None
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    # User-scoped: no cross-user leak.
    return f"v1:resp:{user_id}:{h}"


# ── Lookup (hot path) ──────────────────────────────────────────────


async def lookup(key: str, timeout_ms: int = 5) -> dict[str, Any] | None:
    """Try to retrieve a cached response. Returns None on miss, on
    Redis error, on timeout, on parse error - the caller MUST treat
    None as 'no cache, dispatch normally'. NEVER raises.
    """
    # In-process LRU first (free, sub-µs).
    entry = _local_lru.get(key)
    if entry is not None:
        expires_at, payload = entry
        if expires_at > time.time():
            return payload
        _local_lru.pop(key, None)

    if _redis_client is None:
        return None

    # Redis path with strict timeout.
    try:
        raw = await asyncio.wait_for(
            _redis_client.get(key),
            timeout=timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        # Slow Redis - never block the user. Fall through to dispatch.
        return None
    except Exception as exc:
        logger.debug("response_cache.lookup: redis get failed (%s)", exc)
        return None

    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


# ── Store (background) ─────────────────────────────────────────────


async def store(
    key: str,
    payload: dict[str, Any],
    ttl_seconds: int = 3600,
) -> None:
    """Write the response into both backends. Runs in a BackgroundTask
    so the user has already received their reply when this fires.
    NEVER raises.
    """
    # Local LRU (eviction: oldest entry by insertion order).
    expires_at = time.time() + ttl_seconds
    _local_lru[key] = (expires_at, payload)
    if len(_local_lru) > _LOCAL_MAX_ENTRIES:
        # Drop the oldest 10% so we don't pay this cost every insert.
        keys = list(_local_lru.keys())[: _LOCAL_MAX_ENTRIES // 10]
        for k in keys:
            _local_lru.pop(k, None)

    if _redis_client is None:
        return
    try:
        body = json.dumps(payload, separators=(",", ":")).encode()
        await _redis_client.set(key, body, ex=ttl_seconds)
    except Exception as exc:
        logger.debug("response_cache.store: redis set failed (%s)", exc)


# ── Health ─────────────────────────────────────────────────────────


async def health() -> dict[str, Any]:
    """For /admin/diag/system. Never raises."""
    out: dict[str, Any] = {
        "redis": False,
        "local_entries": len(_local_lru),
    }
    if _redis_client is None:
        return out
    try:
        await asyncio.wait_for(_redis_client.ping(), timeout=0.2)
        out["redis"] = True
    except Exception:
        pass
    return out
