"""Database module - Schema cache with TTL for fast introspection."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class _CacheEntry:
    """A single cached schema snapshot for one connection."""

    tables: list[Any]
    schema_info: Any | None
    table_map: dict[str, Any]
    columns_map: dict[str, list[Any]]
    stats_map: dict[str, Any]
    created_at: float = field(default_factory=time.monotonic)

    def is_expired(self, ttl: float) -> bool:
        if ttl <= 0:
            return True
        return (time.monotonic() - self.created_at) > ttl

class SchemaCache:
    """Per-connection schema cache with configurable TTL."""

    def __init__(self, ttl: float = 300.0) -> None:
        self._ttl = ttl
        self._entries: dict[str, _CacheEntry] = {}

    @property
    def ttl(self) -> float:
        return self._ttl

    @ttl.setter
    def ttl(self, value: float) -> None:
        self._ttl = value

    def has(self, connection_id: str) -> bool:
        """Check if we have a valid (non-expired) cache for this connection."""
        entry = self._entries.get(connection_id)
        if entry is None:
            return False
        if entry.is_expired(self._ttl):
            del self._entries[connection_id]
            return False
        return True

    def store(
        self,
        connection_id: str,
        *,
        tables: list[Any],
        schema_info: Any | None = None,
    ) -> None:
        """Cache table metadata for a connection."""
        table_map = {t.name: t for t in tables}
        columns_map = {t.name: list(t.columns) for t in tables}
        self._entries[connection_id] = _CacheEntry(
            tables=tables,
            schema_info=schema_info,
            table_map=table_map,
            columns_map=columns_map,
            stats_map={},
        )

    def store_stats(self, connection_id: str, table: str, stats: Any) -> None:
        """Cache table stats (row count etc.)."""
        entry = self._entries.get(connection_id)
        if entry and not entry.is_expired(self._ttl):
            entry.stats_map[table] = stats

    def get_tables(self, connection_id: str) -> list[Any] | None:
        """Get cached tables list, or None if not cached/expired."""
        entry = self._entries.get(connection_id)
        if entry is None or entry.is_expired(self._ttl):
            return None
        return entry.tables

    def get_table(self, connection_id: str, table: str) -> Any | None:
        """Get a single cached TableInfo by name (O(1) lookup)."""
        entry = self._entries.get(connection_id)
        if entry is None or entry.is_expired(self._ttl):
            return None
        return entry.table_map.get(table)

    def get_columns(self, connection_id: str, table: str) -> list[Any] | None:
        """Get cached columns for a table."""
        entry = self._entries.get(connection_id)
        if entry is None or entry.is_expired(self._ttl):
            return None
        return entry.columns_map.get(table)

    def get_schema_info(self, connection_id: str) -> Any | None:
        """Get cached full SchemaInfo."""
        entry = self._entries.get(connection_id)
        if entry is None or entry.is_expired(self._ttl):
            return None
        return entry.schema_info

    def get_stats(self, connection_id: str, table: str) -> Any | None:
        """Get cached table stats."""
        entry = self._entries.get(connection_id)
        if entry is None or entry.is_expired(self._ttl):
            return None
        return entry.stats_map.get(table)

    def invalidate(self, connection_id: str) -> None:
        """Invalidate cache for a connection (e.g. after DDL)."""
        self._entries.pop(connection_id, None)

    def invalidate_all(self) -> None:
        """Clear all cached schemas."""
        self._entries.clear()

    def info(self) -> dict[str, Any]:
        """Cache stats for debugging/dashboard."""
        now = time.monotonic()
        entries = {}
        for cid, entry in self._entries.items():
            age = now - entry.created_at
            entries[cid] = {
                "table_count": len(entry.tables),
                "age_seconds": round(age, 1),
                "expired": entry.is_expired(self._ttl),
                "has_schema_info": entry.schema_info is not None,
                "stats_cached": len(entry.stats_map),
            }
        return {
            "ttl": self._ttl,
            "connections_cached": len(entries),
            "entries": entries,
        }
