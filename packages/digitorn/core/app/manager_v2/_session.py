"""_SessionMixin - ConversationSession lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from digitorn.core.app.sessions import ConversationSession

logger = logging.getLogger(__name__)


async def _aggregate_gateway_usage(
    app_id: str, session_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Aggregate `gateway_usage_events` by `external_sid` for one app."""
    if not session_ids:
        return {}
    try:
        from digitorn.core.database import get_session_factory
        from sqlalchemy import text
    except Exception:
        return {}
    sql = text(
        """
        SELECT external_sid AS sid,
               SUM(prompt_tokens) AS prompt_tokens,
               SUM(completion_tokens) AS completion_tokens,
               SUM(total_cost_usd) AS cost_usd
        FROM gateway_usage_events
        WHERE app_id = :app_id
          AND external_sid = ANY(:sids)
        GROUP BY external_sid
        """
    )
    out: dict[str, dict[str, Any]] = {}
    try:
        async with get_session_factory()() as db:
            res = await db.execute(
                sql, {"app_id": app_id, "sids": list(session_ids)},
            )
            for row in res:
                d = dict(row._mapping)
                out[d["sid"]] = {
                    "prompt_tokens": int(d.get("prompt_tokens") or 0),
                    "completion_tokens": int(d.get("completion_tokens") or 0),
                    "cost_usd": float(d.get("cost_usd") or 0.0),
                }
    except Exception as exc:
        logger.debug(
            "gateway_usage_aggregate_failed app=%s n=%d: %s",
            app_id, len(session_ids), exc,
        )
    return out


