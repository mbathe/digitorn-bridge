"""Per-process registry of which (user_id, session_id) pairs have."""
from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)

# user_id → set of session_ids the user has open Socket.IO subscriptions for.
# A user can have several tabs / devices, each joined to (potentially)
# different sessions; the set merges across them.
_user_active_sessions: dict[str, set[str]] = {}

# sid → (user_id, session_id). One row per (sid, session) join. Used
# by `clear_sid` on disconnect to know what to undo without the
# client having to send a leave event.
_sid_join: dict[str, tuple[str, str]] = {}

def mark_user_in_session(sid: str, user_id: str, session_id: str) -> None:
    """Record that this Socket.IO `sid` (owned by `user_id`) is."""
    if not user_id or not session_id or not sid:
        return
    # Strict isolation: one sid = one session, so leave any previous join first.
    prev = _sid_join.get(sid)
    if prev is not None and prev != (user_id, session_id):
        _drop_from_user_set(prev[0], prev[1])
    _sid_join[sid] = (user_id, session_id)
    _user_active_sessions.setdefault(user_id, set()).add(session_id)

def mark_user_left_session(sid: str, user_id: str, session_id: str) -> None:
    """Record that this `sid` no longer holds the join."""
    if not user_id or not session_id or not sid:
        return
    cur = _sid_join.get(sid)
    if cur == (user_id, session_id):
        _sid_join.pop(sid, None)
    _drop_from_user_set(user_id, session_id)

def clear_sid(sid: str) -> None:
    """Drop every join held by `sid` (called on disconnect)."""
    if not sid:
        return
    cur = _sid_join.pop(sid, None)
    if cur is None:
        return
    user_id, session_id = cur
    _drop_from_user_set(user_id, session_id)

def is_user_in_session(user_id: str, session_id: str) -> bool:
    """`True` when the user has at least one Socket.IO sid joined."""
    if not user_id or not session_id:
        return False
    sessions = _user_active_sessions.get(user_id)
    return bool(sessions) and session_id in sessions

def active_sessions_for_user(user_id: str) -> Iterable[str]:
    """Snapshot of the user's active session_ids. Returns an iterable."""
    return tuple(_user_active_sessions.get(user_id, ()))

def _drop_from_user_set(user_id: str, session_id: str) -> None:
    still_joined = any(
        v == (user_id, session_id) for v in _sid_join.values()
    )
    if still_joined:
        return
    sessions = _user_active_sessions.get(user_id)
    if sessions is None:
        return
    sessions.discard(session_id)
    if not sessions:
        _user_active_sessions.pop(user_id, None)
