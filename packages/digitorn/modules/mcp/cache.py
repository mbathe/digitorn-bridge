"""Smart cache for MCP tool results."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from digitorn.modules.base import ActionResult

logger = logging.getLogger(__name__)

@dataclass
class _CacheEntry:
    """Single cached tool result."""

    result: ActionResult
    created_at: float

    def is_expired(self, ttl: float) -> bool:
        if ttl <= 0:
            return True
        return (time.monotonic() - self.created_at) > ttl

    def age_ms(self) -> float:
        return (time.monotonic() - self.created_at) * 1000

class MCPToolCache:
    """Per-server MCP tool result cache with TTL and LRU eviction."""

    def __init__(
        self,
        default_ttl: float = 300.0,
        default_max_size: int = 200,
    ) -> None:
        self._default_ttl = default_ttl
        self._default_max_size = default_max_size
        self._stores: dict[str, dict[str, _CacheEntry]] = {}
        self._server_ttl: dict[str, float] = {}
        self._server_max_size: dict[str, int] = {}
        self._cacheable_tools: dict[str, set[str]] = {}
        self._hits = 0
        self._misses = 0
        self._invalidations = 0
        self._evictions = 0

    def configure_server(
        self,
        server_id: str,
        *,
        ttl: float | None = None,
        max_size: int | None = None,
        cacheable_tools: list[str] | None = None,
    ) -> None:
        """Set per-server TTL / max_size / cacheable_tools override."""
        if ttl is not None:
            self._server_ttl[server_id] = ttl
        if max_size is not None:
            self._server_max_size[server_id] = max_size
        if cacheable_tools is not None:
            self._cacheable_tools[server_id] = set(cacheable_tools)

    def is_cacheable(self, server_id: str, tool_name: str) -> bool:
        """Return True only if *tool_name* is explicitly whitelisted."""
        allowed = self._cacheable_tools.get(server_id)
        if not allowed:
            return False
        return tool_name in allowed

    def _ttl_for(self, server_id: str) -> float:
        return self._server_ttl.get(server_id, self._default_ttl)

    def _max_size_for(self, server_id: str) -> int:
        return self._server_max_size.get(server_id, self._default_max_size)

    @staticmethod
    def make_key(tool_name: str, params: dict[str, Any]) -> str:
        """Deterministic cache key from tool name + sorted params."""
        raw = json.dumps(
            {"t": tool_name, "p": params},
            sort_keys=True,
            default=str,
            ensure_ascii=False,
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def get(self, server_id: str, cache_key: str) -> _CacheEntry | None:
        """Return cached entry if valid, else `None`."""
        store = self._stores.get(server_id)
        if store is None:
            self._misses += 1
            return None

        entry = store.get(cache_key)
        if entry is None:
            self._misses += 1
            return None

        if entry.is_expired(self._ttl_for(server_id)):
            del store[cache_key]
            self._misses += 1
            return None

        self._hits += 1
        return entry

    def store(
        self,
        server_id: str,
        cache_key: str,
        result: ActionResult,
    ) -> None:
        """Store a result.  Evicts oldest entry if at capacity."""
        store = self._stores.setdefault(server_id, {})
        max_size = self._max_size_for(server_id)

        if len(store) >= max_size:
            oldest_key = min(store, key=lambda k: store[k].created_at)
            del store[oldest_key]
            self._evictions += 1

        store[cache_key] = _CacheEntry(
            result=result,
            created_at=time.monotonic(),
        )

    def invalidate_server(self, server_id: str) -> int:
        """Clear ALL entries for *server_id*.  Returns count cleared."""
        store = self._stores.pop(server_id, None)
        if store is None:
            return 0
        count = len(store)
        self._invalidations += count
        return count

    def invalidate_all(self) -> None:
        """Clear everything."""
        total = sum(len(s) for s in self._stores.values())
        self._stores.clear()
        self._invalidations += total

    def info(self) -> dict[str, Any]:
        """Debug stats."""
        return {
            "hits": self._hits,
            "misses": self._misses,
            "invalidations": self._invalidations,
            "evictions": self._evictions,
            "servers": {
                sid: {
                    "entries": len(store),
                    "ttl": self._ttl_for(sid),
                }
                for sid, store in self._stores.items()
            },
        }
