"""Database sync strategies — detect row-level changes without Kafka.

Three pure-SQL strategies:
- updated_at: poll WHERE updated_at > watermark
- changelog: auto-created triggers + changelog table
- notify: PostgreSQL LISTEN/NOTIFY (optimization over updated_at)
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ChangeRecord:
    table: str
    row_id: str
    operation: str  # INSERT | UPDATE | DELETE
    timestamp: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)


class SyncStrategy(ABC):
    @abstractmethod
    async def poll(self, bus: Any, connection_id: str, table: str, columns: list[str]) -> list[ChangeRecord]:
        ...

    @abstractmethod
    def get_watermark(self) -> Any:
        ...

    @abstractmethod
    def set_watermark(self, value: Any) -> None:
        ...

    def to_dict(self) -> dict[str, Any]:
        return {"watermark": self.get_watermark()}

    def restore(self, data: dict[str, Any]) -> None:
        wm = data.get("watermark")
        if wm is not None:
            self.set_watermark(wm)


class UpdatedAtSync(SyncStrategy):
    """Poll rows where updated_at > last watermark."""

    def __init__(self) -> None:
        self._watermark: str = "1970-01-01T00:00:00Z"

    async def poll(
        self, bus: Any, connection_id: str, table: str, columns: list[str],
    ) -> list[ChangeRecord]:
        col_list = ", ".join(columns) if columns else "*"
        query = (
            f"SELECT {col_list}, updated_at FROM {table} "
            f"WHERE updated_at > $1 ORDER BY updated_at LIMIT 1000"
        )
        try:
            result = await bus.call("database", "fetch_results", {
                "connection_id": connection_id,
                "query": query,
                "params": [self._watermark],
            })
        except Exception as exc:
            logger.warning("updated_at poll failed for %s.%s: %s", connection_id, table, exc)
            return []

        if not result or not result.success or not result.data:
            return []

        rows = result.data.get("rows", [])
        changes = []
        max_ts = self._watermark

        for row in rows:
            ts = str(row.get("updated_at", ""))
            if ts > max_ts:
                max_ts = ts
            row_id = str(row.get("id", row.get(columns[0], ""))) if columns else ""
            changes.append(ChangeRecord(
                table=table, row_id=row_id, operation="UPSERT",
                timestamp=time.time(), data=row,
            ))

        if max_ts != self._watermark:
            self._watermark = max_ts

        return changes

    def get_watermark(self) -> Any:
        return self._watermark

    def set_watermark(self, value: Any) -> None:
        self._watermark = str(value)


class ChangelogSync(SyncStrategy):
    """Poll a _rag_changelog table populated by auto-created triggers."""

    def __init__(self) -> None:
        self._last_seq: int = 0

    async def setup_triggers(
        self, bus: Any, connection_id: str, tables: list[str],
    ) -> None:
        """Create changelog table and per-table triggers."""
        create_table = """
            CREATE TABLE IF NOT EXISTS _rag_changelog (
                seq BIGSERIAL PRIMARY KEY,
                table_name TEXT NOT NULL,
                row_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """
        try:
            await bus.call("database", "execute_query", {
                "connection_id": connection_id, "query": create_table,
            })
        except Exception as exc:
            logger.warning("Failed to create _rag_changelog: %s", exc)
            return

        for table in tables:
            safe = table.replace("'", "").replace('"', "").replace(";", "")
            func_sql = f"""
                CREATE OR REPLACE FUNCTION _rag_notify_{safe}() RETURNS trigger AS $$
                BEGIN
                    INSERT INTO _rag_changelog (table_name, row_id, operation)
                    VALUES (TG_TABLE_NAME, COALESCE(NEW.id, OLD.id)::text, TG_OP);
                    RETURN COALESCE(NEW, OLD);
                END;
                $$ LANGUAGE plpgsql
            """
            trigger_sql = f"""
                DROP TRIGGER IF EXISTS _rag_trg_{safe} ON {safe};
                CREATE TRIGGER _rag_trg_{safe}
                    AFTER INSERT OR UPDATE OR DELETE ON {safe}
                    FOR EACH ROW EXECUTE FUNCTION _rag_notify_{safe}()
            """
            try:
                await bus.call("database", "execute_query", {
                    "connection_id": connection_id, "query": func_sql,
                })
                await bus.call("database", "execute_query", {
                    "connection_id": connection_id, "query": trigger_sql,
                })
                logger.info("Created changelog trigger for %s.%s", connection_id, safe)
            except Exception as exc:
                logger.warning("Failed to create trigger for %s: %s", safe, exc)

    async def poll(
        self, bus: Any, connection_id: str, table: str, columns: list[str],
    ) -> list[ChangeRecord]:
        query = (
            "SELECT seq, table_name, row_id, operation, created_at "
            "FROM _rag_changelog WHERE seq > $1 AND table_name = $2 "
            "ORDER BY seq LIMIT 1000"
        )
        try:
            result = await bus.call("database", "fetch_results", {
                "connection_id": connection_id,
                "query": query,
                "params": [self._last_seq, table],
            })
        except Exception as exc:
            logger.warning("changelog poll failed: %s", exc)
            return []

        if not result or not result.success or not result.data:
            return []

        rows = result.data.get("rows", [])
        changes = []
        for row in rows:
            seq = row.get("seq", 0)
            if seq > self._last_seq:
                self._last_seq = seq
            changes.append(ChangeRecord(
                table=row.get("table_name", table),
                row_id=str(row.get("row_id", "")),
                operation=row.get("operation", "UNKNOWN"),
                timestamp=time.time(),
            ))
        return changes

    async def prune(self, bus: Any, connection_id: str, hours: int = 24) -> None:
        query = f"DELETE FROM _rag_changelog WHERE created_at < now() - interval '{hours} hours'"
        try:
            await bus.call("database", "execute_query", {
                "connection_id": connection_id, "query": query,
            })
        except Exception:
            pass

    def get_watermark(self) -> Any:
        return self._last_seq

    def set_watermark(self, value: Any) -> None:
        self._last_seq = int(value)


class NotifySync(SyncStrategy):
    """PostgreSQL LISTEN/NOTIFY — wakes the poller immediately.

    Falls back to updated_at between notifications for reliability.
    """

    def __init__(self) -> None:
        self._inner = UpdatedAtSync()
        self._pending_tables: set[str] = set()

    def on_notify(self, table: str) -> None:
        self._pending_tables.add(table)

    def has_pending(self, table: str) -> bool:
        return table in self._pending_tables

    def clear_pending(self, table: str) -> None:
        self._pending_tables.discard(table)

    async def poll(
        self, bus: Any, connection_id: str, table: str, columns: list[str],
    ) -> list[ChangeRecord]:
        self.clear_pending(table)
        return await self._inner.poll(bus, connection_id, table, columns)

    def get_watermark(self) -> Any:
        return self._inner.get_watermark()

    def set_watermark(self, value: Any) -> None:
        self._inner.set_watermark(value)


def create_sync(strategy: str) -> SyncStrategy:
    if strategy == "changelog":
        return ChangelogSync()
    if strategy == "notify":
        return NotifySync()
    return UpdatedAtSync()
