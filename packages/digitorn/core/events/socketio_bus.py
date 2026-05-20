"""Digitorn - Socket.IO server + event bridge."""

from __future__ import annotations


import logging

logger = logging.getLogger(__name__)
import asyncio
from typing import Any

import socketio
import structlog

from digitorn.core.events.bus import EventBus, EventHandler
from digitorn.core.events.router import EventRouter

logger = structlog.get_logger(__name__)

def _as_dict(data: Any) -> dict[str, Any]:
    return data if isinstance(data, dict) else {}

class SocketIOEventBus(EventBus):
    """FanoutEventBus backend for module-level `UniversalEvent`s."""

    def __init__(
        self,
        sio: socketio.AsyncServer,
        session_bus: Any | None = None,
    ) -> None:
        self._sio = sio
        self._router = EventRouter()

        self._session_bus = session_bus

    def attach_session_bus(self, session_bus: Any) -> None:
        """Late-bind the session bus so envelopes get proper `seq`/`kind`."""
        self._session_bus = session_bus

    def _envelope(self, event: "UniversalEvent") -> dict[str, Any]:
        from datetime import datetime, timezone

        event_type = event.event_type or "info"
        payload: dict[str, Any] = dict(event.data or {})
        # Carry causality + source info alongside the data so clients can
        # still filter on topic or correlate across the chain.
        payload.setdefault("topic", event.topic)
        if event.correlation_id:
            payload.setdefault("correlation_id", event.correlation_id)
        if event.causation_id:
            payload.setdefault("causation_id", event.causation_id)
        if event.source:
            payload.setdefault("source", event.source)

        if self._session_bus is not None:
            # Prefer the real seq + kind pipeline so this envelope is
            # indistinguishable from session-level events downstream.
            try:
                return self._session_bus._buffer.append(
                    user_id="",  # module events are not user-scoped
                    type=event_type,
                    kind=_EVENT_KIND_MAP_FALLBACK.get(event_type, "session"),
                    payload=payload,
                    app_id=event.app_id,
                    session_id=event.session_id,
                )
            except Exception as exc:
                logger.debug("socketio_bus best-effort block failed: %s", exc)

        # Bootstrap window: session bus not attached. Emit seq=-1 so clients drop it
        # (any non-positive seq means "ignore"); log for operator visibility.
        logger.warning(
            "module_event_dropped_pre_bootstrap topic=%s type=%s",
            event.topic, event_type,
        )
        return {
            "type": event_type,
            "seq": -1,
            "kind": _EVENT_KIND_MAP_FALLBACK.get(event_type, "session"),
            "app_id": event.app_id,
            "session_id": event.session_id,
            "payload": payload,
            "ts": datetime.now(timezone.utc).isoformat(),
            "_dropped_pre_bootstrap": True,
        }

    async def publish(self, event: "UniversalEvent") -> None:
        from digitorn.core.events.models import UniversalEvent  # noqa: F401

        envelope = self._envelope(event)
        namespace = "/events"

        # Route to one room only: session > app > broadcast.
        try:
            if event.session_id:
                await self._sio.emit(
                    "event", envelope,
                    room=f"session:{event.session_id}",
                    namespace=namespace,
                )
            elif event.app_id:
                await self._sio.emit(
                    "event", envelope,
                    room=f"app:{event.app_id}",
                    namespace=namespace,
                )
            else:
                await self._sio.emit(
                    "event", envelope,
                    room="broadcast",
                    namespace=namespace,
                )
        except Exception as exc:
            await logger.awarning("socketio_emit_error", error=str(exc))

        handlers = self._router.match(event.topic)
        if handlers:
            import asyncio
            await asyncio.gather(
                *(h(event) for h in handlers),
                return_exceptions=True,
            )

    def subscribe(self, pattern: str, handler: EventHandler) -> None:
        self._router.add(pattern, handler)

    def unsubscribe(self, pattern: str, handler: EventHandler) -> None:
        self._router.remove(pattern, handler)

# Minimal fallback map for module-level `event_type` values. The
# authoritative map lives in `session_bus.py`; we duplicate only the
# common buckets to avoid a circular import.
_EVENT_KIND_MAP_FALLBACK: dict[str, str] = {
    "info": "session",
    "progress": "session",
    "result": "session",
    "error": "error",
    "warning": "session",
}

