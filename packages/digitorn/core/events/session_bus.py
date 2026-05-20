"""SocketIOBus - Socket.IO-backed session event bus."""

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
    try:
        from digitorn.core.history import record as _record
    except Exception:
        return

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
        logger.error(
            "persist_event_failed type=%s sid=%s seq=%s: %s",
            kwargs.get("type"), kwargs.get("session_id"),
            kwargs.get("seq"), exc,
        )

_EVENT_KIND_MAP: dict[str, str] = {
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
    "preview:state_changed": "session",
    "preview:state_patched": "session",
    "preview:cleared": "session",
    "preview:resource_set": "session",
    "preview:resource_patched": "session",
    "preview:resource_deleted": "session",
    "preview:resource_bulk_set": "session",
    "preview:channel_cleared": "session",
    "preview:snapshot": "session",
    "widget:render": "session",
    "widget:update": "session",
    "widget:close": "session",
    "widget:error": "session",
    "widget:state": "session",
    "widget:cleared": "session",
    "widget:snapshot": "session",
    "credential_required": "session",
    "credential_auth_required": "session",
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
        # In-progress operations registry. `None` is allowed so the
        # bus stays usable in tests / migrations that haven't wired the
        # KV backend yet - emit() guards on it.
        self._live_ops = live_ops

    def set_live_ops(self, live_ops: Any) -> None:
        """Inject the live ops registry after construction."""
        self._live_ops = live_ops

    @staticmethod
    def session_key(app_id: str, session_id: str, user_id: str = "local") -> str:
        return f"{app_id}:{user_id}:{session_id}"

    @staticmethod
    def user_key(user_id: str) -> str:
        return f"user:{user_id or 'local'}"

    @staticmethod
    def _parse_key(key: str) -> tuple[str | None, str, str | None]:
        if key.startswith("user:"):
            return None, key[5:] or "local", None
        parts = key.split(":", 2)
        if len(parts) != 3:
            return None, "local", None
        return parts[0], parts[1], parts[2]

    def add_handler(self, handler: HandlerFn) -> None:
        """Register an in-process handler called on every publish."""
        self._handlers.append(handler)

    def remove_handler(self, handler: HandlerFn) -> None:
        try:
            self._handlers.remove(handler)
        except ValueError:
            pass

    async def emit(self, event: "SessionEvent") -> int:
        """Primary emission path - takes a fully-validated."""
        from digitorn.core.events.envelope import SessionEvent as _SE

        if not isinstance(event, _SE):
            raise TypeError(
                f"emit() requires a SessionEvent, got {type(event).__name__}"
            )

        # Ring-buffer append owns the seq assignment - `with_seq` is
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
            # blocks on IO. Row order is maintained by the `seq`
            # column (unique, monotonic, assigned before scheduling).
            try:
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
                # Await persist before broadcast: every seq a client
                # sees must already be queued for `history_log`.
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
        """Legacy dict emission - wraps into a SessionEvent."""
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

        # Reject events that can't identify a session (leak vector).
        if not app_id or not session_id or not user_id:
            # Session-less events (cron ticks, daemon startup) get demoted to
            # the user room when user_id is known, else dropped silently.
            app_id = app_id or ""
            session_id = session_id or ""
            user_id = user_id or "system"

        # Strict contract: session-scoped events without session_id are bugs;
        # fail loud here instead of writing orphan rows to history.
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

    def user_latest_seq(self, user_id: str, session_id: str | None = None) -> int:
        """Latest live seq for a scope. Forwards to `EventBuffer`."""
        return self._buffer.get_latest_seq(user_id, session_id)

    def user_replay(
        self,
        user_id: str,
        since_seq: int,
        *,
        app_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Legacy sync replay from the in-memory ring buffer. Kept."""
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
        """Durable replay from the `history_log` DB table."""
        # Session-scoped replays only; cross-session (`session_id=None`)
        # returns empty so callers use the per-session API.
        try:
            from digitorn.core.runtime.session_store.bridge import (
                get_default_bridge,
            )
        except Exception:
            return []
        bridge = get_default_bridge()
        if bridge is None:
            return []
        if not session_id:
            return []
        try:
            state = await bridge.store.open(
                session_id, app_id="", user_id=user_id or "",
                create_if_missing=False, pin=False,
            )
        except KeyError:
            return []
        except Exception:
            return []
        rows = [
            e for e in state.events
            if e.kind == "event" and e.seq > int(since_seq or 0)
        ]
        if not include_all_users and user_id:
            rows = [
                e for e in rows
                if (e.user_id or "") in (user_id, "")
            ]
        rows = rows[: int(limit)]
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row.payload or {})
            effective_kind = payload.pop("event_kind", None) or row.kind
            env: dict[str, Any] = {
                "type": row.type,
                "kind": effective_kind,
                "seq": row.seq,
                "ts": row.ts,
                "app_id": row.app_id or None,
                "session_id": row.session_id or None,
                "user_id": row.user_id or None,
                "correlation_id": row.correlation_id or None,
                "payload": payload,
            }
            for _key in (
                "event_id", "op_id", "op_type", "op_state", "op_parent_id",
            ):
                val = payload.get(_key)
                if val is not None:
                    env[_key] = val
            out.append(env)
        return out

