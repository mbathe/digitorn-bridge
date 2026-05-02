"""Per-process registry of which (user_id, session_id) pairs have a
LIVE Socket.IO subscription right now.

Owned by the socket layer, read by the inbox producer.

Why this exists
---------------

The inbox producer must NOT promote an event into a notification row
when the same user is currently watching that session in their UI -
they already see the event live, sending them a "ding" on top is just
noise. The producer therefore needs an authoritative "is user X
currently joined to session Y?" answer for every envelope it processes.

The Socket.IO room layer is the source of truth: when a client calls
``join_session``, ``socketio_bus.on_join_session`` calls
:func:`mark_user_in_session` here; when the client calls
``leave_session`` or disconnects, the matching helper removes them.
Producers and any other in-process consumer reads the live state via
:func:`is_user_in_session`.

Concurrency
-----------

The handlers in ``socketio_bus`` run in the asyncio event loop. The
producer also runs in the same loop (it's registered as an
in-process handler on ``SessionBus``). All accesses are therefore
serialised by the single-threaded scheduler - no lock needed. The
data structures are plain dicts/sets.
"""
from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)


# user_id → set of session_ids the user has open Socket.IO subscriptions for.
# A user can have several tabs / devices, each joined to (potentially)
# different sessions; the set merges across them.
_user_active_sessions: dict[str, set[str]] = {}

# sid → (user_id, session_id). One row per (sid, session) join. Used
# by ``clear_sid`` on disconnect to know what to undo without the
# client having to send a leave event.
_sid_join: dict[str, tuple[str, str]] = {}


def mark_user_in_session(sid: str, user_id: str, session_id: str) -> None:
    """Record that this Socket.IO ``sid`` (owned by ``user_id``) is
    now joined to ``session_id``. Idempotent - safe to call on
    re-join."""
    if not user_id or not session_id or not sid:
        return
    # If this sid was previously joined to a DIFFERENT session,
    # implicitly leave it first. Reflects strict isolation: one sid =
    # at most one session at a time (matches the room layer's
    # auto-leave-others behaviour in ``on_join_session``).
    prev = _sid_join.get(sid)
    if prev is not None and prev != (user_id, session_id):
        _drop_from_user_set(prev[0], prev[1])
    _sid_join[sid] = (user_id, session_id)
    _user_active_sessions.setdefault(user_id, set()).add(session_id)


def mark_user_left_session(sid: str, user_id: str, session_id: str) -> None:
    """Record that this ``sid`` no longer holds the join for
    ``session_id``. The user's set still contains the session if
    another sid (different tab / device) is still joined."""
    if not user_id or not session_id or not sid:
        return
    cur = _sid_join.get(sid)
    if cur == (user_id, session_id):
        _sid_join.pop(sid, None)
    _drop_from_user_set(user_id, session_id)


def clear_sid(sid: str) -> None:
    """Drop every join held by ``sid`` (called on disconnect)."""
    if not sid:
        return
    cur = _sid_join.pop(sid, None)
    if cur is None:
        return
    user_id, session_id = cur
    _drop_from_user_set(user_id, session_id)


def is_user_in_session(user_id: str, session_id: str) -> bool:
    """``True`` when the user has at least one Socket.IO sid joined
    to this session right now.

    Producers call this before promoting an event into a notification
    row - if the user is live on the session, they already see the
    event in their UI and a notif would be duplicate noise.
    """
    if not user_id or not session_id:
        return False
    sessions = _user_active_sessions.get(user_id)
    return bool(sessions) and session_id in sessions


def active_sessions_for_user(user_id: str) -> Iterable[str]:
    """Snapshot of the user's active session_ids. Returns an iterable
    over a copy so the caller can iterate while joins/leaves happen."""
    return tuple(_user_active_sessions.get(user_id, ()))


def _drop_from_user_set(user_id: str, session_id: str) -> None:
    """Remove ``session_id`` from the user's set, but only if no
    OTHER sid still holds a join for the same (user, session) pair."""
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
