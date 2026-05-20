"""Durable session persistence - survives daemon restarts."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, update


def _dialect_safe_insert_ignore(table: Any, rows: list[dict[str, Any]],
                                 conflict_cols: list[str]) -> Any:
    """Plain INSERT - we DON'T use ON CONFLICT because the existing"""
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
        self.user_id = (user_id or "").strip() or None
        self._workspace = workspace or ""
        self._workdir = workdir or ""
        self._session_pk: str | None = None

    async def _ensure_session(self, *, create_if_missing: bool = True) -> str | None:
        """Get or create the UserSession row, return its PK (or None)."""
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
        """Persist all messages for this session as `history_log`"""
        session_pk = await self._ensure_session(create_if_missing=create_if_missing)
        if session_pk is None:
            return

        # count per role (not a single max seq) - user_message lands via SocketIO bus, assistant_message lands here; they share no list-index seq space.
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

        appended = 0
        role_seen: dict[str, int] = {}
        for seq, msg in enumerate(messages):
            _role_for_gate = msg.get("role") or "unknown"
            role_seen[_role_for_gate] = role_seen.get(_role_for_gate, 0) + 1
            if role_seen[_role_for_gate] <= projected_by_role.get(_role_for_gate, 0):
                continue
            raw_content = msg.get("content")
            # scalar `content` column gets text only; multimodal blocks go in `payload` so attachments survive replay.
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

            # DeepSeek V4 requires reasoning_content on replay (HistoryLog has no dedicated column); use `in` to preserve empty strings.
            if "reasoning_content" in msg:
                payload["reasoning_content"] = msg["reasoning_content"]

            # agent_seq is the messages-list index (metadata for the streaming projection); canonical seq is allocated below.
            payload["agent_seq"] = seq

            role = msg.get("role", "")
            try:
                # seq=0 tells the bridge to allocate a fresh monotonic seq - passing the enumerate index regresses last_seq and drops events.
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
        """Progressive per-chunk persistence of the in-flight assistant"""
        try:
            from digitorn.core.runtime.session_store.bridge import (
                get_default_bridge,
            )
            _br = get_default_bridge()
            if _br is None:
                return
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
        """Append new messages (incremental save)."""
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
        """Load all messages for this session, ordered by seq, from"""
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
        """Save or update the checkpoint for this session."""
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
        """Delete all persisted data for this session."""
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
