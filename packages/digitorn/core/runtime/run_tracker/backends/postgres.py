"""Postgres backend for cloud mode."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select, text, update

from digitorn.core.database import get_session_factory
from digitorn.core.models import (
    AgentRun, AgentRunEvent, SessionAgent, UserSession,
)

logger = logging.getLogger(__name__)


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


class PostgresBackend:
    """Default cloud backend. Writes go through the daemon's main"""

    def __init__(self, **_: Any) -> None:
        # Nothing to configure: the engine is owned by
        # digitorn.core.database; we just look it up at call time.
        pass

    async def setup(self) -> None:
        return

    async def teardown(self) -> None:
        return


    async def start_run(
        self,
        *,
        run_id: str,
        ctx_snapshot: dict[str, Any],
        max_turns: Optional[int],
        parent_run_id: Optional[str],
        task_summary: Optional[str],
        queued_at_iso: str,
        started_at_iso: str,
    ) -> None:
        user_id = ctx_snapshot["user_id"]
        app_id = ctx_snapshot["app_id"]
        session_id = ctx_snapshot["session_id"]
        agent_id = ctx_snapshot.get("agent_id") or "default"

        factory = get_session_factory()
        async with factory() as db:
            session_pk = (await db.execute(
                select(UserSession.id).where(
                    UserSession.app_id == app_id,
                    UserSession.session_id == session_id,
                )
            )).scalar_one_or_none()

            if session_pk is None:
                try:
                    session_obj = UserSession(
                        app_id=app_id,
                        session_id=session_id,
                        user_id=user_id,
                        workspace=ctx_snapshot.get("workspace") or "",
                    )
                    db.add(session_obj)
                    await db.flush()
                    session_pk = session_obj.id
                except Exception:
                    # Lost the race - re-fetch and continue.
                    await db.rollback()
                    session_pk = (await db.execute(
                        select(UserSession.id).where(
                            UserSession.app_id == app_id,
                            UserSession.session_id == session_id,
                        )
                    )).scalar_one_or_none()
                    if session_pk is None:
                        raise

            # 2. Resolve / create the SessionAgent row. Postgres-side
            #    ON CONFLICT keeps this idempotent under concurrency.
            sa_row = (await db.execute(
                select(SessionAgent).where(
                    SessionAgent.session_pk == session_pk,
                    SessionAgent.agent_id == agent_id,
                )
            )).scalar_one_or_none()
            if sa_row is None:
                sa_row = SessionAgent(
                    session_pk=session_pk,
                    agent_id=agent_id,
                    name=ctx_snapshot.get("agent_name"),
                )
                db.add(sa_row)
                await db.flush()

            # 3. Insert the run row.
            run = AgentRun(
                id=run_id,
                session_agent_id=sa_row.id,
                session_pk=session_pk,
                user_id=user_id,
                parent_run_id=parent_run_id,
                status="active",
                specialist=agent_id,
                provider=ctx_snapshot.get("provider"),
                model=ctx_snapshot.get("model"),
                max_turns=max_turns,
                task_summary=task_summary,
                queued_at=_parse_iso(queued_at_iso),
                started_at=_parse_iso(started_at_iso),
            )
            db.add(run)
            await db.commit()


    async def complete_run(
        self,
        *,
        run_id: str,
        status: str,
        prompt_tokens: int,
        completion_tokens: int,
        turns_used: int,
        status_reason: Optional[str],
        completed_at_iso: str,
    ) -> None:
        completed_at = _parse_iso(completed_at_iso)

        factory = get_session_factory()
        async with factory() as db:
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(
                    status=status,
                    status_reason=status_reason,
                    completed_at=completed_at,
                    last_event_at=completed_at,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    turns_used=turns_used,
                )
            )
            # Stamp last_completed_run_id on the parent session for the
            # dashboard's "resume last run" jump. Only on success.
            if status == "completed":
                await db.execute(text("""
                    UPDATE user_sessions
                       SET last_completed_run_id = CAST(:run_id AS VARCHAR(64))
                     WHERE id = (
                         SELECT session_pk FROM agent_runs
                         WHERE id = CAST(:run_id AS VARCHAR(64))
                     )
                """), {"run_id": run_id})
            await db.commit()


    async def emit_event(
        self,
        *,
        run_id: str,
        event_type: str,
        data: dict[str, Any],
        sequence: int,
        emitted_at_iso: str,
    ) -> None:
        emitted_at = _parse_iso(emitted_at_iso)

        factory = get_session_factory()
        async with factory() as db:
            await db.execute(text("""
                INSERT INTO agent_run_events
                    (run_id, sequence, event_type, data, elapsed_ms, created_at)
                SELECT
                    CAST(:run_id AS VARCHAR(64)),
                    CAST(:sequence AS INTEGER),
                    CAST(:event_type AS VARCHAR(32)),
                    CAST(:data AS JSONB),
                    CASE
                        WHEN ar.started_at IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (CAST(:emitted_at AS TIMESTAMPTZ) - ar.started_at)) * 1000
                        ELSE NULL
                    END,
                    CAST(:emitted_at AS TIMESTAMPTZ)
                FROM agent_runs ar
                WHERE ar.id = CAST(:run_id AS VARCHAR(64))
            """), {
                "run_id": run_id,
                "sequence": sequence,
                "event_type": event_type,
                "data": _json_dumps(data),
                "emitted_at": emitted_at,
            })
            # Heartbeat the run.
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(last_event_at=emitted_at)
            )
            await db.commit()


    async def increment_turns(self, *, run_id: str) -> None:
        factory = get_session_factory()
        async with factory() as db:
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(turns_used=AgentRun.turns_used + 1)
            )
            await db.commit()

    async def increment_sub_agents(self, *, run_id: str) -> None:
        factory = get_session_factory()
        async with factory() as db:
            await db.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(sub_agents_spawned=AgentRun.sub_agents_spawned + 1)
            )
            await db.commit()


# Local helper - import json lazily so this module is cheap to import.
def _json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, default=str, ensure_ascii=False)
