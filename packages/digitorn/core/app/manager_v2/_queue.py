"""_QueueMixin - message queue / session reservation helpers."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class _QueueMixin:
    """Drain queue, reserve / release session, is_turn_running."""

    async def drain_session_queue(
        self, app_id: str, session_id: str, user_id: str = "local",
    ) -> int:
        """Dispatch queued messages for a session until the queue is
        empty. Called from Socket.IO ``join_session`` after a crash /
        reconnect so pending work resumes without the user having to
        trigger it.

        Returns the number of messages successfully processed.
        """
        from digitorn.core.app import message_queue as _mq
        processed = 0
        while True:
            entry = await _mq.next_queued(session_id)
            if entry is None:
                break

            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS,
            )
            # Publish message_started for the client UI.
            try:
                await self.event_bus.emit(_SE.build(
                    type="message_started",
                    app_id=app_id, session_id=session_id, user_id=user_id,
                    op_id=entry.correlation_id,
                    op_type=_OT.TURN, op_state=_OS.RUNNING,
                    correlation_id=entry.correlation_id,
                    payload={
                        "correlation_id": entry.correlation_id,
                        "session_id": session_id,
                        "position": entry.position,
                        "resumed": True,
                    },
                ))
            except Exception:
                pass

            try:
                await self.chat(
                    app_id, session_id, entry.message,
                    user_id=user_id,
                    image_refs=entry.image_refs or None,
                    correlation_id=entry.correlation_id,
                )
            except Exception as exc:
                logger.warning(
                    "drain_session_queue: chat failed app=%s sid=%s: %s",
                    app_id, session_id, exc,
                )
                try:
                    await _mq.mark_failed(
                        entry.id, error_code="internal",
                    )
                    _mq.fail_awaiter(entry.correlation_id, exc)
                    await self.event_bus.emit(_SE.build(
                        type="error",
                        app_id=app_id, session_id=session_id, user_id=user_id,
                        op_id=entry.correlation_id,
                        op_type=_OT.TURN, op_state=_OS.FAILED,
                        correlation_id=entry.correlation_id,
                        payload={
                            "error": str(exc)[:500],
                            "code": "internal",
                            "correlation_id": entry.correlation_id,
                        },
                    ))
                except Exception:
                    pass
                continue

            # Success - mark done + publish.
            try:
                await _mq.mark_done(entry.id)
                _mq.resolve_awaiter(
                    entry.correlation_id, {"status": "completed"},
                )
                await self.event_bus.emit(_SE.build(
                    type="message_done",
                    app_id=app_id, session_id=session_id, user_id=user_id,
                    op_id=entry.correlation_id,
                    op_type=_OT.TURN, op_state=_OS.COMPLETED,
                    correlation_id=entry.correlation_id,
                    payload={
                        "correlation_id": entry.correlation_id,
                        "session_id": session_id,
                    },
                ))
            except Exception:
                pass
            processed += 1
        if processed:
            logger.info(
                "drain_session_queue finished app=%s sid=%s processed=%d",
                app_id, session_id, processed,
            )
        return processed

    async def is_turn_running(self, app_id: str, session_id: str) -> bool:
        """Authoritative check combining in-memory turns (fast-path) AND queue DB rows."""
        if self.is_session_active(app_id, session_id):
            return True
        try:
            from digitorn.core.app import message_queue as _mq
            return await _mq.has_running(session_id)
        except Exception:
            return False

    def reserve_session(self, app_id: str, session_id: str) -> bool:
        """Atomically reserve a session as active. Returns False if already active."""
        key = f"{app_id}:{session_id}"
        if key in self._active_sessions:
            return False
        self._active_sessions.add(key)
        return True

    def release_session(self, app_id: str, session_id: str) -> None:
        self._active_sessions.discard(f"{app_id}:{session_id}")
