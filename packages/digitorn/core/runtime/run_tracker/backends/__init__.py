"""Tracker backend registry."""

from __future__ import annotations

from typing import Any, Callable

from digitorn.core.runtime.run_tracker.protocols import TrackerBackend
from digitorn.core.runtime.run_tracker.backends.jsonfile import JsonFileBackend
from digitorn.core.runtime.run_tracker.backends.kv import KVBackend
from digitorn.core.runtime.run_tracker.backends.null import NullBackend
from digitorn.core.runtime.run_tracker.backends.postgres import PostgresBackend
from digitorn.core.runtime.run_tracker.backends.sqlite import SqliteBackend


BACKEND_REGISTRY: dict[str, Callable[..., TrackerBackend]] = {
    "postgres": PostgresBackend,
    "sqlite": SqliteBackend,
    "jsonfile": JsonFileBackend,
    "kv": KVBackend,
    "null": NullBackend,
}


def select_backend(
    name: str, config: dict[str, Any] | None = None,
) -> TrackerBackend:
    """Build a backend by registry key. `config` is the per-backend"""
    factory = BACKEND_REGISTRY.get(name)
    if factory is None:
        import logging
        logging.getLogger(__name__).warning(
            "Unknown run_tracker backend %r - falling back to null", name,
        )
        return NullBackend()
    return factory(**(config or {}))


__all__ = [
    "BACKEND_REGISTRY",
    "select_backend",
    "PostgresBackend",
    "SqliteBackend",
    "JsonFileBackend",
    "KVBackend",
    "NullBackend",
]
