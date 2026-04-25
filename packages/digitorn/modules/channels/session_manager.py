"""Session management for channel activations — durable persistence.

Handles three session strategies:
- ``per_event``:  Fresh session per inbound event (no history).
- ``shared``:     Persistent session per (provider, source) — conversation continues.
- ``template``:   Session key from a rendered template (e.g. ``wa-{{event.source}}``).

Shared sessions are persisted to the database. On daemon restart,
conversations resume exactly where they left off.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from digitorn.modules.channels.adapter import InboundEvent
from digitorn.modules.channels.template import render

logger = logging.getLogger(__name__)


class ChannelSessionManager:
    """Manages sessions for channel activations with durable persistence."""

    def __init__(self, app_id: str = "default") -> None:
        self._app_id = app_id
        self._locks: dict[str, asyncio.Lock] = {}
        self._histories: dict[str, list[dict[str, Any]]] = {}
        self._max_sessions = 10_000
        self._dirty: set[str] = set()
        # Protects concurrent get_lock/clear_session/_evict_oldest
        import threading
        self._dict_lock = threading.RLock()

    def resolve_session_id(
        self,
        event: InboundEvent,
        session_config: str,
        variables: dict[str, Any],
    ) -> str:
        if not session_config or session_config == "per_event":
            return f"ch-evt-{uuid.uuid4().hex[:12]}"

        if session_config == "shared":
            return f"ch-{event.provider_id}-{event.source}"

        rendered = render(session_config, variables)
        sanitized = "".join(
            c if c.isalnum() or c in "-_." else "-"
            for c in rendered
        )
        return sanitized[:128] or f"ch-{uuid.uuid4().hex[:12]}"

    def get_lock(self, session_id: str) -> asyncio.Lock:
        with self._dict_lock:
            existing = self._locks.get(session_id)
            if existing is not None:
                return existing
            if len(self._locks) >= self._max_sessions:
                self._evict_oldest_locked()
            new_lock = asyncio.Lock()
            self._locks[session_id] = new_lock
            return new_lock

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        return self._histories.setdefault(session_id, [])

    def append_to_history(
        self,
        session_id: str,
        role: str,
        content: str,
    ) -> None:
        history = self.get_history(session_id)
        history.append({"role": role, "content": content})
        self._dirty.add(session_id)

        if len(history) > 100:
            self._histories[session_id] = history[:1] + history[-80:]

    async def persist_session(self, session_id: str) -> None:
        """Persist a session's messages to the database."""
        if session_id not in self._histories:
            return
        try:
            from digitorn.core.database import _engine
            if _engine is None:
                return

            from digitorn.core.runtime.persistence import SessionPersister
            p = SessionPersister(self._app_id, session_id, "channel")
            await p.save_messages(self._histories[session_id])
            self._dirty.discard(session_id)
            logger.debug("channel_session_persisted session=%s msgs=%d", session_id, len(self._histories[session_id]))
        except Exception as exc:
            logger.debug("channel_session_persist_failed session=%s: %s", session_id, exc)

    async def persist_dirty(self) -> None:
        """Persist all sessions that have been modified since last save."""
        for session_id in list(self._dirty):
            await self.persist_session(session_id)

    async def load_session(self, session_id: str) -> list[dict[str, Any]]:
        """Load a session's messages from the database."""
        try:
            from digitorn.core.database import _engine
            if _engine is None:
                return []

            from digitorn.core.runtime.persistence import SessionPersister
            p = SessionPersister(self._app_id, session_id, "channel")
            messages = await p.load_messages()
            if messages:
                self._histories[session_id] = messages
                logger.info(
                    "channel_session_restored session=%s msgs=%d",
                    session_id, len(messages),
                )
            return messages
        except Exception as exc:
            logger.debug("channel_session_load_failed session=%s: %s", session_id, exc)
            return []

    async def restore_active_sessions(self) -> int:
        """Restore all active channel sessions from the database on restart."""
        try:
            from digitorn.core.database import _engine
            if _engine is None:
                return 0

            from digitorn.core.runtime.persistence import list_active_sessions
            active = await list_active_sessions(self._app_id)
            restored = 0
            for info in active:
                sid = info["session_id"]
                if sid.startswith("ch-") or sid.startswith("discord-") or sid.startswith("slack-") or sid.startswith("tg-") or sid.startswith("email-") or sid.startswith("call-") or sid.startswith("phone-"):
                    messages = await self.load_session(sid)
                    if messages:
                        restored += 1

            if restored:
                logger.info("channel_sessions_restored count=%d app=%s", restored, self._app_id)
            return restored
        except Exception as exc:
            logger.debug("channel_sessions_restore_failed: %s", exc)
            return 0

    def clear_session(self, session_id: str) -> None:
        with self._dict_lock:
            self._histories.pop(session_id, None)
            self._locks.pop(session_id, None)
            self._dirty.discard(session_id)

    def _evict_oldest(self) -> None:
        with self._dict_lock:
            self._evict_oldest_locked()

    def _evict_oldest_locked(self) -> None:
        """Internal eviction — caller must hold _dict_lock."""
        to_remove = max(1, len(self._locks) // 10)
        keys = list(self._locks.keys())[:to_remove]
        for key in keys:
            self._locks.pop(key, None)
            self._histories.pop(key, None)
            self._dirty.discard(key)
        logger.debug("channel_sessions_evicted count=%d", to_remove)

    def active_session_count(self) -> int:
        return len(self._histories)

    async def cleanup(self) -> None:
        """Persist all dirty sessions then clear. Called on module stop."""
        await self.persist_dirty()
        self._locks.clear()
        self._histories.clear()
        self._dirty.clear()
