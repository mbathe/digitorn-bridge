"""Digitorn — Module state persistence.

Provides a simple key-value store for persisting module state snapshots
across daemon restarts. The default implementation uses JSON files on disk.

Usage::

    store = JsonStateStore(Path("~/.digitorn/state"))
    await store.save("index", {"entries": {...}, "sources": {...}})
    state = await store.load("index")
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class StateStore(ABC):
    """Abstract state store for module snapshots."""

    @abstractmethod
    async def save(self, module_id: str, state: dict[str, Any]) -> None:
        """Persist a module's state snapshot."""

    @abstractmethod
    async def load(self, module_id: str) -> dict[str, Any] | None:
        """Load a module's saved state. Returns None if no state exists."""

    @abstractmethod
    async def delete(self, module_id: str) -> None:
        """Delete a module's saved state."""

    @abstractmethod
    async def list_modules(self) -> list[str]:
        """List all module IDs that have saved state."""


class JsonStateStore(StateStore):
    """File-based state store using one JSON file per module.

    State files are stored as ``{base_dir}/{module_id}.state.json``.
    All I/O is offloaded to threads to avoid blocking the event loop.
    """

    def __init__(self, base_dir: Path | str) -> None:
        self._base_dir = Path(base_dir)

    def _path_for(self, module_id: str) -> Path:
        """Return the state file path for a module."""
        safe_id = module_id.replace("/", "_").replace("..", "_")
        return self._base_dir / f"{safe_id}.state.json"

    async def save(self, module_id: str, state: dict[str, Any]) -> None:
        path = self._path_for(module_id)
        try:
            await asyncio.to_thread(self._write, path, state)
            await logger.adebug("state_saved", module_id=module_id, path=str(path))
        except Exception as exc:
            await logger.aerror(
                "state_save_failed", module_id=module_id, error=str(exc),
            )
            raise

    async def load(self, module_id: str) -> dict[str, Any] | None:
        path = self._path_for(module_id)
        try:
            data = await asyncio.to_thread(self._read, path)
            if data is not None:
                await logger.adebug("state_loaded", module_id=module_id)
            return data
        except Exception as exc:
            await logger.aerror(
                "state_load_failed", module_id=module_id, error=str(exc),
            )
            return None

    async def delete(self, module_id: str) -> None:
        path = self._path_for(module_id)
        try:
            await asyncio.to_thread(path.unlink, True)
            await logger.adebug("state_deleted", module_id=module_id)
        except Exception as exc:
            await logger.awarning(
                "state_delete_failed", module_id=module_id, error=str(exc),
            )

    async def list_modules(self) -> list[str]:
        if not self._base_dir.exists():
            return []
        return [
            p.stem.removesuffix(".state")
            for p in self._base_dir.glob("*.state.json")
        ]

    @staticmethod
    def _write(path: Path, state: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, default=str, indent=2), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        return json.loads(text)


class InMemoryStateStore(StateStore):
    """In-memory state store for testing."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    async def save(self, module_id: str, state: dict[str, Any]) -> None:
        self._data[module_id] = state

    async def load(self, module_id: str) -> dict[str, Any] | None:
        return self._data.get(module_id)

    async def delete(self, module_id: str) -> None:
        self._data.pop(module_id, None)

    async def list_modules(self) -> list[str]:
        return list(self._data.keys())
