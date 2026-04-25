"""Durable session persistence — survives daemon restarts.

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
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

logger = logging.getLogger(__name__)


class SessionPersister:
    """Persists session state to the database."""

    def __init__(self, app_id: str, session_id: str, agent_id: str = "main") -> None:
        self.app_id = app_id
        self.session_id = session_id
        self.agent_id = agent_id
        self._session_pk: str | None = None

    async def _ensure_session(self) -> str:
        """Get or create the UserSession row, return its PK."""
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
            else:
                session_obj = UserSession(
                    app_id=self.app_id,
                    session_id=self.session_id,
                )
                db.add(session_obj)
                await db.flush()
                self._session_pk = session_obj.id

            await db.commit()

        return self._session_pk

    async def save_messages(self, messages: list[dict[str, Any]]) -> None:
        """Persist all messages for this session (full replace)."""
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import SessionMessage

        session_pk = await self._ensure_session()

        async with get_session_factory()() as db:
            await db.execute(
                delete(SessionMessage).where(
                    SessionMessage.session_pk == session_pk,
                )
            )

            for seq, msg in enumerate(messages):
                db.add(SessionMessage(
                    session_pk=session_pk,
                    seq=seq,
                    role=msg.get("role", ""),
                    content=msg.get("content"),
                    tool_call_id=msg.get("tool_call_id"),
                    tool_calls=msg.get("tool_calls"),
                    name=msg.get("name"),
                ))

            await db.execute(
                update(type(db)).where(False)  # no-op, just to trigger commit
            ) if False else None

            await db.commit()

        logger.debug(
            "session_messages_saved app=%s session=%s count=%d",
            self.app_id, self.session_id, len(messages),
        )

    async def append_messages(self, messages: list[dict[str, Any]], start_seq: int) -> None:
        """Append new messages starting from start_seq (incremental save)."""
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import SessionMessage

        session_pk = await self._ensure_session()

        async with get_session_factory()() as db:
            for i, msg in enumerate(messages):
                db.add(SessionMessage(
                    session_pk=session_pk,
                    seq=start_seq + i,
                    role=msg.get("role", ""),
                    content=msg.get("content"),
                    tool_call_id=msg.get("tool_call_id"),
                    tool_calls=msg.get("tool_calls"),
                    name=msg.get("name"),
                ))
            await db.commit()

    async def load_messages(self) -> list[dict[str, Any]]:
        """Load all messages for this session, ordered by seq."""
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import SessionMessage, UserSession

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
                select(SessionMessage)
                .where(SessionMessage.session_pk == session_pk)
                .order_by(SessionMessage.seq)
            )
            rows = result.scalars().all()

        messages = []
        for row in rows:
            msg: dict[str, Any] = {"role": row.role}
            if row.content is not None:
                msg["content"] = row.content
            if row.tool_call_id:
                msg["tool_call_id"] = row.tool_call_id
            if row.tool_calls:
                msg["tool_calls"] = row.tool_calls
            if row.name:
                msg["name"] = row.name
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
    ) -> None:
        """Save or update the checkpoint for this session."""
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
        from digitorn.core.models import SessionCheckpoint, SessionMessage, UserSession

        async with get_session_factory()() as db:
            result = await db.execute(
                select(UserSession.id).where(
                    UserSession.app_id == self.app_id,
                    UserSession.session_id == self.session_id,
                )
            )
            session_pk = result.scalar_one_or_none()

            if session_pk:
                await db.execute(
                    delete(SessionMessage).where(SessionMessage.session_pk == session_pk)
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
