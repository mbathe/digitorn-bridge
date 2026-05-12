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
        workspace: str = "", workdir: str = "",
    ) -> None:
        self.app_id = app_id
        self.session_id = session_id
        self.agent_id = agent_id
        # BANK-GRADE: the user_id is part of the audit contract - a
        # history row whose owner is NULL is unattributable and breaks
        # "who did this". Store it on the persister so _ensure_session
        # can stamp the row at creation time.
        self.user_id = (user_id or "").strip() or None
        # Daemon-private workspace + agent-facing workdir. Persisted on
        # the UserSession row so a daemon restart can rebuild the session
        # without losing the per-session dir (otherwise the agent loses
        # access to files it just wrote).
        self._workspace = workspace or ""
        self._workdir = workdir or ""
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
                    workspace=self._workspace or "",
                    workdir=self._workdir or "",
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
        # ``HistoryLog`` / ``get_session_factory`` / ``func`` imports
        # that used to live here served the legacy
        # ``streaming_rows_finalize`` block (which scanned Postgres
        # ``history_log`` to flip streaming rows to complete). That
        # block was deleted alongside the per-chunk Postgres write
        # in ``upsert_streaming_assistant`` -- ``save_messages`` is
        # now SessionStore-only via ``history.record`` (which routes
        # to the bridge). The only Postgres hop left in this function
        # is ``_ensure_session`` (analytics ``user_sessions`` row),
        # and that runs on the persist_worker thread off the main
        # loop, so it cannot stall Socket.IO heartbeats.

        session_pk = await self._ensure_session(create_if_missing=create_if_missing)
        if session_pk is None:
            # First turn not yet committed - skip silently. The final
            # "completed" persist will flush the full message history.
            return

        # BANK-GRADE: APPEND-ONLY. History is immutable. We pre-check
        # which roles are already projected in the SessionStore (the
        # post-Phase-4c source of truth) so a re-entry never duplicates
        # a row. Counting per role (instead of a single ``existing_max``
        # seq) is required because user_message is emitted by the
        # SocketIO bus on POST, while assistant_message is emitted here
        # at turn-end -- they share no list-index seq space and the old
        # ``MAX(HistoryLog.seq)`` gate (which read partial streaming
        # rows from ``upsert_streaming_assistant``) silently skipped
        # every assistant turn.
        projected_by_role: dict[str, int] = {}
        try:
            from digitorn.core.runtime.session_store.bridge import (
                get_default_bridge,
            )
            _bridge = get_default_bridge()
            if _bridge is not None:
                _state = _bridge.store.state(self.session_id)
                if _state is not None:
                    for _m in _state.messages:
                        projected_by_role[_m.role] = (
                            projected_by_role.get(_m.role, 0) + 1
                        )
        except Exception as exc:
            logger.debug("session_store_existing_count_failed: %s", exc)

        try:
            from digitorn.core.history import record as _record
        except Exception as exc:
            logger.debug("history.record import failed: %s", exc)
            return

        # The legacy ``streaming_rows_finalize`` block that used to
        # live here walked Postgres ``history_log`` to flip
        # ``streaming_status='streaming'`` rows to ``'complete'``.
        # That whole code path is dead now: ``upsert_streaming_assistant``
        # no longer writes to Postgres, so there are no streaming rows
        # to finalize. Removed entirely to drop the per-turn DB roundtrip.

        appended = 0
        role_seen: dict[str, int] = {}
        for seq, msg in enumerate(messages):
            _role_for_gate = msg.get("role") or "unknown"
            role_seen[_role_for_gate] = role_seen.get(_role_for_gate, 0) + 1
            # Skip the message if the SessionStore already has at least
            # this many messages of that role projected -- it was either
            # emitted by another path (user via SocketIO bus) or by a
            # previous save_messages call on this same session.
            if role_seen[_role_for_gate] <= projected_by_role.get(_role_for_gate, 0):
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
            # Use ``in`` so empty string is preserved - V4 emits empty
            # reasoning for trivial turns and still requires it on replay.
            if "reasoning_content" in msg:
                payload["reasoning_content"] = msg["reasoning_content"]

            # Stamp the agent-loop slot seq so the projection can pop
            # the matching ``streaming_partials`` entry when the final
            # assistant_message lands. Without this, partial buffers
            # accumulate forever in memory after every turn.
            # NOTE: ``agent_seq`` is the index in ``messages`` (0..N-1),
            # NOT the event-stream seq. It is metadata for the streaming
            # projection only -- the canonical seq for ordering /
            # dedup is the one the SessionStore allocator assigns
            # below via ``seq=0``.
            payload["agent_seq"] = seq

            role = msg.get("role", "")
            try:
                # ``seq=0`` tells ``bridge.record`` / ``store.append_event``
                # to allocate a fresh monotonic seq from the SessionStore's
                # SeqAllocator. Critical: previously we passed ``seq=seq``
                # which is the ENUMERATE INDEX in the messages list
                # (0, 1, 2, ...). Once the session had any real events
                # on disk (last_seq >> N), the flusher rejected every
                # ``save_messages`` write as ``seq_regression`` and the
                # messages were SILENTLY LOST from the on-disk journal.
                # By letting the allocator decide, the seq is guaranteed
                # to be > current ``meta.last_seq`` -- no regression
                # possible by construction.
                await _record(
                    kind="message",
                    type=f"{role or 'unknown'}_message",
                    app_id=self.app_id,
                    session_id=self.session_id,
                    user_id=self.user_id,
                    seq=0,
                    role=role,
                    content=content_for_col,
                    tool_call_id=msg.get("tool_call_id"),
                    tool_calls=msg.get("tool_calls"),
                    name=msg.get("name"),
                    payload=payload,
                )
                appended += 1
            except Exception as exc:
                logger.debug(
                    "history.record message failed agent_seq=%d: %s",
                    seq, exc,
                )

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
        message. SessionStore-only: emits an ``assistant_message_partial``
        event via the bridge so the projection buffers the latest text
        in ``state.streaming_partials``. Crash recovery reads the
        buffer; the live UI already sees the tokens on the wire.

        The legacy ``history_log`` Postgres write that used to live
        here was retired: Phase 4c removed every reader of those rows
        (``/history`` reads the SessionStore, ``/events`` reads the
        SessionStore, ``save_messages`` at turn-end emits the
        canonical ``assistant_message``). Writing them per chunk was
        pure overhead -- worse, it ran asyncpg on the main loop
        every 500ms which on Windows IOCP could block in
        ``ssl.write`` for 20+ seconds.

        ``status='streaming'`` marks the snapshot as partial;
        ``status='complete'`` is the abort-finalize variant. Both
        paths only touch the bridge.
        """
        try:
            from digitorn.core.runtime.session_store.bridge import (
                get_default_bridge,
            )
            _br = get_default_bridge()
            if _br is None:
                return
            # Bypass ``digitorn.core.history.record`` (its strict
            # contract refuses ``seq=0`` on kind='event'); call
            # ``bridge.record`` directly -- the bridge allocates the
            # real event seq itself from the SessionStore allocator.
            await _br.record(
                kind="event",
                type="assistant_message_partial",
                app_id=self.app_id,
                session_id=self.session_id,
                user_id=self.user_id or "",
                role="assistant",
                content=content,
                tool_calls=tool_calls,
                payload={
                    "agent_seq": seq,
                    "streaming_status": status,
                    "content": content,
                    "content_kind": "text",
                    **({"message_id": message_id} if message_id else {}),
                },
            )
        except Exception as exc:
            logger.debug(
                "upsert_streaming_assistant bridge emit failed seq=%d: %s",
                seq, exc,
            )

    async def append_messages(
        self,
        messages: list[dict[str, Any]],
        start_seq: int,
        *,
        create_if_missing: bool = True,
    ) -> None:
        """Append new messages (incremental save).

        Writes to the SessionStore journal with ``kind='message'``.
        Used by code paths that already know the next seq -- but the
        seq is now ignored: the SessionStore's SeqAllocator owns the
        numbering, and we let it allocate fresh seqs that are
        guaranteed to be > the current ``meta.last_seq``.

        NOTE: ``start_seq`` is preserved in the signature for
        backwards compat but is no longer used. Previously we passed
        ``seq=start_seq + i`` which produced regressions on any
        session with prior on-disk events: ``start_seq`` was always
        a low integer (computed from the in-memory ``messages`` list
        length, NOT the event-stream high-water mark), so the flusher
        rejected every write as ``seq_regression``. Now ``seq=0``
        defers to the allocator -- no regression possible.
        """
        _ = start_seq  # intentionally unused -- see docstring
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
            # Same `in` check as save_messages - empty string must be
            # preserved for DeepSeek V4 thinking-mode replay.
            if "reasoning_content" in msg:
                _payload["reasoning_content"] = msg["reasoning_content"]
            # In-list position survives as ``agent_seq`` for the
            # streaming-partials projection; the persistence-stream
            # seq is allocated by the store (seq=0 trigger).
            _payload["agent_seq"] = i
            await _record(
                kind="message",
                type=f"{role or 'unknown'}_message",
                app_id=self.app_id,
                session_id=self.session_id,
                user_id=self.user_id,
                seq=0,
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
            if isinstance(row.payload, dict) and "reasoning_content" in row.payload:
                # Preserve empty string - DeepSeek V4 thinking mode
                # rejects assistant turns missing reasoning_content
                # even when the original turn emitted an empty block.
                msg["reasoning_content"] = row.payload["reasoning_content"]
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
