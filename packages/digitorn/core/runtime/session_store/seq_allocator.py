"""Monotonic sequence number allocator."""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


SeedLoader = Callable[[str], int]


class SeqAllocator:
    """Process-wide allocator. One instance per `SessionStore`."""

    def __init__(self, seed_loader: SeedLoader) -> None:
        self._seq: dict[str, int] = {}
        self._lock = threading.Lock()
        self._seed_loader = seed_loader

    @staticmethod
    def _scope_key(*, user_id: str, session_id: Optional[str]) -> str:
        if session_id:
            return f"session::{session_id}"
        return f"user::{user_id}"

    def next(self, *, user_id: str = "", session_id: Optional[str] = None) -> int:
        """Allocate the next monotonic seq for the given scope."""
        scope_key = self._scope_key(user_id=user_id, session_id=session_id)
        with self._lock:
            if scope_key not in self._seq:
                seed = 0
                try:
                    seed = int(self._seed_loader(scope_key))
                except Exception as exc:
                    logger.warning(
                        "seq_seed_failed scope=%s err=%s defaulting to 0",
                        scope_key, exc,
                    )
                    seed = 0
                self._seq[scope_key] = seed
            self._seq[scope_key] += 1
            return self._seq[scope_key]

    def latest(self, *, user_id: str = "", session_id: Optional[str] = None) -> int:
        """Read the current high-water mark for a scope WITHOUT"""
        scope_key = self._scope_key(user_id=user_id, session_id=session_id)
        with self._lock:
            return self._seq.get(scope_key, 0)

    def reset_for_tests(self) -> None:
        """Wipe internal state. Test-only helper: production code must"""
        with self._lock:
            self._seq.clear()

    def force_seed(self, scope_key: str, value: int) -> None:
        """Force the counter to a specific value. Used by recovery"""
        with self._lock:
            self._seq[scope_key] = int(value)
