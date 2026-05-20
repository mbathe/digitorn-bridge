"""Bridge: convert `history.record(...)` calls into `Event`"""

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
    """Converts `history.record` kwargs into Event + append_event."""

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
        """Route a record() call into the SessionStore."""
        if self.mode == BridgeMode.OFF:
            return None

        if not session_id:
            self.dropped_no_session += 1
            return None

        state = self._store.state(session_id)
        if state is None:
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
    """Install the process-wide bridge. `None` removes it (returns"""
    global _DEFAULT_BRIDGE
    _DEFAULT_BRIDGE = bridge


def get_default_bridge() -> "SessionStoreBridge | None":
    return _DEFAULT_BRIDGE