class _SessionMixin:
    """Methods that operate on ConversationSession lifecycle."""

    async def get_session(self, app_id: str, session_id: str, user_id: str | None = None) -> ConversationSession | None:
        """Get a conversation session - single source of truth = DB."""
        uid = user_id or "local"
        session = await asyncio.to_thread(
            self._session_store.get, app_id, session_id, user_id=uid,
        )
        if session is not None:
            return session

        # Cache miss → rebuild from the DB (source of truth).
        return await self._rebuild_session_from_db(
            app_id, session_id, user_id=uid,
        )

    async def _rebuild_session_from_db(
        self, app_id: str, session_id: str, user_id: str,
    ) -> ConversationSession | None:
        """Reconstruct a ConversationSession from the durable DB rows."""
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import HistoryLog, UserSession
        from sqlalchemy import select
        from digitorn.core.app.sessions import ConversationSession

        try:
            factory = get_session_factory()
        except Exception as exc:
            logger.debug("session rebuild: DB not ready: %s", exc)
            return None

        def _row_to_msg(m: HistoryLog) -> dict[str, Any]:
            msg: dict[str, Any] = {"role": m.role or ""}
            # Multimodal messages carry their structured `raw_content`
            # in payload - prefer it so images / documents replay intact.
            raw = None
            if isinstance(m.payload, dict):
                raw = m.payload.get("raw_content")
            if raw is not None:
                msg["content"] = raw
            elif m.content is not None:
                msg["content"] = m.content
            if m.tool_call_id:
                msg["tool_call_id"] = m.tool_call_id
            if m.tool_calls:
                msg["tool_calls"] = m.tool_calls
            if m.name:
                msg["name"] = m.name
            return msg

        try:
            async with factory() as db:
                row = (
                    await db.execute(
                        select(UserSession).where(
                            UserSession.app_id == app_id,
                            UserSession.session_id == session_id,
                            UserSession.user_id == user_id,
                        )
                    )
                ).scalar_one_or_none()

                if row is None:
                    return None

                compaction_row = (
                    await db.execute(
                        select(HistoryLog)
                        .where(HistoryLog.kind == "event")
                        .where(HistoryLog.type == "compaction")
                        .where(HistoryLog.app_id == app_id)
                        .where(HistoryLog.session_id == session_id)
                        .order_by(HistoryLog.seq.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()

                messages: list[dict[str, Any]] = []
                memory_snapshot: dict[str, Any] | None = None

                if compaction_row is not None and isinstance(
                    compaction_row.payload, dict
                ):
                    payload = compaction_row.payload
                    kept_from_seq = int(
                        (payload.get("kept_range") or {}).get("from_seq", 0)
                    )

                    original_system = (
                        await db.execute(
                            select(HistoryLog)
                            .where(HistoryLog.kind == "message")
                            .where(HistoryLog.app_id == app_id)
                            .where(HistoryLog.session_id == session_id)
                            .where(HistoryLog.role == "system")
                            .where(HistoryLog.seq < kept_from_seq)
                            .order_by(HistoryLog.seq.asc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if original_system is not None:
                        messages.append(_row_to_msg(original_system))

                    # The compacted system note - reconstructed from the
                    # frozen snapshot (summary + tools + memory + …).
                    from digitorn.core.runtime.compaction_persistence import (
                        build_system_note_from_payload,
                    )
                    messages.append(build_system_note_from_payload(payload))

                    # Kept + post-compaction messages
                    kept_rows = (
                        await db.execute(
                            select(HistoryLog)
                            .where(HistoryLog.kind == "message")
                            .where(HistoryLog.app_id == app_id)
                            .where(HistoryLog.session_id == session_id)
                            .where(HistoryLog.seq >= kept_from_seq)
                            .order_by(HistoryLog.seq.asc())
                        )
                    ).scalars().all()
                    messages.extend(_row_to_msg(m) for m in kept_rows)

                    mem = payload.get("memory_snapshot")
                    if isinstance(mem, dict) and mem:
                        memory_snapshot = mem

                    logger.info(
                        "session_rebuild_compacted app=%s session=%s "
                        "kept_from_seq=%d kept_msgs=%d",
                        app_id, session_id, kept_from_seq, len(kept_rows),
                    )
                else:
                    # No compaction on record - full history rebuild
                    # (the original behaviour).
                    msg_rows = (
                        await db.execute(
                            select(HistoryLog)
                            .where(HistoryLog.kind == "message")
                            .where(HistoryLog.app_id == app_id)
                            .where(HistoryLog.session_id == session_id)
                            .order_by(HistoryLog.seq.asc())
                        )
                    ).scalars().all()
                    messages.extend(_row_to_msg(m) for m in msg_rows)

            title = ""
            for m in messages:
                if m.get("role") == "user":
                    content = m.get("content") or ""
                    if isinstance(content, list):
                        content = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict)
                        )
                    title = str(content)[:80]
                    break

            session = ConversationSession(
                session_id=session_id,
                app_id=app_id,
                user_id=user_id,
                messages=messages,
                title=title,
                created_at=(
                    row.created_at.timestamp()
                    if row.created_at else time.time()
                ),
                last_active=(
                    row.last_active_at.timestamp()
                    if row.last_active_at else time.time()
                ),
                workspace=getattr(row, "workspace", "") or "",
                workdir=getattr(row, "workdir", "") or "",
                memory_snapshot=memory_snapshot or {},
            )

            # Warm the cache so the next read is hot. Idempotent -
            # race-safe even if multiple concurrent misses fire.
            try:
                await asyncio.to_thread(self._session_store.put, session)
            except Exception as exc:
                logger.debug("session rebuild: cache warmup failed: %s", exc)

            logger.info(
                "session_rebuilt_from_db app=%s session=%s user=%s messages=%d",
                app_id, session_id, user_id, len(messages),
            )
            return session
        except Exception as exc:
            logger.warning(
                "session_rebuild_failed app=%s session=%s: %s",
                app_id, session_id, exc, exc_info=True,
            )
            return None

    async def end_session(self, app_id: str, session_id: str, user_id: str = "local") -> bool:
        """End and remove a conversation session."""
        try:
            deployed = self.get(app_id, user_id=user_id)
            cb = getattr(deployed, "context_builder", None) if deployed else None
            hook_runner = getattr(cb, "hook_runner", None) if cb else None
            if hook_runner is not None:
                from digitorn.core.runtime.hooks import TurnState
                state = TurnState(
                    messages=[],
                    turn=0, max_turns=0, tool_calls_count=0,
                    agent_id="",
                )
                state._session_id = session_id  # type: ignore[attr-defined]
                await hook_runner.run("session_end", state)
        except Exception as exc:
            logger.debug("session_end hook failed: %s", exc)

        try:
            loop = asyncio.get_running_loop()
            _task = loop.create_task(self.cleanup_session(app_id, session_id))
            if not hasattr(self, "_end_session_cleanup_tasks"):
                self._end_session_cleanup_tasks = set()
            self._end_session_cleanup_tasks.add(_task)
            _task.add_done_callback(self._end_session_cleanup_tasks.discard)
        except RuntimeError:
            pass  # No event loop - standalone CLI, resources will be cleaned on undeploy

        # Drop the on-disk preview snapshot so the session dir does
        # not outlive the session itself.
        try:
            await self._delete_session_workspace_snapshot(
                app_id, session_id, user_id,
            )
        except Exception as exc:
            logger.warning(
                "session_workspace_snapshot_delete_failed sid=%s: %s",
                session_id, exc,
            )

        return await asyncio.to_thread(self._session_store.delete, app_id, session_id, user_id=user_id)

    async def _delete_session_workspace_snapshot(
        self, app_id: str, session_id: str, user_id: str,
    ) -> None:
        """`rmtree` the session's preview snapshot dir at"""
        try:
            deployed = self.get(app_id, user_id=user_id)
            preview_mod = (
                deployed.modules.get("preview") if deployed else None
            )
            ws = ""
            if preview_mod is not None:
                ws = (
                    getattr(preview_mod, "_session_workspaces", {})
                    .get(session_id)
                    or ""
                )
            if ws:
                import os
                import shutil
                snap_dir = os.path.join(
                    ws, ".digitorn", "sessions", session_id,
                )
                if os.path.isdir(snap_dir):
                    await asyncio.to_thread(
                        shutil.rmtree, snap_dir, True,  # ignore_errors
                    )
        except Exception as exc:
            logger.debug(
                "snapshot_disk_cleanup_failed sid=%s: %s",
                session_id, exc,
            )

    def is_session_active(self, app_id: str, session_id: str) -> bool:
        """In-memory check: a turn is currently held by this process."""
        return f"{app_id}:{session_id}" in self._active_sessions

    async def list_sessions(
        self,
        app_id: str,
        user_id: str | None = None,
        limit: int = 0,
        offset: int = 0,
        *,
        include_empty: bool = False,
    ) -> list[dict[str, Any]]:
        """List sessions for an app, optionally filtered by user."""
        if user_id:
            rows = await asyncio.to_thread(
                self._session_store.list_for_user,
                app_id, user_id, limit=0, offset=0,
            )
        else:
            rows = await asyncio.to_thread(
                self._session_store.list_for_app,
                app_id, limit=0, offset=0,
            )

        rows = [
            r.summary() if hasattr(r, "summary") else r
            for r in rows
        ]

        if not include_empty:
            # a session is committed if it has a message role, any completed turn, or an auto-set title.
            def _is_committed(r: dict) -> bool:
                if r.get("last_message_role") or "":
                    return True
                if int(r.get("turn_count") or 0) > 0:
                    return True
                if (r.get("title") or "").strip():
                    return True
                return False
            rows = [r for r in rows if _is_committed(r)]

        rows.sort(key=lambda s: s.get("last_active", 0) or 0, reverse=True)

        if offset:
            rows = rows[offset:]
        if limit:
            rows = rows[:limit]

        deployed = self._deployed.get(app_id)
        if deployed is not None:
            meta = deployed.compiled.meta
            app_name = getattr(meta, "name", app_id)
            app_icon = getattr(meta, "icon", "") or ""
            app_color = getattr(meta, "color", "") or ""
            for r in rows:
                r["app_name"] = app_name
                r["app_icon"] = app_icon
                r["app_color"] = app_color

        sids = [r.get("session_id") for r in rows if r.get("session_id")]
        totals = await _aggregate_gateway_usage(app_id, sids) if sids else {}
        for r in rows:
            t = totals.get(r.get("session_id"))
            if t:
                r["tokens"] = {
                    "prompt": t["prompt_tokens"],
                    "completion": t["completion_tokens"],
                    "total": t["prompt_tokens"] + t["completion_tokens"],
                }
                r["cost_usd"] = float(t["cost_usd"])
            else:
                r.setdefault("tokens", {"prompt": 0, "completion": 0, "total": 0})
                r.setdefault("cost_usd", 0.0)

        return rows

    async def count_sessions(
        self, app_id: str, user_id: str | None = None,
        *, include_empty: bool = False,
    ) -> int:
        """Count total sessions for an app/user (for pagination)."""
        if not include_empty:
            # Cheaper to reuse `list_sessions` (already filters) than
            # replicate the predicate. We only read length, not rows.
            rows = await self.list_sessions(
                app_id, user_id=user_id, limit=0, offset=0,
            )
            return len(rows)
        if user_id:
            return await asyncio.to_thread(self._session_store.count_for_user, app_id, user_id)
        return len(await asyncio.to_thread(self._session_store._index_get, app_id))

    async def cleanup_session(self, app_id: str, session_id: str) -> None:
        """Clean up all session-scoped resources (agents, notifications, tasks, metrics)."""
        deployed = self._deployed.get(app_id)
        if deployed is None:
            return

        # Clean agent_spawn
        for mod in deployed.modules.values():
            if hasattr(mod, "cleanup_session"):
                try:
                    await mod.cleanup_session(session_id)
                except Exception:
                    logger.debug("cleanup_session failed for module %s", mod, exc_info=True)

        # Clean context_builder resources
        cb = deployed.entry_context.context_builder
        if cb is not None:
            if hasattr(cb, "cleanup_session_queue"):
                cb.cleanup_session_queue(session_id)
            if hasattr(cb, "cleanup_session_bg_tasks"):
                try:
                    await cb.cleanup_session_bg_tasks(session_id)
                except Exception:
                    logger.debug("cleanup_session_bg_tasks failed", exc_info=True)
        # Clean session metrics - prevent unbounded memory growth
        try:
            from digitorn.core.runtime.session_metrics import remove_session_metrics
            remove_session_metrics(app_id, session_id)
        except Exception as exc:
            logger.debug("_session best-effort block failed: %s", exc)

        # Clean image store - prevent disk leak from session image directories
        try:
            from digitorn.core.image_store import get_image_store
            get_image_store().cleanup_session(session_id)
        except Exception:
            logger.debug("image_store_cleanup_failed session=%s", session_id, exc_info=True)

    async def load_session_events(
        self, app_id: str, session_id: str, *, user_id: str = "local",
    ) -> list[dict[str, Any]]:
        """Load persisted events for a session, seq-ordered, real-time."""
        bus = self.event_bus
        if bus is None:
            return []
        try:
            return await bus.async_replay(
                user_id or "local", 0, session_id=session_id,
            )
        except Exception as exc:
            logger.debug("load_session_events_failed: %s", exc)
            return []
