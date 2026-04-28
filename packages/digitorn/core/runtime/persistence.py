"""Durable session persistence - survives daemon restarts.

Every message, tool call result, and checkpoint is persisted to the database.
On restart, sessions are reloaded exactly where they left off.

Usage in agent_loop:
    persister = SessionPersister(app_id, session_id, agent_id)
    await persister.save_messages(messages)
    await persister.save_checkpoint(turn, status, usage)

Usage on restart:
    messages = await persister.load_messages()
    checkpoint = await persister.load_checkpoint()
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, update


def _dialect_safe_insert_ignore(table: Any, rows: list[dict[str, Any]],
                                 conflict_cols: list[str]) -> Any:
    """Plain INSERT - we DON'T use ON CONFLICT because the existing
    ``session_messages`` index on (session_pk, seq) is NOT UNIQUE
    and SQLite refuses ``ON CONFLICT`` on non-unique constraints.

    We get idempotency from the caller's pre-check (only rows with
    seq > existing_max are passed in), so duplicates can only arise
    under tight concurrent-write races on the same session - which
    don't happen in practice because ``_persist_turn`` is awaited
    serially per turn within a session.

    When we eventually migrate to Postgres AND promote the index to
    UNIQUE, this helper can be extended to re-enable ON CONFLICT DO
    NOTHING. Both Postgres and SQLite expose the same API shape via
    their respective ``insert`` constructors (``index_elements=``).
    """
    from sqlalchemy import insert as core_insert
    return core_insert(table).values(rows)

logger = logging.getLogger(__name__)


class SessionPersister:
    """Persists session state to the database."""

    def __init__(
        self,
        app_id: str, session_id: str, agent_id: str = "main",
        *, user_id: str | None = None,
    ) -> None:
        self.app_id = app_id
        self.session_id = session_id
        self.agent_id = agent_id
        # BANK-GRADE: the user_id is part of the audit contract - a
        # history row whose owner is NULL is unattributable and breaks
        # "who did this". Store it on the persister so _ensure_session
        # can stamp the row at creation time.
        self.user_id = (user_id or "").strip() or None
        self._session_pk: str | None = None

    async def _ensure_session(self, *, create_if_missing: bool = True) -> str | None:
        """Get or create the UserSession row, return its PK (or None).

        ``create_if_missing=True`` (default): create the row if it
        doesn't exist. Used at the end of a successful turn
        (``status="completed"``) to commit the session to DB.

        ``create_if_missing=False``: only return the PK if the row
        already exists; return None otherwise. Used by intermediate
        per-tool-call persistence during a turn so a FAILED first
        turn leaves no UserSession row in DB.
        """
        if self._session_pk:
            return self._session_pk

        from digitorn.core.database import get_session_factory
        from digitorn.core.models import UserSession

        async with get_session_factory()() as db:
            result = await db.execute(
                select(UserSession.id).where(
                    UserSession.app_id == self.app_id,
                    UserSession.session_id == self.session_id,
                )
            )
            row = result.scalar_one_or_none()

            if row:
                self._session_pk = row
                # If the row was created earlier without a user_id (pre-
                # bank-grade fix) and we now know the owner, backfill.
                if self.user_id:
                    from sqlalchemy import update as _update
                    await db.execute(
                        _update(UserSession)
                        .where(UserSession.id == row)
                        .where(UserSession.user_id.is_(None))
                        .values(user_id=self.user_id)
                    )
            elif create_if_missing:
                session_obj = UserSession(
                    app_id=self.app_id,
                    session_id=self.session_id,
                    user_id=self.user_id,
                )
                db.add(session_obj)
                await db.flush()
                self._session_pk = session_obj.id
            else:
                # Uncommitted session - caller skips persistence.
                await db.commit()
                return None

            await db.commit()

        return self._session_pk

    async def save_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        create_if_missing: bool = True,
    ) -> None:
        """Persist all messages for this session as ``history_log``
        rows with ``kind='message'``.

        With ``create_if_missing=False``, persistence is a no-op unless
        the UserSession row already exists - use this flag on
        intermediate per-tool-call saves so a failing first turn
        doesn't leak a half-written session into DB.
        """
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import HistoryLog
        from sqlalchemy import func

        session_pk = await self._ensure_session(create_if_missing=create_if_missing)
        if session_pk is None:
            # First turn not yet committed - skip silently. The final
            # "completed" persist will flush the full message history.
            return

        # BANK-GRADE: APPEND-ONLY. History is immutable. We pre-check
        # the highest ``seq`` already persisted for this session in
        # history_log (kind='message') so a reentry never duplicates
        # a row. Only messages whose index > max_persisted_seq are
        # written, which also means the client can call save_messages
        # with the full message list on every turn without fear.
        async with get_session_factory()() as db:
            existing_max = (
                await db.execute(
                    select(func.coalesce(func.max(HistoryLog.seq), -1))
                    .where(HistoryLog.kind == "message")
                    .where(HistoryLog.app_id == self.app_id)
                    .where(HistoryLog.session_id == self.session_id)
                )
            ).scalar()
            if existing_max is None:
                existing_max = -1

        try:
            from digitorn.core.history import record as _record
        except Exception as exc:
            logger.debug("history.record import failed: %s", exc)
            return

        # Progressive-streaming bridge: walk any already-persisted
        # assistant rows flagged ``streaming_status='streaming'`` and
        # bring them to their final ``'complete'`` state with the
        # final content/tool_calls from the in-memory ``messages``
        # list. Covers the hand-off from ``upsert_streaming_assistant``
        # (called per-snapshot during the stream) to the authoritative
        # post-turn row produced by the agent loop. Without this
        # bridge the row would stay "streaming" forever even though
        # the turn finished cleanly - which would make the client
        # think the message was interrupted.
        try:
            from digitorn.core.models import HistoryLog as _HL
            async with get_session_factory()() as db:
                streaming_rows = (
                    await db.execute(
                        select(_HL)
                        .where(_HL.kind == "message")
                        .where(_HL.app_id == self.app_id)
                        .where(_HL.session_id == self.session_id)
                        .where(_HL.role == "assistant")
                    )
                ).scalars().all()
                dirty = False
                for row in streaming_rows:
                    payload = row.payload if isinstance(row.payload, dict) else {}
                    if payload.get("streaming_status") != "streaming":
                        continue
                    if 0 <= row.seq < len(messages):
                        final_msg = messages[row.seq]
                        if final_msg.get("role") != "assistant":
                            continue
                        final_content = final_msg.get("content")
                        if isinstance(final_content, str):
                            row.content = final_content
                        elif final_content is not None:
                            try:
                                import json as _json
                                row.content = _json.dumps(
                                    final_content, ensure_ascii=False,
                                    default=str,
                                )
                            except Exception:
                                row.content = str(final_content)
                        if final_msg.get("tool_calls") is not None:
                            row.tool_calls = final_msg["tool_calls"]
                        new_payload = dict(payload)
                        new_payload["streaming_status"] = "complete"
                        row.payload = new_payload
                        dirty = True
                if dirty:
                    await db.commit()
        except Exception as exc:
            logger.debug("streaming_rows_finalize_failed: %s", exc)

        appended = 0
        for seq, msg in enumerate(messages):
            if seq <= existing_max:
                continue
            raw_content = msg.get("content")
            # Flatten: scalar ``content`` column takes the text excerpt
            # only. Full structured content (multimodal blocks) goes
            # into ``payload`` so images/attachments survive replay.
            content_for_col: str | None
            if raw_content is None:
                content_for_col = None
            elif isinstance(raw_content, str):
                content_for_col = raw_content
            else:
                import json as _json
                try:
                    content_for_col = _json.dumps(
                        raw_content, ensure_ascii=False, default=str,
                    )
                except Exception:
                    content_for_col = str(raw_content)

            payload: dict[str, Any] = {
                "content_kind": (
                    "multimodal" if isinstance(raw_content, list)
                    else "text" if isinstance(raw_content, str)
                    else "none" if raw_content is None
                    else type(raw_content).__name__
                ),
            }
            if not isinstance(raw_content, str) and raw_content is not None:
                payload["raw_content"] = raw_content
                if isinstance(raw_content, list):
                    atts = []
                    for block in raw_content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type", "")
                        if btype in (
                            "image", "image_url", "file",
                            "document", "attachment",
                        ):
                            src = (
                                block.get("source")
                                or block.get("image_url")
                                or {}
                            )
                            atts.append({
                                "type": btype,
                                "media_type": (
                                    src.get("media_type")
                                    if isinstance(src, dict)
                                    else None
                                ),
                                "bytes": (
                                    len(src.get("data", ""))
                                    if isinstance(src, dict)
                                    and isinstance(src.get("data"), str)
                                    else None
                                ),
                            })
                    if atts:
                        payload["attachments"] = atts

            # DeepSeek V4 thinking: stash reasoning_content in payload
            # (HistoryLog has no dedicated column). It's required on the
            # NEXT API call to pass back the full assistant message shape.
            rc = msg.get("reasoning_content")
            if rc:
                payload["reasoning_content"] = rc

            role = msg.get("role", "")
            try:
                await _record(
                    kind="message",
                    type=f"{role or 'unknown'}_message",
                    app_id=self.app_id,
                    session_id=self.session_id,
                    user_id=self.user_id,
                    seq=seq,
                    role=role,
                    content=content_for_col,
                    tool_call_id=msg.get("tool_call_id"),
                    tool_calls=msg.get("tool_calls"),
                    name=msg.get("name"),
                    payload=payload,
                )
                appended += 1
            except Exception as exc:
                logger.debug("history.record message failed seq=%d: %s", seq, exc)

        logger.debug(
            "session_messages_saved app=%s session=%s appended=%d total=%d",
            self.app_id, self.session_id,
            appended, len(messages),
        )

    async def upsert_streaming_assistant(
        self,
        seq: int,
        content: str,
        *,
        tool_calls: list[dict[str, Any]] | None = None,
        status: str = "streaming",
        message_id: str | None = None,
        create_if_missing: bool = False,
    ) -> None:
        """Progressive per-chunk persistence of the in-flight assistant
        message. Keeps the ``history_log`` row at ``(session, seq)``
        in sync with the accumulated text as streaming proceeds so a
        daemon crash mid-turn leaves a durable trace of whatever was
        already produced. UPDATE when the row exists, INSERT when it
        doesn't - idempotent across retries and reconnects.
        Intentionally fire-and-forget at the call site (the agent loop
        wraps this in ``asyncio.create_task``), which is why we swallow
        exceptions loudly (debug log) instead of bubbling them up.
        ``status='streaming'`` marks the row as partial (the final
        ``save_messages`` at turn-end flips it to ``'complete'`` via
        a second call with ``status='complete'``)."""
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import HistoryLog

        session_pk = await self._ensure_session(
            create_if_missing=create_if_missing,
        )
        if session_pk is None:
            return

        try:
            factory = get_session_factory()
        except Exception as exc:
            logger.debug("upsert_streaming_assistant: DB not ready: %s", exc)
            return

        payload: dict[str, Any] = {
            "content_kind": "text",
            "streaming_status": status,
        }
        if message_id:
            payload["message_id"] = message_id

        try:
            async with factory() as db:
                existing = (
                    await db.execute(
                        select(HistoryLog).where(
                            HistoryLog.kind == "message",
                            HistoryLog.app_id == self.app_id,
                            HistoryLog.session_id == self.session_id,
                            HistoryLog.seq == seq,
                        )
                    )
                ).scalar_one_or_none()

                if existing is not None:
                    # Guard against a slow in-flight snapshot task
                    # downgrading a row the turn-end flush has
                    # already marked ``complete``. Snapshots are
                    # fire-and-forget, so one scheduled before the
                    # stream ended can still run after ``save_messages``
                    # has finalized the row - without this guard it
                    # would silently revert content + status back to
                    # the mid-stream partial, which is precisely the
                    # "last message disappears" symptom we set out to
                    # fix.
                    prev_status = None
                    if isinstance(existing.payload, dict):
                        prev_status = existing.payload.get("streaming_status")
                    if prev_status == "complete" and status == "streaming":
                        await db.commit()
                        return
                    merged_payload: dict[str, Any] = {}
                    if isinstance(existing.payload, dict):
                        merged_payload.update(existing.payload)
                    merged_payload.update(payload)
                    existing.content = content
                    if tool_calls is not None:
                        existing.tool_calls = tool_calls
                    existing.payload = merged_payload
                    existing.type = "assistant_message"
                    existing.role = "assistant"
                    await db.commit()
                    return

                row = HistoryLog(
                    kind="message",
                    type="assistant_message",
                    app_id=self.app_id,
                    session_id=self.session_id,
                    user_id=self.user_id,
                    seq=seq,
                    role="assistant",
                    content=content,
                    tool_calls=tool_calls,
                    payload=payload,
                )
                db.add(row)
                await db.commit()
        except Exception as exc:
            logger.debug(
                "upsert_streaming_assistant failed seq=%d: %s", seq, exc,
            )

    async def append_messages(
        self,
        messages: list[dict[str, Any]],
        start_seq: int,
        *,
        create_if_missing: bool = True,
    ) -> None:
        """Append new messages starting from start_seq (incremental save).

        Writes to ``history_log`` with ``kind='message'``. Used by
        code paths that already know the next seq (skip the pre-check
        in save_messages).
        """
        session_pk = await self._ensure_session(create_if_missing=create_if_missing)
        if session_pk is None:
            return

        try:
            from digitorn.core.history import record as _record
        except Exception:
            return

        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            raw_content = msg.get("content")
            content_for_col: str | None
            if raw_content is None:
                content_for_col = None
            elif isinstance(raw_content, str):
                content_for_col = raw_content
            else:
                import json as _json
                try:
                    content_for_col = _json.dumps(
                        raw_content, ensure_ascii=False, default=str,
                    )
                except Exception:
                    content_for_col = str(raw_content)
            _payload: dict[str, Any] = {}
            if not isinstance(raw_content, str) and raw_content is not None:
                _payload["raw_content"] = raw_content
            rc = msg.get("reasoning_content")
            if rc:
                _payload["reasoning_content"] = rc
            await _record(
                kind="message",
                type=f"{role or 'unknown'}_message",
                app_id=self.app_id,
                session_id=self.session_id,
                user_id=self.user_id,
                seq=start_seq + i,
                role=role,
                content=content_for_col,
                tool_call_id=msg.get("tool_call_id"),
                tool_calls=msg.get("tool_calls"),
                name=msg.get("name"),
                payload=_payload,
            )

    async def load_messages(self) -> list[dict[str, Any]]:
        """Load all messages for this session, ordered by seq, from
        the unified ``history_log`` table (kind='message')."""
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import HistoryLog, UserSession

        async with get_session_factory()() as db:
            result = await db.execute(
                select(UserSession.id).where(
                    UserSession.app_id == self.app_id,
                    UserSession.session_id == self.session_id,
                )
            )
            session_pk = result.scalar_one_or_none()
            if not session_pk:
                return []

            self._session_pk = session_pk

            result = await db.execute(
                select(HistoryLog)
                .where(HistoryLog.kind == "message")
                .where(HistoryLog.app_id == self.app_id)
                .where(HistoryLog.session_id == self.session_id)
                .order_by(HistoryLog.seq.asc())
            )
            rows = result.scalars().all()

        messages = []
        for row in rows:
            msg: dict[str, Any] = {"role": row.role or ""}
            # Prefer the raw multimodal structure when available so
            # images/attachments replay intact. Fall back to the
            # flattened text column otherwise.
            raw = None
            if isinstance(row.payload, dict):
                raw = row.payload.get("raw_content")
            if raw is not None:
                msg["content"] = raw
            elif row.content is not None:
                msg["content"] = row.content
            if row.tool_call_id:
                msg["tool_call_id"] = row.tool_call_id
            if row.tool_calls:
                msg["tool_calls"] = row.tool_calls
            if row.name:
                msg["name"] = row.name
            if isinstance(row.payload, dict):
                rc = row.payload.get("reasoning_content")
                if rc:
                    msg["reasoning_content"] = rc
            messages.append(msg)

        logger.debug(
            "session_messages_loaded app=%s session=%s count=%d",
            self.app_id, self.session_id, len(messages),
        )
        return messages

    async def save_checkpoint(
        self,
        turn: int,
        status: str = "active",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        tool_calls_count: int = 0,
        last_error: str | None = None,
        metadata: dict[str, Any] | None = None,
        memory_snapshot: dict[str, Any] | None = None,
        workbench_snapshot: dict[str, Any] | None = None,
        *,
        create_if_missing: bool = True,
    ) -> None:
        """Save or update the checkpoint for this session.

        Skipped if ``create_if_missing=False`` and no UserSession row
        exists yet - avoids writing a checkpoint for a session that
        the commit-on-first-success gate chose not to persist.
        """
        if not create_if_missing:
            pk = await self._ensure_session(create_if_missing=False)
            if pk is None:
                return
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import SessionCheckpoint

        async with get_session_factory()() as db:
            result = await db.execute(
                select(SessionCheckpoint).where(
                    SessionCheckpoint.app_id == self.app_id,
                    SessionCheckpoint.session_id == self.session_id,
                    SessionCheckpoint.agent_id == self.agent_id,
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                existing.turn = turn
                existing.status = status
                existing.prompt_tokens = prompt_tokens
                existing.completion_tokens = completion_tokens
                existing.tool_calls_count = tool_calls_count
                existing.last_error = last_error
                existing.extra = metadata or {}
                if memory_snapshot is not None:
                    existing.memory_snapshot = memory_snapshot
                if workbench_snapshot is not None:
                    existing.workbench_snapshot = workbench_snapshot
                existing.updated_at = datetime.now(timezone.utc)
            else:
                db.add(SessionCheckpoint(
                    app_id=self.app_id,
                    session_id=self.session_id,
                    agent_id=self.agent_id,
                    turn=turn,
                    status=status,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    tool_calls_count=tool_calls_count,
                    last_error=last_error,
                    memory_snapshot=memory_snapshot,
                    workbench_snapshot=workbench_snapshot,
                    extra=metadata or {},
                ))

            await db.commit()

        logger.debug(
            "session_checkpoint_saved app=%s session=%s turn=%d status=%s",
            self.app_id, self.session_id, turn, status,
        )

    async def load_checkpoint(self) -> dict[str, Any] | None:
        """Load the latest checkpoint for this session."""
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import SessionCheckpoint

        async with get_session_factory()() as db:
            result = await db.execute(
                select(SessionCheckpoint).where(
                    SessionCheckpoint.app_id == self.app_id,
                    SessionCheckpoint.session_id == self.session_id,
                    SessionCheckpoint.agent_id == self.agent_id,
                )
            )
            cp = result.scalar_one_or_none()

        if not cp:
            return None

        return {
            "turn": cp.turn,
            "status": cp.status,
            "prompt_tokens": cp.prompt_tokens,
            "completion_tokens": cp.completion_tokens,
            "tool_calls_count": cp.tool_calls_count,
            "last_error": cp.last_error,
            "memory_snapshot": cp.memory_snapshot,
            "workbench_snapshot": cp.workbench_snapshot,
            "extra": cp.extra,
            "updated_at": cp.updated_at.isoformat() if cp.updated_at else None,
        }

    async def mark_completed(self, turn: int, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        """Mark the session as completed."""
        await self.save_checkpoint(
            turn=turn,
            status="completed",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    async def mark_failed(self, turn: int, error: str) -> None:
        """Mark the session as failed with error."""
        await self.save_checkpoint(
            turn=turn,
            status="failed",
            last_error=error,
        )

    async def delete_session_data(self) -> None:
        """Delete all persisted data for this session.

        Wipes checkpoints + history_log rows (messages / events /
        audit entries scoped to this session). UserSession row is
        preserved - history_log rows live past session lifetime
        for audit traceability, but ``delete_session_data`` is only
        called on explicit user-driven "forget this session" flows.
        """
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import SessionCheckpoint, HistoryLog

        async with get_session_factory()() as db:
            await db.execute(
                delete(HistoryLog).where(
                    HistoryLog.app_id == self.app_id,
                    HistoryLog.session_id == self.session_id,
                )
            )
            await db.execute(
                delete(SessionCheckpoint).where(
                    SessionCheckpoint.app_id == self.app_id,
                    SessionCheckpoint.session_id == self.session_id,
                )
            )
            await db.commit()


async def list_active_sessions(app_id: str) -> list[dict[str, Any]]:
    """List all active sessions for an app (for resume on restart)."""
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionCheckpoint

    async with get_session_factory()() as db:
        result = await db.execute(
            select(SessionCheckpoint).where(
                SessionCheckpoint.app_id == app_id,
                SessionCheckpoint.status == "active",
            )
        )
        rows = result.scalars().all()

    return [
        {
            "session_id": r.session_id,
            "agent_id": r.agent_id,
            "turn": r.turn,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "tool_calls_count": r.tool_calls_count,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]
