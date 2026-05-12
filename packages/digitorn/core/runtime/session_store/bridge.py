"""Bridge: convert ``history.record(...)`` calls into ``Event``
objects and route them to the in-memory ``SessionStore``.

Why a bridge: the daemon already routes every persistence call through
``digitorn.core.history.record()``. Adding a fan-out hook in that
function lets every event land in the SessionStore in parallel with
(or in place of) the legacy ``history_log`` Postgres write path -
without touching ``agent_loop.py`` or any other call site.

Modes:
  * ``shadow``   -- write to BOTH backends. Use during transition to
    diff outputs and validate byte-identical reconstruction.
  * ``primary``  -- write ONLY to SessionStore. The legacy DB path is
    skipped entirely. Use after shadow validation passes.

Default mode is ``off`` (no bridge). Operators flip via env var:
  ``DIGITORN_SESSION_STORE_MODE=off|shadow|primary``

This module has no daemon imports. Tests run standalone.
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from typing import Any

from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event

logger = logging.getLogger(__name__)


class BridgeMode(str, Enum):
    OFF = "off"
    SHADOW = "shadow"
    PRIMARY = "primary"


def resolve_mode_from_env() -> BridgeMode:
    raw = os.environ.get("DIGITORN_SESSION_STORE_MODE", "primary").lower().strip()
    try:
        return BridgeMode(raw)
    except ValueError:
        logger.warning(
            "DIGITORN_SESSION_STORE_MODE=%r is invalid; falling back to PRIMARY",
            raw,
        )
        return BridgeMode.PRIMARY


class SessionStoreBridge:
    """Converts ``history.record`` kwargs into Event + append_event.

    Caller-facing API mirrors the kwargs of ``digitorn.core.history.record``
    so the bridge can be invoked from anywhere that already calls
    ``record()`` -- including the legacy ``history_writer.enqueue``
    path on shadow mode.
    """

    def __init__(
        self,
        store: InMemorySessionStore,
        *,
        mode: BridgeMode = BridgeMode.SHADOW,
    ) -> None:
        self._store = store
        self.mode = mode
        self.dropped_no_session: int = 0
        self.dropped_unopened: int = 0
        self.routed: int = 0

    @property
    def store(self) -> InMemorySessionStore:
        return self._store

    async def record(
        self,
        *,
        kind: str,
        type: str,
        app_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        seq: int = 0,
        actor_user_id: str | None = None,
        actor_roles: list[str] | None = None,
        role: str | None = None,
        content: str | None = None,
        tool_call_id: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        name: str | None = None,
        payload: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        target_user_id: str | None = None,
        target_app_id: str | None = None,
        target_resource: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        correlation_id: str = "",
        success: bool = True,
        message: str = "",
        **_ignored: Any,
    ) -> int | None:
        """Route a record() call into the SessionStore.

        Returns the seq on success. ``None`` if the row was dropped
        (no session_id, session not opened, mode=OFF, ...).

        SEQ CONTRACT (locked):
        If the caller passes ``seq > 0`` it MUST be honored exactly --
        history seqs are never overridden. This is critical because
        the wire-level allocator (EventBuffer in session_bus.emit) has
        already stamped the envelope the client received; if the
        persisted seq diverges from the wire seq, replay would surface
        the same event under a different number and the frontend's
        strict-seq dedup would treat it as a phantom. When ``seq == 0``
        the SessionStore allocator picks one (internal callers without
        a wire pipe -- e.g. compaction markers, parent-link bookkeeping).
        """
        if self.mode == BridgeMode.OFF:
            return None

        if not session_id:
            self.dropped_no_session += 1
            return None

        state = self._store.state(session_id)
        if state is None:
            # Auto-open: the daemon's session creation lives in
            # Postgres, not in the SessionStore. The first history
            # event for a session is the implicit "session begins"
            # signal -- open the session lazily so the bridge never
            # drops legitimate traffic. Idempotent: a duplicate open
            # for an already-loaded session is a no-op.
            try:
                state = await self._store.open(
                    session_id,
                    app_id=app_id or "",
                    user_id=user_id or "",
                )
            except Exception as exc:
                self.dropped_unopened += 1
                logger.warning(
                    "session_store_bridge_auto_open_failed sid=%s "
                    "type=%s err=%s",
                    session_id, type, exc,
                )
                return None
            if state is None:
                self.dropped_unopened += 1
                return None

        # SEQ CONTRACT (locked):
        # The caller may have already allocated a wire-level seq via
        # EventBuffer and shipped it to the client. If so, propagate it
        # into the Event so store.append_event keeps it intact instead
        # of allocating a parallel one (which would make wire seq and
        # persisted seq diverge). seq <= 0 means "no caller allocation,
        # let the store assign one".
        ev = Event(
            type=type,
            seq=int(seq) if seq and seq > 0 else 0,
            kind=kind,
            app_id=app_id,
            session_id=session_id,
            user_id=user_id,
            actor_user_id=actor_user_id,
            actor_roles=list(actor_roles or []),
            role=role,
            content=content,
            tool_call_id=tool_call_id,
            tool_calls=tool_calls,
            name=name,
            payload=dict(payload or {}),
            before=dict(before or {}),
            after=dict(after or {}),
            target_user_id=target_user_id,
            target_app_id=target_app_id,
            target_resource=target_resource,
            ip_address=ip_address,
            user_agent=user_agent,
            correlation_id=correlation_id or "",
            success=bool(success),
            message=str(message or ""),
        )
        new_seq = await self._store.append_event(session_id, ev)
        self.routed += 1
        return new_seq

    def stats(self) -> dict[str, int]:
        return {
            "mode": self.mode.value,
            "routed": self.routed,
            "dropped_no_session": self.dropped_no_session,
            "dropped_unopened": self.dropped_unopened,
        }


_DEFAULT_BRIDGE: "SessionStoreBridge | None" = None


def set_default_bridge(bridge: "SessionStoreBridge | None") -> None:
    """Install the process-wide bridge. ``None`` removes it (returns
    history.record() to legacy-only behavior).

    Wired at daemon startup once the SessionStore is ready. Tests
    install a fresh bridge in setUp and remove it in tearDown."""
    global _DEFAULT_BRIDGE
    _DEFAULT_BRIDGE = bridge


def get_default_bridge() -> "SessionStoreBridge | None":
    return _DEFAULT_BRIDGE
