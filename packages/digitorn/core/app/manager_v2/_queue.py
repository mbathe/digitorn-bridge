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

        Returns the number of messages successfully processed (PAUSED
        and FAILED entries don't count - they stop the loop or get
        recorded as failed and we move on).
        """
        from digitorn.core.app import message_queue as _mq
        from digitorn.core.api.apps_v2._dispatch import (
            dispatch_turn, TurnEntry, TurnSource, TurnStatus,
        )
        deployed = None
        try:
            deployed = self._get_deployed(app_id, user_id=user_id)
        except Exception:
            deployed = None
        credential_store = getattr(self, "_credential_store", None)

        from digitorn.core.runtime.request_context import (
            set_inbound_user_jwt, reset_inbound_user_jwt,
        )
        processed = 0
        while True:
            entry = await _mq.next_queued(session_id)
            if entry is None:
                break

            # Re-publish the JWT stashed at enqueue (None when missing).
            # Without this, gateway-routed turns lose ``Authorization``.
            queued_jwt = _mq.pop_jwt(entry.id)
            _jwt_token = set_inbound_user_jwt(queued_jwt) if queued_jwt else None
            try:
                outcome = await dispatch_turn(
                    None,  # no FastAPI request — Socket.IO context
                    app_id, session_id,
                    entry=TurnEntry(
                        correlation_id=entry.correlation_id,
                        message=entry.message,
                        image_refs=entry.image_refs or None,
                        queue_row_id=entry.id,
                        position=entry.position,
                    ),
                    user_id=user_id,
                    source=TurnSource.RESUME,
                    manager=self,
                    deployed=deployed,
                    credential_store=credential_store,
                )
            finally:
                if _jwt_token is not None:
                    reset_inbound_user_jwt(_jwt_token)

            if outcome.status == TurnStatus.PAUSED:
                # Credential gate hit - mark the row failed with
                # `credential_required` so is_turn_running drops to
                # False (otherwise the user's RETRY queues behind a
                # stuck row). Then stop the resume loop: the next
                # queued entries likely need the same missing key.
                try:
                    await _mq.mark_failed(
                        entry.id, error_code="credential_required",
                    )
                    _mq.fail_awaiter(
                        entry.correlation_id,
                        RuntimeError("credential_required"),
                    )
                except Exception:
                    pass
                break
            if outcome.status == TurnStatus.COMPLETED:
                try:
                    await _mq.mark_done(entry.id)
                    _mq.resolve_awaiter(
                        entry.correlation_id, {"status": "completed"},
                    )
                except Exception:
                    pass
                processed += 1
            else:
                # FAILED or CANCELLED - mark the row terminal and keep
                # draining; the next entry might succeed.
                try:
                    err_code = (
                        outcome.error_code or "internal"
                        if outcome.status == TurnStatus.FAILED
                        else "turn_cancelled"
                    )
                    await _mq.mark_failed(entry.id, error_code=err_code)
                    _mq.fail_awaiter(
                        entry.correlation_id,
                        RuntimeError(err_code),
                    )
                except Exception:
                    pass
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
