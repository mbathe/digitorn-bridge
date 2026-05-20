"""In-progress operations registry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from digitorn.core.events.envelope import TERMINAL_STATES, OpState

if TYPE_CHECKING:
    from digitorn.core.events.envelope import SessionEvent
    from digitorn.core.kv import KeyValueBackend

logger = logging.getLogger(__name__)

# 10 minutes - long enough that any healthy op will refresh the entry
# (every emit bumps the TTL), short enough that a daemon crash mid-op
# self-evicts instead of leaking the entry forever.
_ENTRY_TTL_SECONDS = 600.0

def _entry_key(session_id: str, op_id: str) -> str:
    return f"live_ops:{session_id}:{op_id}"

def _index_key(session_id: str) -> str:
    return f"live_ops_index:{session_id}"

class LiveOpsRegistry:
    """Tracks in-progress operations per session in a key-value store."""

    def __init__(self, backend: KeyValueBackend) -> None:
        self._kv = backend

    def record(self, event: SessionEvent | None) -> None:
        """Update the registry from an event being emitted."""
        if event is None:
            return
        sid = event.session_id
        op_id = event.op_id
        if not sid or not op_id:
            return

        envelope = event.to_dict()
        if event.op_state in TERMINAL_STATES:
            self._forget(sid, op_id)
            return

        # Skip ops that never publish an in-progress state - they are
        # fire-and-forget by contract (e.g. a one-shot `status` event
        # whose op_state is already terminal/synthetic).
        if event.op_state == OpState.PENDING:
            return

        try:
            self._kv.set(
                _entry_key(sid, op_id),
                envelope,
                expire=_ENTRY_TTL_SECONDS,
            )
            self._index_add(sid, op_id)
        except Exception as exc:
            logger.debug("live_ops_record_failed sid=%s op=%s: %s", sid, op_id, exc)

    def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        """Return every active op envelope for the session."""
        if not session_id:
            return []
        try:
            ids = self._kv.get(_index_key(session_id), default=None) or []
        except Exception as exc:
            logger.debug("live_ops_index_read_failed sid=%s: %s", session_id, exc)
            return []
        if not isinstance(ids, list):
            return []

        envelopes: list[dict[str, Any]] = []
        survivors: list[str] = []
        for op_id in ids:
            try:
                env = self._kv.get(_entry_key(session_id, op_id), default=None)
            except Exception:
                env = None
            if env is None:
                continue
            envelopes.append(env)
            survivors.append(op_id)

        if len(survivors) != len(ids):
            try:
                self._kv.set(_index_key(session_id), survivors, expire=_ENTRY_TTL_SECONDS)
            except Exception as exc:
                logger.debug("live_ops best-effort block failed: %s", exc)

        return envelopes

    def clear_session(self, session_id: str) -> None:
        """Drop every active op for a session - called on session."""
        if not session_id:
            return
        try:
            ids = self._kv.get(_index_key(session_id), default=None) or []
        except Exception:
            ids = []
        if isinstance(ids, list):
            for op_id in ids:
                try:
                    self._kv.delete(_entry_key(session_id, op_id))
                except Exception as exc:
                    logger.debug("live_ops best-effort block failed: %s", exc)
        try:
            self._kv.delete(_index_key(session_id))
        except Exception as exc:
            logger.debug("live_ops best-effort block failed: %s", exc)

    def _forget(self, session_id: str, op_id: str) -> None:
        try:
            self._kv.delete(_entry_key(session_id, op_id))
        except Exception as exc:
            logger.debug("live_ops_delete_failed sid=%s op=%s: %s", session_id, op_id, exc)
        self._index_remove(session_id, op_id)

    def _index_add(self, session_id: str, op_id: str) -> None:
        try:
            existing = self._kv.get(_index_key(session_id), default=None) or []
        except Exception:
            existing = []
        if not isinstance(existing, list):
            existing = []
        if op_id in existing:
            try:
                self._kv.set(
                    _index_key(session_id), existing, expire=_ENTRY_TTL_SECONDS,
                )
            except Exception as exc:
                logger.debug("live_ops best-effort block failed: %s", exc)
            return
        existing.append(op_id)
        try:
            self._kv.set(_index_key(session_id), existing, expire=_ENTRY_TTL_SECONDS)
        except Exception as exc:
            logger.debug("live_ops_index_write_failed sid=%s: %s", session_id, exc)

    def _index_remove(self, session_id: str, op_id: str) -> None:
        try:
            existing = self._kv.get(_index_key(session_id), default=None) or []
        except Exception:
            return
        if not isinstance(existing, list) or op_id not in existing:
            return
        existing = [x for x in existing if x != op_id]
        try:
            if existing:
                self._kv.set(_index_key(session_id), existing, expire=_ENTRY_TTL_SECONDS)
            else:
                self._kv.delete(_index_key(session_id))
        except Exception as exc:
            logger.debug("live_ops best-effort block failed: %s", exc)