def create_socketio_server(
    cors_allowed_origins: list[str] | str = "*",
    async_mode: str = "asgi",
    auth_service: Any = None,
    manager: Any = None,
    session_bus: Any = None,
    redis_url: str | None = None,
    **kwargs: Any,
) -> socketio.AsyncServer:
    """Create the Socket.IO server and register session-level handlers."""
    # Multi-worker support: use Redis as the Socket.IO message queue
    # so events emitted by one worker reach clients on all workers.
    sio_kwargs: dict[str, Any] = {
        "async_mode": async_mode,
        "cors_allowed_origins": cors_allowed_origins,
        "logger": False,
        "engineio_logger": False,
        "ping_interval": 60,
        "ping_timeout": 20,
        "max_http_buffer_size": 256_000,
    }
    if redis_url and redis_url.startswith(("redis://", "rediss://")):
        try:
            mgr = socketio.AsyncRedisManager(redis_url)
            sio_kwargs["client_manager"] = mgr
            logger.info(
                "socketio_redis_adapter_enabled url=%s",
                redis_url.split("@")[-1] if "@" in redis_url else redis_url,
            )
        except Exception as exc:
            logger.warning(
                "socketio_redis_adapter_failed (falling back to in-memory): %s", exc,
            )
    sio_kwargs.update(kwargs)
    sio = socketio.AsyncServer(**sio_kwargs)

    # Per-IP rate limiter - only counts REJECTED connections.
    _ws_connect_times: dict[str, list[float]] = {}
    try:
        from digitorn.core.config import get_settings
        _ws_cfg = get_settings().websocket
        _WS_RATE_WINDOW = _ws_cfg.rate_limit_window
        _WS_RATE_MAX = _ws_cfg.rate_limit_max_connections
    except Exception:
        _WS_RATE_WINDOW = 10.0
        _WS_RATE_MAX = 30

    def _ws_rate_ok(ip: str) -> bool:
        import time
        now = time.monotonic()
        times = _ws_connect_times.get(ip, [])
        times = [t for t in times if now - t < _WS_RATE_WINDOW]
        if len(times) >= _WS_RATE_MAX:
            _ws_connect_times[ip] = times
            return False
        times.append(now)
        _ws_connect_times[ip] = times
        if len(_ws_connect_times) > 1000:
            _ws_connect_times.clear()
        return True

    _sid_sessions: dict[str, dict[str, Any]] = {}

    _active_sessions: dict[str, int] = {}
    _sid_to_session_ids: dict[str, set[str]] = {}
    _heartbeat_task: dict[str, Any] = {"task": None}  # mutable closure cell

    def _sid_user(sid: str) -> str:
        return _sid_sessions.get(sid, {}).get("user_id", "anonymous")

    def _track_session_join(sid: str, session_id: str) -> None:
        _active_sessions[session_id] = _active_sessions.get(session_id, 0) + 1
        _sid_to_session_ids.setdefault(sid, set()).add(session_id)
        _ensure_heartbeat_task()

    def _track_session_leave(sid: str, session_id: str) -> None:
        if session_id in _active_sessions:
            _active_sessions[session_id] -= 1
            if _active_sessions[session_id] <= 0:
                _active_sessions.pop(session_id, None)
        sids = _sid_to_session_ids.get(sid)
        if sids:
            sids.discard(session_id)
            if not sids:
                _sid_to_session_ids.pop(sid, None)

    def _track_sid_disconnect(sid: str) -> None:
        for session_id in list(_sid_to_session_ids.get(sid, set())):
            _track_session_leave(sid, session_id)

    def _ensure_heartbeat_task() -> None:
        existing = _heartbeat_task.get("task")
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        _heartbeat_task["task"] = loop.create_task(
            _heartbeat_loop(), name="socketio:heartbeat",
        )

    async def _heartbeat_loop() -> None:
        try:
            from digitorn.core.instance import get_instance_id
        except Exception:
            return
        while True:
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                return
            if not _active_sessions:
                continue
            instance_id = get_instance_id()
            _active_sids = [
                (s, c) for s, c in _active_sessions.items() if c > 0
            ]
            _BATCH = 32
            for _batch_start in range(0, len(_active_sids), _BATCH):
                batch = _active_sids[_batch_start:_batch_start + _BATCH]
                for session_id, _count in batch:
                    current_seq = 0
                    if session_bus is not None:
                        try:
                            current_seq = session_bus.user_latest_seq("", session_id)
                        except Exception:
                            current_seq = 0
                    try:
                        # Control-plane heartbeat (carries `control=True`,
                        # no seq) so the timeline reducer skips it.
                        await sio.emit(
                            "event",
                            {
                                "type": "heartbeat",
                                "kind": "system",
                                "control": True,
                                "session_id": session_id,
                                "app_id": None,
                                "payload": {},
                                "ts": _utc_iso(),
                                "instance_id": instance_id,
                                "current_seq": current_seq,
                            },
                            room=f"session:{session_id}",
                            namespace="/events",
                        )
                    except Exception as exc:
                        logger.debug(
                            "heartbeat_emit_failed session=%s: %s",
                            session_id, exc,
                        )
                # Yield between batches so HTTP / agent turns can
                # run during the fanout.
                await asyncio.sleep(0)

    async def _authenticate(sid: str, environ: dict, auth: Any) -> str | None:
        if not auth_service:
            _sid_sessions[sid] = {"user_id": "local", "roles": ["admin"], "permissions": ["*"]}
            return "local"

        token: str | None = None
        if isinstance(auth, dict):
            token = auth.get("token")
        if not token:
            hdr = environ.get("HTTP_AUTHORIZATION", "")
            if hdr.startswith("Bearer "):
                token = hdr[7:]
        if not token:
            from urllib.parse import parse_qs
            qs = parse_qs(environ.get("QUERY_STRING", ""))
            token = qs.get("token", [None])[0]
        if not token:
            # Check for preview cookie (set by HTTP middleware for preview routes)
            cookie_header = environ.get("HTTP_COOKIE", "")
            if "digitorn_preview_token=" in cookie_header:
                import re
                match = re.search(r"digitorn_preview_token=([^;]+)", cookie_header)
                if match:
                    token = match.group(1)
        if not token:
            await logger.ainfo("socketio_auth_no_token", sid=sid)
            return None
        try:
            # Off-loop: JWT verification is GIL-bound sync crypto.
            payload = await asyncio.to_thread(
                auth_service.verify_access_token, token,
            )
        except Exception as exc:
            await logger.awarning(
                "socketio_auth_verify_failed",
                sid=sid, error_type=type(exc).__name__, error=str(exc),
            )
            return None
        try:
            user_id = payload.user_id
            roles = payload.roles or []
            permissions = payload.permissions or []
        except AttributeError as exc:
            await logger.awarning("socketio_auth_payload_invalid", sid=sid, error=str(exc))
            return None

        _sid_sessions[sid] = {
            "user_id": user_id, "roles": roles, "permissions": permissions,
            "token": token,
        }
        return user_id

    def _utc_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    @sio.on("connect", namespace="/events")
    async def on_connect(sid: str, environ: dict, auth: Any = None) -> bool | None:
        ip = environ.get("REMOTE_ADDR") or "?"
        user_id = await _authenticate(sid, environ, auth)
        if user_id is None:
            if not _ws_rate_ok(ip):
                return False
            await logger.ainfo("socketio_auth_rejected", sid=sid, ip=ip)
            return False

        # Capture the client kind from `auth['client_kind']` or the
        # `X-Digitorn-Client` header for socket-scoped branching.
        from digitorn.core.clients import parse_client_kind
        raw_ck: str | None = None
        if isinstance(auth, dict):
            ck_val = auth.get("client_kind")
            if isinstance(ck_val, str):
                raw_ck = ck_val
        if raw_ck is None:
            raw_ck = environ.get("HTTP_X_DIGITORN_CLIENT")
        client_kind = parse_client_kind(raw_ck)
        if sid in _sid_sessions:
            _sid_sessions[sid]["client_kind"] = client_kind

        await logger.ainfo(
            "socketio_connected",
            sid=sid,
            user_id=user_id,
            client_kind=client_kind.value,
        )

        # Auto-join the user room (global inbox/notifications).
        await sio.enter_room(sid, f"user:{user_id}", namespace="/events")
        
        
        latest = 0
        if session_bus is not None:
            try:
                latest = session_bus.user_latest_seq(user_id)
            except Exception:
                latest = 0

        # Daemon instance id: clients wipe their local state when this
        # changes (process restart).
        try:
            from digitorn.core.instance import get_instance_id
            _instance_id = get_instance_id()
        except Exception:
            _instance_id = ""
        # Control-plane handshake (`control=True`, no seq).
        await sio.emit(
            "event",
            {
                "type": "connected",
                "kind": "system",
                "control": True,
                "app_id": None,
                "session_id": None,
                "payload": {},
                "ts": _utc_iso(),
                "capabilities": ["full_events"],
                "latest_seq": latest,
                "user_id": user_id,
                "instance_id": _instance_id,
            },
            to=sid,
            namespace="/events",
        )

    @sio.on("disconnect", namespace="/events")
    async def on_disconnect(sid: str) -> None:
        user_id = _sid_user(sid)
        _sid_sessions.pop(sid, None)
        _track_sid_disconnect(sid)
        try:
            from digitorn.core.events import presence as _presence
            _presence.clear_sid(sid)
        except Exception as exc:
            logger.debug("presence_clear_failed sid=%s: %s", sid, exc)
        await sio.emit(
            "event",
            {
                "type": "disconnected",
                "kind": "system",
                "control": True,
                "app_id": None,
                "session_id": None,
                "payload": {},
                "ts": _utc_iso(),
                "capabilities": ["full_events"],
                "user_id": user_id,
            },
            to=sid,
            namespace="/events",
        )
        await logger.ainfo("socketio_disconnected", sid=sid, user_id=user_id)

    @sio.on("join_app", namespace="/events")
    async def on_join_app(sid: str, data: dict) -> dict:
        """Join an app room. `{app_id, since?}` - replays missed events."""
        app_id = _as_dict(data).get("app_id")
        since = int(_as_dict(data).get("since", 0) or 0)
        if not app_id:
            return {"ok": False, "error": "app_id required"}

        user_id = _sid_user(sid)
        room = f"app:{app_id}"
        await sio.enter_room(sid, room, namespace="/events")

        if since > 0 and session_bus is not None:
            try:
                missed = session_bus.user_replay(user_id, since, app_id=app_id)
                for env in missed:
                    await sio.emit("event", env, to=sid, namespace="/events")
            except Exception as exc:
                await logger.awarning("replay_failed", scope="app", error=str(exc))

        latest = session_bus.user_latest_seq(user_id) if session_bus else 0
        return {"ok": True, "room": room, "latest_seq": latest}

    @sio.on("leave_app", namespace="/events")
    async def on_leave_app(sid: str, data: dict) -> dict:
        app_id = _as_dict(data).get("app_id")
        if not app_id:
            return {"ok": False, "error": "app_id required"}
        await sio.leave_room(sid, f"app:{app_id}", namespace="/events")
        return {"ok": True}

    @sio.on("join_session", namespace="/events")
    async def on_join_session(sid: str, data: dict) -> dict:
        """Join a session room. `{app_id, session_id}`."""
        app_id = _as_dict(data).get("app_id")
        session_id = _as_dict(data).get("session_id")
        if not app_id or not session_id:
            return {"ok": False, "error": "app_id and session_id required"}

        user_id = _sid_user(sid)

        if manager is not None:
            try:
                sess = await manager.get_session(app_id, session_id, user_id=user_id)
                if sess is None:
                    await logger.awarning(
                        "socketio_join_session_denied",
                        sid=sid, user_id=user_id, session_id=session_id,
                    )
                    return {"ok": False, "error": "session not found or access denied"}
            except Exception as exc:
                await logger.awarning("socketio_join_session_error", error=str(exc))
                return {"ok": False, "error": "internal error"}

        room = f"session:{session_id}"
        await sio.enter_room(sid, room, namespace="/events")

        _track_session_join(sid, session_id)

        # Mark the user live so the inbox producer skips notifications
        # that already arrive via this socket.
        try:
            from digitorn.core.events import presence as _presence
            _presence.mark_user_in_session(sid, user_id, session_id)
        except Exception as exc:
            logger.debug("presence_mark_join_failed sid=%s: %s", sid, exc)

        # Full session isolation: leave every other room so the socket
        # only receives events tagged with this exact session_id. The
        # user room is rejoined in `on_leave_session`.
        try:
            current_rooms = sio.rooms(sid, namespace="/events")
        except Exception:
            current_rooms = []
        for r in list(current_rooms):
            if r == sid:
                continue
            if r == room:
                continue
            try:
                await sio.leave_room(sid, r, namespace="/events")
            except Exception as exc:
                logger.debug(
                    "join_session leave_room_failed sid=%s room=%s: %s",
                    sid, r, exc,
                )

        # Per-session latest_seq: the client de-dups against the
        # session-scoped counter, not the per-user one.
        latest = session_bus.user_latest_seq(user_id, session_id) if session_bus else 0

        async def _make_hydration_envelope(
            evt_type: str, payload: dict[str, Any],
        ) -> dict[str, Any]:
            _seq = session_bus._buffer.next_seq(user_id, session_id) \
                if session_bus else 0
            return {
                "event_id": f"ev-hydr-{session_id}-{_seq}",
                "type": evt_type,
                "kind": "session",
                "seq": _seq,
                "ts": _utc_iso(),
                "app_id": app_id,
                "session_id": session_id,
                "user_id": user_id,
                "op_id": f"hydration-{session_id}",
                "op_type": "system",
                "op_state": "completed",
                "op_parent_id": None,
                "correlation_id": None,
                "payload": payload,
            }

        # preview:snapshot is now served by GET /sessions/{sid}/preview.

        # Queue snapshot + turn status so the client rebuilds the pending-messages
        # UI without a separate HTTP round-trip.
        try:
            from digitorn.core.app import message_queue as _mq
            entries = await _mq.list_for_session(session_id)
            running = next(
                (e for e in entries if e.status == "running"), None,
            )
            await sio.emit(
                "event",
                await _make_hydration_envelope("queue:snapshot", {
                    "entries": [e.to_dict() for e in entries],
                    "depth": len(entries),
                    "is_active": running is not None,
                    "running_correlation_id": (
                        running.correlation_id if running else None
                    ),
                }),
                to=sid, namespace="/events",
            )

            # Resume-after-crash: if the session has queued messages and nothing
            # is running, kick the dispatcher so reconnects flush old queues.
            has_queued = any(e.status == "queued" for e in entries)
            turn_running = False
            if manager is not None and hasattr(manager, "is_turn_running"):
                try:
                    turn_running = await manager.is_turn_running(app_id, session_id)
                except Exception:
                    turn_running = False
            if has_queued and not turn_running and manager is not None:
                try:
                    import asyncio as _aio
                    _aio.create_task(
                        manager.drain_session_queue(
                            app_id, session_id, user_id,
                        ),
                    )
                    logger.info(
                        "queue_resume_after_reconnect app=%s sid=%s queued=%d",
                        app_id, session_id,
                        sum(1 for e in entries if e.status == "queued"),
                    )
                except Exception as exc:
                    logger.warning("queue_resume_failed: %s", exc)
        except Exception as exc:
            logger.warning("queue_snapshot_on_join failed: %s", exc)

        # state:snapshot is now served by GET /sessions/{sid}/state.

        try:
            from digitorn.core.events.hydration import (
                compute_active_ops,
                compute_memory_snapshot,
                compute_session_snapshot,
                compute_approvals_snapshot,
            )
        except Exception as exc:
            logger.debug("hydration helpers import failed: %s", exc)
            compute_active_ops = None  # type: ignore[assignment]
            compute_memory_snapshot = None  # type: ignore[assignment]
            compute_session_snapshot = None  # type: ignore[assignment]
            compute_approvals_snapshot = None  # type: ignore[assignment]

        # active_ops snapshot: non-terminal tool / agent / approval /
        # compact / turn operations - answers "what was running?".
        if compute_active_ops is not None:
            try:
                ops_payload = await compute_active_ops(
                    app_id=app_id, session_id=session_id, user_id=user_id,
                )
                await sio.emit(
                    "event",
                    await _make_hydration_envelope(
                        "active_ops:snapshot", ops_payload,
                    ),
                    to=sid, namespace="/events",
                )
            except Exception as exc:
                logger.warning("active_ops_snapshot_on_join failed: %s", exc)

        # (b) session:snapshot - title, created_at, message_count,
        # token totals, turn_running flag, interrupted flag. The
        # sidebar needs this on every open.
        if compute_session_snapshot is not None and manager is not None:
            try:
                sess_payload = await compute_session_snapshot(
                    manager=manager, app_id=app_id,
                    session_id=session_id, user_id=user_id,
                )
                await sio.emit(
                    "event",
                    await _make_hydration_envelope(
                        "session:snapshot", sess_payload,
                    ),
                    to=sid, namespace="/events",
                )
            except Exception as exc:
                logger.warning("session_snapshot_on_join failed: %s", exc)

        # (c) memory:snapshot - goal + todos + recent facts. Only
        # emitted if the app has a memory module.
        if compute_memory_snapshot is not None and manager is not None:
            try:
                mem_payload = await compute_memory_snapshot(
                    manager=manager, app_id=app_id,
                    session_id=session_id, user_id=user_id,
                )
                if mem_payload is not None:
                    await sio.emit(
                        "event",
                        await _make_hydration_envelope(
                            "memory:snapshot", mem_payload,
                        ),
                        to=sid, namespace="/events",
                    )
            except Exception as exc:
                logger.warning("memory_snapshot_on_join failed: %s", exc)

        # (d) approvals:snapshot - open approval modals (the original
        # `approval_request` event won't replay; the modal would
        # stay closed without this).
        if compute_approvals_snapshot is not None and manager is not None:
            try:
                ap_payload = await compute_approvals_snapshot(
                    manager=manager, app_id=app_id,
                    session_id=session_id, user_id=user_id,
                )
                if ap_payload.get("count", 0) > 0:
                    await sio.emit(
                        "event",
                        await _make_hydration_envelope(
                            "approvals:snapshot", ap_payload,
                        ),
                        to=sid, namespace="/events",
                    )
            except Exception as exc:
                logger.warning("approvals_snapshot_on_join failed: %s", exc)

        # Replay every in-flight op envelope to the joining socket so
        # the reducer can rebuild the in-progress bubbles.
        live_ops = getattr(session_bus, "_live_ops", None)
        if live_ops is not None:
            try:
                in_flight = live_ops.list_for_session(session_id)
                for env in in_flight:
                    try:
                        await sio.emit("event", env, to=sid, namespace="/events")
                    except Exception as exc:
                        logger.debug("live_ops_emit_failed: %s", exc)
                if in_flight:
                    logger.debug(
                        "live_ops_replayed sid=%s ops=%d",
                        session_id, len(in_flight),
                    )
            except Exception as exc:
                logger.warning("live_ops_snapshot_on_join failed: %s", exc)

        # `stream:catchup`: rehydrate an in-flight assistant bubble
        # when the client reconnects mid-turn.
        try:
            from digitorn.core.runtime.session_store.bridge import (
                get_default_bridge,
            )
            _bridge = get_default_bridge()
            if _bridge is not None:
                _state = await _bridge.store.open(
                    session_id, app_id=app_id, user_id=user_id,
                    create_if_missing=False, pin=False,
                )
                _partials_raw = dict(_state.streaming_partials)
                if _partials_raw:
                    # Bind each partial slot to an in-flight turn's
                    # `correlation_id` so the client maps it onto
                    # the right bubble.
                    _turn_corrids: list[str] = []
                    if live_ops is not None:
                        try:
                            for _env in live_ops.list_for_session(session_id):
                                if _env.get("op_type") == "turn":
                                    _cid = _env.get("correlation_id") or _env.get("op_id")
                                    if _cid:
                                        _turn_corrids.append(_cid)
                        except Exception:
                            _turn_corrids = []
                    # Single in-flight turn: bind every slot to it.
                    # Multi-agent: leave it None and let the final
                    # `assistant_message` event resolve the bubble.
                    _solo_cid = (
                        _turn_corrids[0] if len(_turn_corrids) == 1 else None
                    )
                    _partials_payload = {
                        str(_slot): {
                            "content": _content,
                            "correlation_id": _solo_cid,
                        }
                        for _slot, _content in _partials_raw.items()
                    }
                    await sio.emit(
                        "event",
                        await _make_hydration_envelope(
                            "stream:catchup",
                            {"partials": _partials_payload},
                        ),
                        to=sid, namespace="/events",
                    )
        except KeyError:
            pass  # session not currently open in the store
        except Exception as exc:
            logger.debug("stream_catchup_on_join failed: %s", exc)

        return {"ok": True, "room": room, "latest_seq": latest}

    @sio.on("leave_session", namespace="/events")
    async def on_leave_session(sid: str, data: dict) -> dict:
        session_id = _as_dict(data).get("session_id")
        if not session_id:
            return {"ok": False, "error": "session_id required"}
        await sio.leave_room(sid, f"session:{session_id}", namespace="/events")
        # Drop the heartbeat refcount for this session - the loop
        # stops emitting once the last sid leaves.
        _track_session_leave(sid, session_id)
        # Drop the live-in-session flag so the inbox producer starts
        # promoting events into notifications again.
        try:
            from digitorn.core.events import presence as _presence
            _presence.mark_user_left_session(
                sid, _sid_user(sid), session_id,
            )
        except Exception as exc:
            logger.debug("presence_mark_leave_failed sid=%s: %s", sid, exc)
        # Rejoin the user inbox room dropped by `on_join_session`.
        user_id = _sid_user(sid)
        if user_id:
            try:
                await sio.enter_room(
                    sid, f"user:{user_id}", namespace="/events",
                )
            except Exception as exc:
                logger.debug(
                    "leave_session rejoin_user_room_failed sid=%s: %s",
                    sid, exc,
                )
        return {"ok": True}

    @sio.on("send_message", namespace="/events")
    async def on_send_message(sid: str, data: dict) -> dict:
        """Run an agent turn. `{app_id, session_id, message, images?, workspace?}`."""
        if manager is None:
            return {"ok": False, "error": "manager unavailable"}

        app_id = _as_dict(data).get("app_id")
        session_id = _as_dict(data).get("session_id")
        message = _as_dict(data).get("message")
        if not app_id or not session_id or message is None:
            return {"ok": False, "error": "app_id, session_id and message required"}

        user_id = _sid_user(sid)
        workspace = _as_dict(data).get("workspace")
        raw_images = _as_dict(data).get("images") or []

        # One-turn system prompt fragment from the preview SDK; framed the same
        # way as POST /messages so the LLM treats it identically across transports.
        raw_addendum = _as_dict(data).get("system_addendum")
        framed_addendum = ""
        if isinstance(raw_addendum, str):
            stripped = raw_addendum.strip()[:16_000]
            if stripped:
                framed_addendum = (
                    "# Client-side context (one-turn)\n\n"
                    "The iframe surfaces this signal because it observed "
                    "state the user can't easily restate. Honour it for "
                    "THIS turn only - do not echo it back unless the user "
                    "asks.\n\n"
                    "---\n\n"
                    f"{stripped}"
                )

        # Process images (same limits as HTTP endpoint).
        image_refs: list[dict[str, Any]] = []
        if raw_images:
            try:
                from digitorn.core.image_store import get_image_store
                store = get_image_store()
                for img in raw_images[:10]:
                    mime = img.get("mime", "image/png")
                    b64 = img.get("data", "")
                    name = img.get("name", "image")
                    if b64:
                        ref = await store.store_base64(b64, mime, session_id, alt_text=name)
                        image_refs.append(ref.to_dict())
            except Exception as exc:
                await logger.awarning("socketio_image_upload_failed", error=str(exc))

        # Funnel through the per-session queue (same contract as
        # `POST /messages`) so REST + socket share the FIFO.
        import asyncio as _asyncio
        from digitorn.core.app import message_queue as _mq
        from digitorn.core.config import get_settings as _get_settings

        _qcfg = _get_settings().session.queue
        if not _qcfg.enabled:
            # Queue disabled: direct chat path. The JWT contextvar must
            # be set inside the spawned task so the gateway sees the
            # user's bearer.
            _socket_jwt = _sid_sessions.get(sid, {}).get("token")
            async def _run_direct():
                from digitorn.core.runtime.request_context import (
                    set_inbound_user_jwt, reset_inbound_user_jwt,
                )
                _tok_handle = set_inbound_user_jwt(_socket_jwt) if _socket_jwt else None
                try:
                    await manager.chat(
                        app_id, session_id, message, user_id=user_id,
                        workspace=workspace,
                        image_refs=image_refs or None,
                        template_system_prompt=framed_addendum,
                    )
                except Exception as exc:
                    await logger.awarning(
                        "socketio_chat_failed",
                        app_id=app_id, session_id=session_id,
                        error=str(exc),
                    )
                finally:
                    if _tok_handle is not None:
                        try:
                            reset_inbound_user_jwt(_tok_handle)
                        except Exception as exc:
                            logger.debug("socketio_bus best-effort block failed: %s", exc)
            _asyncio.create_task(_run_direct())
            return {"ok": True, "accepted": True}

        # Persist the workspace before enqueueing; the drain reads it
        # from `session.workspace` (the queue doesn't carry the field).
        if workspace:
            try:
                store = getattr(manager, "_session_store", None)
                if store is not None:
                    sess = store.get(app_id, session_id, user_id=user_id)
                    if sess is not None:
                        sess.workspace = str(workspace)
                        store.put(sess)
            except Exception as exc:
                await logger.awarning(
                    "socketio_workspace_persist_failed",
                    app_id=app_id, session_id=session_id, error=str(exc),
                )

        try:
            entry = await _mq.enqueue(
                app_id=app_id, session_id=session_id, user_id=user_id,
                message=message,
                image_refs=image_refs or [],
                ttl_seconds=_qcfg.ttl_seconds,
                max_depth=_qcfg.max_depth,
                template_system_prompt=framed_addendum,
            )
        except _mq.QueueFullError as exc:
            return {
                "ok": False,
                "error": (
                    f"Session queue full ({exc.depth}/{exc.max_depth}). "
                    "Cancel pending messages or wait."
                ),
            }
        except Exception as exc:
            await logger.awarning(
                "socketio_enqueue_failed",
                app_id=app_id, session_id=session_id, error=str(exc),
            )
            return {"ok": False, "error": f"enqueue failed: {exc}"}

        # Stash the inbound JWT on the queue entry; the contextvar from
        # the HTTP middleware does not cross into this Socket.IO
        # coroutine. Fall back to the JWT validated at connect time.
        try:
            from digitorn.core.runtime.request_context import get_inbound_user_jwt
            _jwt = _sid_sessions.get(sid, {}).get("token") or get_inbound_user_jwt()
            _mq.attach_jwt(entry.id, _jwt)
        except Exception as exc:
            logger.debug("socketio_bus best-effort block failed: %s", exc)

        # Echo a `user_message` event so iframe-driven sends show up
        # in the chat history (mirror of POST /messages).
        try:
            from digitorn.core.events.envelope import (
                SessionEvent, OpType, OpState,
            )
            await manager.event_bus.emit(SessionEvent.build(
                type="user_message",
                app_id=app_id,
                session_id=session_id,
                user_id=user_id,
                correlation_id=entry.correlation_id,
                op_id=entry.correlation_id,
                op_type=OpType.MESSAGE,
                op_state=OpState.PENDING,
                payload={
                    "session_id": session_id,
                    "role": "user",
                    "content": message,
                    "images": [
                        img.get("id") or img.get("ref")
                        for img in image_refs
                        if isinstance(img, dict)
                    ],
                    "correlation_id": entry.correlation_id,
                    "pending": True,
                },
            ))
        except Exception as exc:
            await logger.awarning(
                "socketio_user_message_emit_failed",
                app_id=app_id, session_id=session_id, error=str(exc),
            )

        # Kick the drain chain when nothing is in flight.
        async def _maybe_drain():
            try:
                if await _mq.has_running(session_id):
                    return  # an existing drain will pick up our entry
                await manager.drain_session_queue(
                    app_id, session_id, user_id,
                )
            except Exception as exc:
                await logger.awarning(
                    "socketio_drain_failed",
                    app_id=app_id, session_id=session_id, error=str(exc),
                )
        _asyncio.create_task(_maybe_drain())

        return {
            "ok": True,
            "accepted": True,
            "correlation_id": entry.correlation_id,
            "position": entry.position,
            "queue_depth": entry.position + 1,
        }

    @sio.on("abort_turn", namespace="/events")
    async def on_abort_turn(sid: str, data: dict) -> dict:
        """Abort the currently running agent turn for a session."""
        if manager is None:
            return {"ok": False, "error": "manager unavailable"}

        d = _as_dict(data)
        app_id = d.get("app_id")
        session_id = d.get("session_id")
        purge_queue = bool(d.get("purge_queue", False))
        if not app_id or not session_id:
            return {"ok": False, "error": "app_id and session_id required"}

        user_id = _sid_user(sid)

        was_active = manager.is_session_active(app_id, session_id)
        active_key = f"{app_id}:{session_id}"
        task = manager._session_tasks.get(active_key)
        task_cancelled = False
        if task is not None and not task.done():
            task.cancel()
            task_cancelled = True

        # Cancel pending approvals so an approve-blocked turn unwinds.
        try:
            deployed = manager.get(app_id, user_id=user_id)
        except Exception:
            deployed = None
        pending_cancelled = 0
        if deployed is not None:
            aq = getattr(deployed, "approval_queue", None)
            if aq is not None and hasattr(aq, "cancel_all"):
                try:
                    pending_cancelled = aq.cancel_all()
                except Exception:
                    logger.debug(
                        "abort_turn: cancel_all failed", exc_info=True,
                    )

        purged = 0
        if purge_queue:
            try:
                from digitorn.core.app import message_queue as _mq
                purged = await _mq.clear(session_id)
            except Exception as exc:
                logger.debug("abort_turn: clear queue failed: %s", exc)

        return {
            "ok": True,
            "was_active": was_active,
            "task_cancelled": task_cancelled,
            "pending_approvals_cancelled": pending_cancelled,
            "queue_purged": purged,
        }

    @sio.on("resolve_approval", namespace="/events")
    async def on_resolve_approval(sid: str, data: dict) -> dict:
        """Resolve a pending approval over the live WebSocket."""
        if manager is None:
            return {"ok": False, "error": "manager unavailable"}

        d = _as_dict(data)
        app_id = d.get("app_id")
        request_id = d.get("request_id")
        approved = bool(d.get("approved", False))
        message = str(d.get("message", "") or "")

        if not app_id or not request_id:
            return {"ok": False, "error": "app_id and request_id required"}

        user_id = _sid_user(sid)
        try:
            deployed = manager.get(app_id, user_id=user_id)
        except Exception:
            deployed = None
        if deployed is None:
            return {"ok": False, "error": "app not deployed"}

        aq = getattr(deployed, "approval_queue", None)
        if aq is None:
            return {"ok": False, "error": "no approval queue for this app"}

        try:
            resolved = aq.resolve(
                request_id, approved, message=message, user_id=user_id,
            )
        except Exception as exc:
            return {"ok": False, "error": f"resolve failed: {exc}"}

        if not resolved:
            return {
                "ok": False,
                "error": "request not found, already resolved, or not authorized",
            }
        return {
            "ok": True,
            "request_id": request_id,
            "approved": approved,
            "payload_received": bool(message),
        }

    @sio.on("replay", namespace="/events")
    async def on_replay(sid: str, data: dict) -> dict:
        """Replay missed events. `{since, app_id?, session_id?}`."""
        if session_bus is None:
            return {"ok": False, "error": "bus unavailable"}
        user_id = _sid_user(sid)
        since = int(_as_dict(data).get("since", 0) or 0)
        app_id = _as_dict(data).get("app_id")
        session_id = _as_dict(data).get("session_id")
        try:
            missed = session_bus.user_replay(
                user_id, since, app_id=app_id, session_id=session_id,
            )
            for env in missed:
                await sio.emit("event", env, to=sid, namespace="/events")
            latest = session_bus.user_latest_seq(user_id, session_id)
            return {"ok": True, "replayed": len(missed), "latest_seq": latest}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @sio.on("latest_seq", namespace="/events")
    async def on_latest_seq(sid: str, data: Any = None) -> dict:
        if session_bus is None:
            return {"ok": False, "error": "bus unavailable"}
        user_id = _sid_user(sid)
        # Optional session_id lets the client request the per-session
        # counter (which is what its de-dup is keyed on). Falls back to
        # user-scope when absent (inbox / approvals).
        session_id = _as_dict(data).get("session_id") if data else None
        return {
            "ok": True,
            "latest_seq": session_bus.user_latest_seq(user_id, session_id),
        }

    @sio.on("ping")
    async def on_ping(sid: str, data: Any = None) -> dict:
        return {"pong": True}

    return sio
