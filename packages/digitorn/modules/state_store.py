"""Module state persistence — SQLite-backed store for module state snapshots.

Used by the LifecycleManager to:
  - Save module state_snapshot() on shutdown
  - Restore module state via restore_state() on startup

Consistent with PlanStateStore, PermissionStore pattern (aiosqlite).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from digitorn.modules.log import get_logger

log = get_logger(__name__)


class ModuleStateStore:
    """SQLite-backed store for module state snapshots.

    Each module's state is stored as a JSON blob keyed by module_id.
    Only the latest snapshot is kept per module.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Create table and open connection."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS module_state (
                module_id   TEXT PRIMARY KEY,
                state_json  TEXT NOT NULL,
                saved_at    REAL NOT NULL
            )
            """
        )
        await self._db.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def save(self, module_id: str, state: dict[str, Any]) -> None:
        """Persist a module's state snapshot (upsert)."""
        import time

        if self._db is None:
            return
        state_json = json.dumps(state, default=str)
        await self._db.execute(
            """
            INSERT INTO module_state (module_id, state_json, saved_at)
            VALUES (?, ?, ?)
            ON CONFLICT(module_id) DO UPDATE SET
                state_json = excluded.state_json,
                saved_at = excluded.saved_at
            """,
            (module_id, state_json, time.time()),
        )
        await self._db.commit()

    async def load(self, module_id: str) -> dict[str, Any] | None:
        """Load a module's saved state, or None if not found."""
        if self._db is None:
            return None
        cursor = await self._db.execute(
            "SELECT state_json FROM module_state WHERE module_id = ?",
            (module_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    async def delete(self, module_id: str) -> None:
        """Remove a module's saved state."""
        if self._db is None:
            return
        await self._db.execute(
            "DELETE FROM module_state WHERE module_id = ?", (module_id,)
        )
        await self._db.commit()

    async def list_all(self) -> list[str]:
        """Return all module IDs that have saved state."""
        if self._db is None:
            return []
        cursor = await self._db.execute("SELECT module_id FROM module_state")
        rows = await cursor.fetchall()
        return [r[0] for r in rows]
