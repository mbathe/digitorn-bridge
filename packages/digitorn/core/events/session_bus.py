"""SocketIOBus - Socket.IO-backed session event bus.

Replaces the old in-process ``SessionEventBus``. Same public API so
the runtime agent (manager.py, hooks.py, apps.py) does not change -
but under the hood every ``publish(key, event)`` call turns into a
room-scoped ``sio.emit(...)`` plus an append in the replay buffer.

Key formats (kept for backward compat with the agent runtime):
    session key: "{app_id}:{user_id}:{session_id}"
    user key:    "user:{user_id}"

Room routing (most-specific first, per client-side spec):
    has session_id → ``session:{session_id}``
    has app_id     → ``app:{app_id}``
    otherwise      → ``user:{user_id}``

The envelope shape emitted to clients is the cross-language contract::

    {
      "type":       "token",
      "seq":        42,
      "kind":       "session",
      "app_id":     "my-app",
      "session_id": "sess-1234",
      "payload":    {...},
      "ts":         "2026-04-15T10:23:11.000Z"
    }

Internal consumers (InboxProducer) subscribe via ``add_handler()``
instead of polling a subscribers dict. Each handler receives
``(user_id, envelope)`` for every published event.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from digitorn.core.events.event_buffer import EventBuffer

logger = logging.getLogger(__name__)

HandlerFn = Callable[[str, dict[str, Any]], Awaitable[None]]


# Raw runtime event type → logical "kind" column used by the inbox
# producer and the client-side event router. Unknown types get "session".
# FULL PERSISTENCE MODE - nothing is dropped from the durable log.
#
# Every event published on the session bus lands in the DB. Not just
# the logical milestones (user_message, tool_call, message_done) but
# also fine-grained streaming deltas (token, thinking_delta,
# assistant_stream_snapshot, preview:delta, …). The bet: DB bytes
# are cheap, losing user conversation history is not.
#
# This frozenset stays defined as an empty set so the filter hook
# still exists in case a future deployment wants to re-enable a few
# very-high-volume types (append here) - but by default every type
# is persisted. Paired with the fire-and-forget bg writer below, the
# agent loop never blocks on IO.
_EPHEMERAL_EVENT_TYPES: frozenset[str] = frozenset()


async def _persist_event(**kwargs: Any) -> None:
    """Enqueue one event into the batched ``history_log`` writer and
    AWAIT the enqueue.

    Universal-truth invariant: callers ``await`` this BEFORE the
    Socket.IO broadcast. The previous fire-and-forget
    (``asyncio.create_task``) implementation let the broadcast race
    ahead of the writer enqueue, so a client that received the event
    over the wire could reconnect milliseconds later and find no DB
    row yet (``async_replay`` returned a phantom gap). Awaiting the
    enqueue closes the window: the row is queued in the writer
    before the client ever sees the seq.

    Effectively O(1) - the writer's ``enqueue`` is a single
    ``asyncio.Queue.put_nowait`` under the hood. The actual DB
    commit still happens asynchronously in the background batch.
    On full queue / no writer, :func:`digitorn.core.history.record`
    falls back to a synchronous insert so the event still lands.
    """
    try:
        from digitorn.core.history import record as _record
    except Exception:
        return

    # Preserve the SessionBus routing ``kind`` (session / agent /
    # system) in payload so clients reading from history_log see it
    # the same way they saw it live.
    payload = dict(kwargs.get("payload") or {})
    legacy_kind = kwargs.get("kind")
    if legacy_kind:
        payload.setdefault("event_kind", legacy_kind)

    try:
        await _record(
            kind="event",
            type=kwargs.get("type", ""),
            app_id=kwargs.get("app_id"),
            session_id=kwargs.get("session_id"),
            user_id=kwargs.get("user_id"),
            seq=kwargs.get("seq", 0),
            payload=payload,
            correlation_id=kwargs.get("correlation_id", ""),
        )
    except Exception as exc:
        # Never let a persist failure crash the agent loop. The
        # ``error`` log is loud so operators see it. The UNIQUE
        # constraint on (session_id, seq) catches duplicates here -
        # legitimate failures of the writer (DB down) leave a gap that
        # the client will spot on the next reconnect-replay.
        logger.error(
            "persist_event_failed type=%s sid=%s seq=%s: %s",
            kwargs.get("type"), kwargs.get("session_id"),
            kwargs.get("seq"), exc,
        )


_EVENT_KIND_MAP: dict[str, str] = {
    # ── Agent session events ──────────────────────────────────────
    "user_message": "session",
    "message_queued": "session",
    "message_merged": "session",
    "message_replaced": "session",
    "message_started": "session",
    "message_done": "session",
    "message_cancelled": "session",
    "queue_full": "session",
    "result": "session",
    "turn_complete": "session",
    "stream_done": "session",
    "token": "session",
    "token_usage": "session",
    "thinking": "session",
    "thinking_started": "session",
    "thinking_delta": "session",
    "tool_start": "session",
    "tool_call": "session",
    "memory_update": "session",
    "agent_event": "session",
    "hook": "session",
    "hook_notification": "session",
    "abort": "session",
    "out_token": "session",
    "in_token": "session",
    "bg_task_update": "session",
    "terminal_output": "session",
    "notification": "session",
    # ── Preview module events ─────────────────────────────────────
    "preview:state_changed": "session",
    "preview:state_patched": "session",
    "preview:cleared": "session",
    "preview:resource_set": "session",
    "preview:resource_patched": "session",
    "preview:resource_deleted": "session",
    "preview:resource_bulk_set": "session",
    "preview:channel_cleared": "session",
    "preview:snapshot": "session",
    # ── Widget module events ──────────────────────────────────────
    "widget:render": "session",
    "widget:update": "session",
    "widget:close": "session",
    "widget:error": "session",
    "widget:state": "session",
    "widget:cleared": "session",
    "widget:snapshot": "session",
    # ── Credentials ───────────────────────────────────────────────
    "credential_required": "session",
    "credential_auth_required": "session",
    # ── Error / approval / background ─────────────────────────────
    "error": "error",
    "approval_request": "approval",
    "notification": "background_activation",
    "notification_result": "background_activation",
    "status": "status",
}


# Backpressure limits for Socket.IO emit
_EMIT_MAX_CONCURRENT = 500   # Max concurrent emits before backpressure kicks in
_EMIT_TIMEOUT = 5.0          # Abandon emit after this many seconds


class SocketIOBus:
    """Session event bus backed by Socket.IO rooms + an in-memory replay buffer."""

    def __init__(
        self,
        sio: Any,
        buffer: EventBuffer | None = None,
        live_ops: Any = None,
    ) -> None:
        self._sio = sio
        self._buffer = buffer or EventBuffer()
        self._handlers: list[HandlerFn] = []
        self._emit_semaphore = asyncio.Semaphore(_EMIT_MAX_CONCURRENT)
        self._dropped_count = 0
        # In-progress operations registry. ``None`` is allowed so the
        # bus stays usable in tests / migrations that haven't wired the
        # KV backend yet - emit() guards on it.
        self._live_ops = live_ops

    def set_live_ops(self, live_ops: Any) -> None:
        """Inject the live ops registry after construction.

        ``server.py`` builds the SocketIOBus before the KV-backed
        registry exists (the bus is needed by Socket.IO itself, which
        is created earlier in lifespan setup). This setter lets the
        wiring step plug in the registry once the KV backend is up.
        """
        self._live_ops = live_ops

    # ── Key helpers (unchanged API) ────────────────────────────────

    @staticmethod
    def session_key(app_id: str, session_id: str, user_id: str = "local") -> str:
        return f"{app_id}:{user_id}:{session_id}"

    @staticmethod
    def user_key(user_id: str) -> str:
        return f"user:{user_id or 'local'}"

    @staticmethod
    def _parse_key(key: str) -> tuple[str | None, str, str | None]:
        """Return (app_id, user_id, session_id). Missing parts are None."""
        if key.startswith("user:"):
            return None, key[5:] or "local", None
        parts = key.split(":", 2)
        if len(parts) != 3:
            return None, "local", None
        return parts[0], parts[1], parts[2]

    # ── Internal consumer handlers (replaces subscribe/queue) ──────

    def add_handler(self, handler: HandlerFn) -> None:
        """Register an in-process handler called on every publish.

        Signature: ``async def handler(user_id: str, envelope: dict) -> None``.
        Used by InboxProducer and anything else that needs to react to
        events without going through a Socket.IO client.
        """
        self._handlers.append(handler)

    def remove_handler(self, handler: HandlerFn) -> None:
        try:
            self._handlers.remove(handler)
        except ValueError:
            pass

    # ── Publish (called by the agent runtime) ──────────────────────

    async def emit(self, event: "SessionEvent") -> int:
        """Primary emission path - takes a fully-validated
        :class:`SessionEvent`.

        The bus handles only the transport concerns: assigning ``seq``,
        writing to the ring buffer, persisting non-ephemeral events,
        fanning out to Socket.IO rooms. The caller is responsible for
        the contract (op_id, op_type, op_state, scope).

        Returns the authoritative ``seq`` assigned to this event by
        the ring buffer (monotonic per-session). Callers that need to
        echo this number back to a client (e.g. POST /messages must
        return the seq of the user_message it just emitted, since the
        seq is the ONLY ordering source-of-truth for the chat) should
        capture this value. Returns ``0`` only when the buffer's
        append failed silently - never raises.

        Was returning the literal ``1`` for legacy dict-based
        ``publish`` compat; that contract was unused in production
        (no caller captured the return) so we now expose the real
        seq. Existing callers that ignored the return value are
        unaffected.
        """
        from digitorn.core.events.envelope import SessionEvent as _SE

        if not isinstance(event, _SE):
            raise TypeError(
                f"emit() requires a SessionEvent, got {type(event).__name__}"
            )

        # Ring-buffer append owns the seq assignment - ``with_seq`` is
        # called here so the frozen event carries the attribution from
        # this point on.
        envelope_raw = self._buffer.append(
            user_id=event.user_id,
            type=event.type,
            kind=event.kind,
            payload=event.to_dict()["payload"],
            app_id=event.app_id,
            session_id=event.session_id,
        )
        seq = int(envelope_raw.get("seq") or 0)
        event = event.with_seq(seq)
        envelope = event.to_dict()

        if event.session_id and event.type not in _EPHEMERAL_EVENT_TYPES:
            # Fire-and-forget DB persistence. The agent loop never
            # blocks on IO. Row order is maintained by the ``seq``
            # column (unique, monotonic, assigned before scheduling).
            try:
                # Contract fields live at the envelope TOP LEVEL for
                # the live Socket.IO wire - but the DB row only keeps
                # a JSON ``payload`` column, so we merge them in here
                # to survive persistence. ``/active-ops`` reconstructs
                # by reading them back from ``payload``. Without this,
                # reconnect-after-crash would never see op_id/op_state.
                persisted_payload = dict(envelope["payload"])
                persisted_payload.setdefault("op_id", event.op_id)
                persisted_payload.setdefault("op_type", event.op_type.value)
                persisted_payload.setdefault("op_state", event.op_state.value)
                if event.op_parent_id:
                    persisted_payload.setdefault(
                        "op_parent_id", event.op_parent_id,
                    )
                if event.event_id:
                    persisted_payload.setdefault("event_id", event.event_id)
                # AWAIT persist BEFORE broadcast so the row is in the
                # writer queue before the client can possibly receive
                # the seq. Universal-truth invariant: every seq the
                # client sees is already accounted for in history_log.
                await _persist_event(
                    app_id=event.app_id,
                    session_id=event.session_id,
                    user_id=event.user_id,
                    type=event.type,
                    kind=event.kind,
                    seq=seq,
                    payload=persisted_payload,
                    correlation_id=event.correlation_id or "",
                )
            except Exception as exc:
                logger.debug("session_event_persist_failed: %s", exc)

        # Update the in-progress ops registry. Non-terminal events
        # refresh the entry so a join_session reads the latest envelope
        # for each active op; terminal events remove the entry. Runs
        # AFTER persistence (so the registry never points at an event
        # that isn't on disk) and BEFORE the socket emit (so a client
        # that joins the room a microsecond later sees a consistent
        # registry view). Best-effort - never blocks the emit path.
        if self._live_ops is not None and event.session_id:
            try:
                self._live_ops.record(event)
            except Exception as exc:
                logger.debug("live_ops_record_swallowed: %s", exc)

        if event.session_id:
            room = f"session:{event.session_id}"
        elif event.app_id:
            room = f"app:{event.app_id}"
        else:
            room = self.user_key(event.user_id)

        await self._emit(room, envelope)

        # NOTE: ``approval_request`` previously fanned out to the
        # user-level room ``user:<uid>`` so a global inbox/badge UI
        # could display approvals across every session. That fanout
        # was removed because the strict session isolation contract
        # (see ``socketio_bus.on_join_session``) requires that a
        # client joined to ``session:<sid>`` receive ONLY events
        # tagged with that exact session_id. The session-scoped emit
        # above is the single source of truth; clients that need a
        # cross-session inbox poll the REST endpoint
        # ``/api/users/me/inbox`` instead.

        if self._handlers:
            for h in self._handlers:
                try:
                    await h(event.user_id, envelope)
                except Exception as exc:
                    logger.warning(
                        "bus_handler_error type=%s: %s", event.type, exc,
                    )

        return seq

    async def publish(self, key: str, event: dict[str, Any]) -> int:
        """Legacy dict emission - wraps into a SessionEvent.

        Kept so unmigrated call sites keep working while we sweep the
        codebase. Once every caller uses ``emit(SessionEvent(...))``
        directly this method is deleted.

        Best-effort contract backfill:
          * ``op_id`` is taken from ``data.op_id`` if present, else
            from ``data.correlation_id`` (turn-scoped default), else a
            generated ``op-legacy-<hex>``.
          * ``op_type`` / ``op_state`` come from the ``_LEGACY_OP_*``
            tables in :mod:`envelope`, which map every known event type
            to a sensible default. Unknown types fall back to
            ``SYSTEM`` + ``RUNNING`` so the event still ships.
        """
        from digitorn.core.events.envelope import (
            SessionEvent as _SE,
            OpType as _OpType,
            OpState as _OpState,
            _LEGACY_OP_TYPE,
            _LEGACY_OP_STATE,
            gen_op_id,
        )

        app_id, user_id, session_id = self._parse_key(key)
        raw_type = str(event.get("type") or "unknown")
        payload = event.get("data")
        if not isinstance(payload, dict):
            payload = {"data": payload} if payload is not None else {}

        # Contract backfill from legacy dicts.
        op_id = (
            str(payload.get("op_id") or "")
            or str(payload.get("correlation_id") or "")
            or gen_op_id("legacy")
        )
        op_type = payload.get("op_type")
        if isinstance(op_type, _OpType):
            pass
        elif isinstance(op_type, str):
            try:
                op_type = _OpType(op_type)
            except ValueError:
                op_type = _LEGACY_OP_TYPE.get(raw_type, _OpType.SYSTEM)
        else:
            op_type = _LEGACY_OP_TYPE.get(raw_type, _OpType.SYSTEM)

        op_state = payload.get("op_state")
        if isinstance(op_state, _OpState):
            pass
        elif isinstance(op_state, str):
            try:
                op_state = _OpState(op_state)
            except ValueError:
                op_state = _LEGACY_OP_STATE.get(raw_type, _OpState.RUNNING)
        else:
            op_state = _LEGACY_OP_STATE.get(raw_type, _OpState.RUNNING)

        correlation_id = str(payload.get("correlation_id") or "")
        op_parent_id = payload.get("op_parent_id")

        # Dev-mode discipline: these fields are optional on the legacy
        # path so partial migrations ship, but we refuse to accept an
        # event that can't identify the session it belongs to - that's
        # a leak vector.
        if not app_id or not session_id or not user_id:
            # Session-less events (cron ticks before a user opens the
            # app, daemon startup, …) still need to go somewhere. We
            # lower the envelope to a system kind and emit at the user
            # room when user_id is known, else drop silently. This is
            # the SAME behaviour the old publish path had.
            app_id = app_id or ""
            session_id = session_id or ""
            user_id = user_id or "system"

        # Strict contract: a session-scoped event without ``session_id``
        # is a caller bug. The old fallback landed these under
        # ``"anonymous_session"`` which meant they were persisted to a
        # DB row no client ever sees - effectively a silent drop. Fail
        # loud here so whichever caller forgot to carry session context
        # is visible in logs and gets fixed, rather than corrupting
        # history with orphan rows.
        if not session_id:
            logger.warning(
                "publish_legacy_missing_session_id type=%s - dropping event, "
                "caller must carry session context",
                raw_type,
            )
            return 0
        try:
            se = _SE.build(
                type=raw_type,
                app_id=app_id or "anonymous_app",
                session_id=session_id,
                user_id=user_id,
                op_id=op_id,
                op_type=op_type,
                op_state=op_state,
                correlation_id=correlation_id,
                op_parent_id=op_parent_id if isinstance(op_parent_id, str) else None,
                payload=payload,
            )
        except ValueError as exc:
            logger.warning("publish_legacy_contract_failed type=%s err=%s", raw_type, exc)
            return 0
        return await self.emit(se)

    async def _publish_legacy_passthrough(self, key: str, event: dict[str, Any]) -> int:
        """Preserved fallback for call sites that truly can't build a
        full SessionEvent yet (cross-user fanout needs access to the
        raw envelope). Walks through the original pre-envelope code
        path for 100% behavioural parity.
        """
        app_id, user_id, session_id = self._parse_key(key)
        raw_type = str(event.get("type") or "unknown")
        kind = _EVENT_KIND_MAP.get(raw_type, "session")
        payload = event.get("data")
        if not isinstance(payload, dict):
            payload = {"data": payload} if payload is not None else {}

        envelope = self._buffer.append(
            user_id=user_id,
            type=raw_type,
            kind=kind,
            payload=payload,
            app_id=app_id,
            session_id=session_id,
        )

        if session_id and raw_type not in _EPHEMERAL_EVENT_TYPES:
            # Awaited persist on the legacy dict path too - same
            # universal-truth invariant as the SessionEvent path above.
            try:
                correlation_id = ""
                if isinstance(payload, dict):
                    correlation_id = str(payload.get("correlation_id") or "")
                await _persist_event(
                    app_id=app_id or "",
                    session_id=session_id,
                    user_id=user_id,
                    type=raw_type,
                    kind=kind,
                    seq=envelope.get("seq", 0),
                    payload=payload,
                    correlation_id=correlation_id,
                )
            except Exception as exc:
                logger.debug("session_event_persist_failed: %s", exc)

        if session_id:
            room = f"session:{session_id}"
        elif app_id:
            room = f"app:{app_id}"
        else:
            room = self.user_key(user_id)

        await self._emit(room, envelope)

        # NOTE: ``approval_request`` fanout to the user-level room was
        # removed in favour of strict session isolation - see the
        # matching block in ``emit()`` above for the rationale.

        # In-process handlers (InboxProducer etc).
        if self._handlers:
            for h in self._handlers:
                try:
                    await h(user_id, envelope)
                except Exception as exc:
                    logger.warning("bus_handler_error type=%s: %s", raw_type, exc)

        return 1

    async def _emit(self, room: str, envelope: dict[str, Any]) -> None:
        if self._sio is None:
            logger.debug("session_bus._emit: sio is None, dropping event to room=%s", room)
            return
        evt_type = envelope.get("type", "?")
        try:
            async with asyncio.timeout(_EMIT_TIMEOUT):
                async with self._emit_semaphore:
                    await self._sio.emit(
                        "event", envelope, room=room, namespace="/events",
                    )
            if evt_type.startswith("preview:"):
                logger.info(
                    "preview_socketio_emit_ok: room=%s type=%s seq=%s",
                    room, evt_type, envelope.get("seq", "?"),
                )
        except TimeoutError:
            self._dropped_count += 1
            logger.warning(
                "socketio_emit_timeout room=%s type=%s dropped_total=%d",
                room, envelope.get("type", "?"), self._dropped_count,
            )
        except Exception as exc:
            self._dropped_count += 1
            logger.warning("socketio_emit_failed room=%s: %s", room, exc)

    # ── Replay helpers ─────────────────────────────────────────────

    def user_latest_seq(self, user_id: str, session_id: str | None = None) -> int:
        """Latest live seq for a scope. Forwards to ``EventBuffer``.

        ``session_id`` lets callers in ``socketio_bus`` ask for the
        per-session counter directly (which is the one the client
        actually de-dups against). Without it, the counter for the
        per-user scope is returned (inbox / approvals).
        """
        return self._buffer.get_latest_seq(user_id, session_id)

    def user_replay(
        self,
        user_id: str,
        since_seq: int,
        *,
        app_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Legacy sync replay from the in-memory ring buffer. Kept for
        callers that can't await. Prefer ``async_replay`` which reads
        from the durable ``history_log`` table and survives daemon
        restarts."""
        return self._buffer.replay(
            user_id, since_seq, app_id=app_id, session_id=session_id,
        )

    async def async_replay(
        self,
        user_id: str,
        since_seq: int,
        *,
        session_id: str | None = None,
        limit: int = 50000,
        include_all_users: bool = False,
    ) -> list[dict[str, Any]]:
        """Durable replay from the ``history_log`` DB table.

        Single source of truth - survives daemon restart, ring buffer
        rollover, and cross-device handoff. Used by Socket.IO
        ``join_session`` and the HTTP ``GET /events`` endpoint so both
        paths return identical chronology.

        Returns envelopes ordered by ``seq`` ascending, filtered to
        ``session_id`` when provided (no cross-session leak).
        """
        try:
            from digitorn.core.database import get_session_factory
            from digitorn.core.models import HistoryLog
            from sqlalchemy import select, or_
        except Exception:
            return []
        try:
            sf = get_session_factory()
        except RuntimeError:
            return []  # DB not initialised

        stmt = (
            select(HistoryLog)
            .where(HistoryLog.kind == "event")
            .where(HistoryLog.seq > int(since_seq or 0))
            .order_by(HistoryLog.seq.asc(), HistoryLog.ts.asc())
            .limit(limit)
        )
        if session_id:
            stmt = stmt.where(HistoryLog.session_id == session_id)
        elif not include_all_users:
            stmt = stmt.where(
                or_(
                    HistoryLog.user_id == (user_id or ""),
                    HistoryLog.user_id.is_(None),
                    HistoryLog.user_id == "",
                )
            )
        async with sf() as db:
            r = await db.execute(stmt)
            rows = r.scalars().all()
        # Contract fields (event_id/op_id/op_type/op_state/op_parent_id)
        # live in the DB ``payload`` JSON column (the only place a
        # non-typed column exists in ``session_events``). Live emissions
        # put them at the ENVELOPE top level via ``SessionEvent.to_dict``;
        # we must do the same reconstruction here so the replay wire
        # shape matches the live wire shape 1:1. Without this, clients
        # have to read the contract from two different locations.
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row.payload or {})
            # The unified table carries kind in {"event","message","audit"}
            # but clients used to see SessionBus ``kind`` values
            # ("session"/"agent"/"system"). Restore the original kind
            # from payload.event_kind - set on dual-write.
            effective_kind = payload.pop("event_kind", None) or row.kind
            env: dict[str, Any] = {
                "type": row.type,
                "kind": effective_kind,
                "seq": row.seq,
                "ts": row.ts.isoformat() if row.ts else None,
                "app_id": row.app_id or None,
                "session_id": row.session_id or None,
                "user_id": row.user_id or None,
                "correlation_id": row.correlation_id or None,
                "payload": payload,
            }
            # Promote the persisted contract fields to the top level.
            # Payload keeps them too for backward-compat with clients
            # already reading from payload - until every consumer is
            # migrated, this duplication is intentional.
            for _key in (
                "event_id", "op_id", "op_type", "op_state", "op_parent_id",
            ):
                val = payload.get(_key)
                if val is not None:
                    env[_key] = val
            out.append(env)
        return out

