"""Key-value backend abstraction for sessions, rate limiting, and caches."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)



@runtime_checkable
class KeyValueBackend(Protocol):
    """Minimal key-value store interface."""

    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any, expire: float | None = None) -> None: ...

    def delete(self, key: str) -> bool: ...

    def incr(self, key: str, expire: float | None = None) -> int: ...

    def close(self) -> None: ...



class DiskCacheBackend:
    """SQLite-backed key-value store via DiskCache."""

    def __init__(
        self,
        directory: str | Path | None = None,
        size_limit: int = 2**30,
    ) -> None:
        import diskcache

        self._dir = Path(directory) if directory else Path.home() / ".digitorn" / "kv"
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._cache = diskcache.Cache(
            str(self._dir),
            eviction_policy="least-recently-used",
            size_limit=size_limit,
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self._cache.get(key, default)

    def set(self, key: str, value: Any, expire: float | None = None) -> None:
        self._cache.set(key, value, expire=expire)

    def delete(self, key: str) -> bool:
        return self._cache.pop(key) is not None

    def incr(self, key: str, expire: float | None = None) -> int:
        # transact() takes an exclusive SQLite lock => TOCTOU-safe
        with self._cache.transact():
            val = self._cache.get(key, 0) + 1
            self._cache.set(key, val, expire=expire)
            return val

    def close(self) -> None:
        self._cache.close()

    @property
    def volume(self) -> int:
        return self._cache.volume()

    def __len__(self) -> int:
        return len(self._cache)



class RedisBackend:
    """Redis-backed key-value store for multi-host deployments."""

    def __init__(self, url: str = "redis://localhost:6379/0") -> None:
        import redis

        self._redis = redis.Redis.from_url(url, decode_responses=False)
        self._url = url
        self._redis.ping()
        logger.info("redis_backend_connected: %s", self._mask_url(url))

    def get(self, key: str, default: Any = None) -> Any:
        raw = self._redis.get(key)
        if raw is None:
            return default
        return self._deserialize(raw)

    def set(self, key: str, value: Any, expire: float | None = None) -> None:
        data = self._serialize(value)
        if expire is not None:
            self._redis.setex(key, int(expire), data)
        else:
            self._redis.set(key, data)

    def delete(self, key: str) -> bool:
        return self._redis.delete(key) > 0

    def incr(self, key: str, expire: float | None = None) -> int:
        # Redis INCR is natively atomic
        val = self._redis.incr(key)
        if expire is not None and val == 1:
            self._redis.expire(key, int(expire))
        return val

    def close(self) -> None:
        self._redis.close()

    @staticmethod
    def _serialize(value: Any) -> bytes:
        import json
        import dataclasses

        def _encode(obj: Any) -> Any:
            if isinstance(obj, bytes):
                import base64
                return {"__bytes__": base64.b64encode(obj).decode("ascii")}
            if isinstance(obj, (set, frozenset)):
                return {"__set__": sorted(obj, key=str)}
            if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
                return {
                    "__dataclass__": f"{type(obj).__module__}.{type(obj).__qualname__}",
                    "__fields__": dataclasses.asdict(obj),
                }
            raise TypeError(f"Cannot serialize {type(obj).__name__}")

        return json.dumps(value, default=_encode, ensure_ascii=False).encode("utf-8")

    # ONLY codebase-internal types; never arbitrary classes
    _SAFE_DATACLASS_TYPES: dict[str, type] | None = None

    @classmethod
    def _get_safe_types(cls) -> dict[str, type]:
        if cls._SAFE_DATACLASS_TYPES is None:
            cls._SAFE_DATACLASS_TYPES = {}
            try:
                from digitorn.core.app.sessions import ConversationSession
                fqn = f"{ConversationSession.__module__}.{ConversationSession.__qualname__}"
                cls._SAFE_DATACLASS_TYPES[fqn] = ConversationSession
            except ImportError:
                pass
        return cls._SAFE_DATACLASS_TYPES

    @staticmethod
    def _deserialize(data: bytes) -> Any:
        import json

        def _decode(obj: dict) -> Any:
            if "__bytes__" in obj:
                import base64
                return base64.b64decode(obj["__bytes__"])
            if "__set__" in obj:
                return set(obj["__set__"])
            if "__dataclass__" in obj:
                fqn = obj["__dataclass__"]
                safe = RedisBackend._get_safe_types()
                cls = safe.get(fqn)
                if cls is None:
                    logger.warning("redis_blocked_dataclass: %s", fqn)
                    return obj["__fields__"]
                return cls(**obj["__fields__"])
            return obj

        return json.loads(data, object_hook=_decode)

    @staticmethod
    def _mask_url(url: str) -> str:
        if "@" in url and ":" in url.split("@")[0]:
            parts = url.split("@")
            creds = parts[0]
            scheme_user = creds.rsplit(":", 1)[0]
            return f"{scheme_user}:****@{parts[1]}"
        return url


class ResilientRedisBackend:
    """Redis backend with automatic DiskCache fallback on failure."""

    def __init__(
        self,
        url: str,
        *,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
    ) -> None:
        import time as _time

        self._url = url
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._failures = 0
        self._circuit_open_until: float = 0.0
        self._time = _time

        self._fallback = DiskCacheBackend()
        try:
            self._redis = RedisBackend(url)
            self._redis_available = True
        except Exception as exc:
            logger.warning("Redis unavailable at startup, using DiskCache fallback: %s", exc)
            self._redis = None  # type: ignore[assignment]
            self._redis_available = False
            self._circuit_open_until = self._time.time() + self._recovery_timeout

    @property
    def _is_circuit_open(self) -> bool:
        if self._failures < self._failure_threshold:
            return False
        if self._time.time() >= self._circuit_open_until:
            return False
        return True

    def _on_success(self) -> None:
        if self._failures > 0:
            logger.info("redis_circuit_closed: recovered")
        self._failures = 0

    def _on_failure(self, exc: Exception) -> None:
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._circuit_open_until = self._time.time() + self._recovery_timeout
            logger.warning(
                "redis_circuit_open: %d failures, fallback for %ds - %s",
                self._failures, int(self._recovery_timeout), exc,
            )

    def _try_reconnect(self) -> bool:
        try:
            self._redis = RedisBackend(self._url)
            self._redis_available = True
            self._on_success()
            return True
        except Exception as exc:
            self._on_failure(exc)
            return False

    def get(self, key: str, default: Any = None) -> Any:
        if self._is_circuit_open or not self._redis_available:
            return self._fallback.get(key, default)
        try:
            result = self._redis.get(key, default)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure(exc)
            return self._fallback.get(key, default)

    def set(self, key: str, value: Any, expire: float | None = None) -> None:
        if self._is_circuit_open or not self._redis_available:
            self._fallback.set(key, value, expire=expire)
            return
        try:
            self._redis.set(key, value, expire=expire)
            self._on_success()
        except Exception as exc:
            self._on_failure(exc)
            self._fallback.set(key, value, expire=expire)

    def delete(self, key: str) -> bool:
        if self._is_circuit_open or not self._redis_available:
            return self._fallback.delete(key)
        try:
            result = self._redis.delete(key)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure(exc)
            return self._fallback.delete(key)

    def incr(self, key: str, expire: float | None = None) -> int:
        if self._is_circuit_open or not self._redis_available:
            return self._fallback.incr(key, expire=expire)
        try:
            result = self._redis.incr(key, expire=expire)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure(exc)
            return self._fallback.incr(key, expire=expire)

    def close(self) -> None:
        if self._redis_available and self._redis is not None:
            try:
                self._redis.close()
            except Exception:
                logger.debug("redis close error", exc_info=True)
        self._fallback.close()


class AsyncKeyValueBackend:
    """Async wrapper around any sync KeyValueBackend."""

    def __init__(self, backend: KeyValueBackend) -> None:
        self._backend = backend

    async def get(self, key: str, default: Any = None) -> Any:
        import asyncio
        return await asyncio.to_thread(self._backend.get, key, default)

    async def set(self, key: str, value: Any, expire: float | None = None) -> None:
        import asyncio
        await asyncio.to_thread(self._backend.set, key, value, expire)

    async def delete(self, key: str) -> bool:
        import asyncio
        return await asyncio.to_thread(self._backend.delete, key)

    async def incr(self, key: str, expire: float | None = None) -> int:
        import asyncio
        return await asyncio.to_thread(self._backend.incr, key, expire)

    async def close(self) -> None:
        import asyncio
        await asyncio.to_thread(self._backend.close)

    @property
    def sync(self) -> KeyValueBackend:
        return self._backend


class NativeAsyncRedisBackend:
    """True async Redis backend using redis.asyncio."""

    def __init__(self, url: str = "redis://localhost:6379/0") -> None:
        self._url = url
        self._redis: Any = None

    async def _ensure_connected(self) -> Any:
        if self._redis is None:
            try:
                import redis.asyncio as aioredis
            except ImportError:
                raise ImportError(
                    "redis[async] required for NativeAsyncRedisBackend. "
                    "Install with: pip install 'redis[async]'"
                )
            self._redis = aioredis.from_url(
                self._url, decode_responses=False,
            )
            await self._redis.ping()
            logger.info("async_redis_connected: %s", RedisBackend._mask_url(self._url))
        return self._redis

    async def get(self, key: str, default: Any = None) -> Any:
        r = await self._ensure_connected()
        raw = await r.get(key)
        if raw is None:
            return default
        return RedisBackend._deserialize(raw)

    async def set(self, key: str, value: Any, expire: float | None = None) -> None:
        r = await self._ensure_connected()
        data = RedisBackend._serialize(value)
        if expire is not None:
            await r.setex(key, int(expire), data)
        else:
            await r.set(key, data)

    async def delete(self, key: str) -> bool:
        r = await self._ensure_connected()
        return (await r.delete(key)) > 0

    async def incr(self, key: str, expire: float | None = None) -> int:
        r = await self._ensure_connected()
        val = await r.incr(key)
        if expire is not None and val == 1:
            await r.expire(key, int(expire))
        return val

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    @property
    def sync(self) -> KeyValueBackend:
        raise RuntimeError(
            "NativeAsyncRedisBackend has no sync interface. "
            "Use AsyncKeyValueBackend(RedisBackend(...)) if you need sync access."
        )


def create_async_backend(
    url: str | Path | None = None,
    *,
    directory: str | Path | None = None,
    size_limit: int = 2**30,
    native_async: bool = True,
) -> AsyncKeyValueBackend | NativeAsyncRedisBackend:
    """Create an async backend."""
    url_str = str(url) if url is not None else None

    if url_str and url_str.startswith(("redis://", "rediss://")) and native_async:
        return NativeAsyncRedisBackend(url_str)

    sync_backend = create_backend(url, directory=directory, size_limit=size_limit)
    return AsyncKeyValueBackend(sync_backend)


def create_backend(
    url: str | Path | None = None,
    *,
    directory: str | Path | None = None,
    size_limit: int = 2**30,
) -> KeyValueBackend:
    """Create a backend from a URL or directory path."""
    url_str = str(url) if url is not None else None

    if url_str and url_str.startswith(("redis://", "rediss://")):
        return ResilientRedisBackend(url_str)

    disk_dir = url_str or directory
    return DiskCacheBackend(directory=disk_dir, size_limit=size_limit)
