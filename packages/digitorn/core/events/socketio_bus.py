"""Digitorn - Socket.IO server + event bridge.

Two things live here:

1. ``create_socketio_server()`` - builds the Socket.IO AsyncServer and
   installs all session-level handlers on the ``/events`` namespace:
   connect (auth + auto-join user room + handshake), join_app,
   leave_app, join_session, leave_session, send_message, replay,
   disconnect. Rooms match the client spec::

        user:{user_id}       (auto-joined on connect)
        app:{app_id}         (join_app)
        session:{session_id} (join_session)

2. ``SocketIOEventBus`` - a ``FanoutEventBus`` backend that forwards
   module-level ``UniversalEvent`` instances to broadcast rooms. Kept
   for backward compat with the module system; session-level events
   go through ``SocketIOBus`` (see ``session_bus.py``), not this class.
"""

from __future__ import annotations

from typing import Any

import socketio
import structlog

from digitorn.core.events.bus import EventBus, EventHandler
from digitorn.core.events.router import EventRouter

logger = structlog.get_logger(__name__)


def _as_dict(data: Any) -> dict[str, Any]:
    """Coerce a Socket.IO event payload into a dict.

    Clients sometimes emit raw strings (``sio.emit('join_session',
    'sid-xyz')``) instead of the documented dict shape - that's a
    legitimate client mistake but historically crashed the daemon
    here with ``AttributeError: 'str' object has no attribute 'get'``,
    leaving the join silently un-acknowledged so the client never
    received any session events. Treat anything that isn't a dict
    as an empty payload - handlers will then fail their explicit
    ``if not app_id`` check and return a clean error instead.
    """
    return data if isinstance(data, dict) else {}


class SocketIOEventBus(EventBus):
    """FanoutEventBus backend for module-level ``UniversalEvent``s.

    Session-level events (tokens, tool calls, results) flow through
    ``SocketIOBus`` in ``session_bus.py``. Both buses MUST emit
    envelopes with the same shape - the frontend sorts by ``seq`` and
    assumes ``{type, seq, kind, app_id, session_id, payload, ts}`` on
    every message. Previously this bus emitted the raw ``UniversalEvent``
    fields (``event_id, topic, timestamp, event_type, data, ...``)
    which had no ``seq``, no ``kind`` and no ``type``, breaking the
    client's timeline sort for anything emitted from module actions
    (notably ``action_failed`` errors).
    """

    def __init__(
        self,
        sio: socketio.AsyncServer,
        session_bus: Any | None = None,
    ) -> None:
        self._sio = sio
        self._router = EventRouter()
        # ``session_bus`` is a ``SocketIOBus`` - we use it as the single
        # source of truth for seq generation and envelope shape.
        self._session_bus = session_bus

    def attach_session_bus(self, session_bus: Any) -> None:
        """Late-bind the session bus so envelopes get proper ``seq``/``kind``."""
        self._session_bus = session_bus

    def _envelope(self, event: "UniversalEvent") -> dict[str, Any]:
        """Normalize a ``UniversalEvent`` into the standard envelope.

        The standard shape (produced by ``SocketIOBus``) is::

            {type, seq, kind, app_id, session_id, payload, ts}

        where ``seq`` is the monotonic per-user counter and ``kind`` is
        routed by ``_EVENT_KIND_MAP``. Module events come in with
        ``event_type`` (info|error|progress|result) and ``data`` - we
        map ``event_type -> type`` and ``data -> payload``. All original
        UniversalEvent fields (topic, event_id, correlation_id, source)
        are preserved inside ``payload`` so nothing is lost.
        """
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
            except Exception:
                pass

        # Bootstrap window - the session bus hasn't been attached yet.
        # Returning an envelope with seq=0 (the previous behavior)
        # violated the monotonicity invariant: the client has no way
        # to order seq=0 against the real per-session counter once it
        # starts at N+1, and the row was never persisted to
        # ``history_log`` either, so a reconnect-replay never sees it.
        # Signal the absence with seq=-1 (clients are expected to drop
        # any envelope with a non-positive seq) and log so the operator
        # sees a publish that happened too early.
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

        # Route to ONE room only - the most specific scope wins.
        # Previously this method emitted to BOTH ``app:<id>`` AND
        # ``session:<id>`` when both were present, sending the
        # envelope twice over the wire. Combined with the strict
        # session isolation in ``on_join_session`` (which kicks the
        # client out of every room except its current session), the
        # ``app:`` copy went to nobody but still consumed CPU,
        # bandwidth, and a serialisation pass each time. Choose ONE
        # destination based on the most specific scope present:
        # session > app > broadcast.
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


# Minimal fallback map for module-level ``event_type`` values. The
# authoritative map lives in ``session_bus.py``; we duplicate only the
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
    """Create the Socket.IO server and register session-level handlers.

    Args:
        auth_service: JWT verifier. When set, every connection must
            provide a valid token via Socket.IO ``auth={'token': ...}``,
            ``?token=`` query string, or ``Authorization: Bearer ...``
            header.
        manager: AppManager used by the ``send_message`` handler.
        session_bus: ``SocketIOBus`` instance used for replay. The same
            instance is shared with AppManager so ``publish()`` and
            replay read the same buffer.
        redis_url: Optional Redis URL for multi-worker pub/sub. When set,
            Socket.IO uses ``AsyncRedisManager`` so events are shared between
            all uvicorn workers. Without this, each worker has isolated rooms.
    """
    # Multi-worker support: use Redis as the Socket.IO message queue
    # so events emitted by one worker reach clients on all workers.
    sio_kwargs: dict[str, Any] = {
        "async_mode": async_mode,
        "cors_allowed_origins": cors_allowed_origins,
        "logger": False,
        "engineio_logger": False,
        "ping_interval": 25,
        "ping_timeout": 10,
        "max_http_buffer_size": 1_000_000,
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

    # Per-sid session state, populated in the connect handler. We keep
    # it here rather than using ``sio.save_session()`` because some
    # python-socketio versions silently fail when save_session is called
    # inside the connect handler.
    _sid_sessions: dict[str, dict[str, Any]] = {}

    def _sid_user(sid: str) -> str:
        return _sid_sessions.get(sid, {}).get("user_id", "anonymous")

    async def _authenticate(sid: str, environ: dict, auth: Any) -> str | None:
        """Return user_id on success, None on failure. Token sources:
            1. ``auth={'token': ...}`` (Socket.IO standard)
            2. ``Authorization: Bearer <t>`` header
            3. ``?token=<t>`` query string (browser fallback)
            4. ``digitorn_preview_token`` cookie (preview iframe)
        """
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
            payload = auth_service.verify_access_token(token)
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
        }
        return user_id

    def _utc_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    # ── Connect / disconnect ───────────────────────────────────────

    @sio.on("connect", namespace="/events")
    async def on_connect(sid: str, environ: dict, auth: Any = None) -> bool | None:
        ip = environ.get("REMOTE_ADDR") or "?"
        user_id = await _authenticate(sid, environ, auth)
        if user_id is None:
            if not _ws_rate_ok(ip):
                return False
            await logger.ainfo("socketio_auth_rejected", sid=sid, ip=ip)
            return False

        await logger.ainfo("socketio_connected", sid=sid, user_id=user_id)

        # Auto-join the user room (global inbox/notifications).
        await sio.enter_room(sid, f"user:{user_id}", namespace="/events")
        
        
        latest = 0
        if session_bus is not None:
            try:
                latest = session_bus.user_latest_seq(user_id)
            except Exception:
                latest = 0

        await sio.emit(
            "event",
            {
                "type": "connected",
                "seq": latest,
                "kind": "system",
                "app_id": None,
                "session_id": None,
                "payload": {},
                "ts": _utc_iso(),
                "capabilities": ["full_events"],
                "latest_seq": latest,
                "user_id": user_id,
            },
            to=sid,
            namespace="/events",
        )

    @sio.on("disconnect", namespace="/events")
    async def on_disconnect(sid: str) -> None:
        user_id = _sid_user(sid)
        _sid_sessions.pop(sid, None)
        # Forget any live-session flag this sid was holding so the
        # producer resumes promoting events for that (user, session)
        # the moment the user leaves. Fail-safe on disconnect when
        # the client never sent a clean ``leave_session``.
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

    # ── Room joins (with replay) ───────────────────────────────────

    @sio.on("join_app", namespace="/events")
    async def on_join_app(sid: str, data: dict) -> dict:
        """Join an app room. ``{app_id, since?}`` - replays missed events."""
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
        """Join a session room. ``{app_id, session_id}``.

        Verifies session ownership via ``manager.get_session`` before
        letting the client into the room. The session-event replay
        path was removed - clients load history through the paginated
        HTTP ``GET /sessions/{sid}/history`` route, and any in-flight
        operation comes back via the ``LiveOpsRegistry`` snapshot
        emitted at the end of this handler.
        """
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

        # Mark the user as LIVE in this session so the inbox producer
        # knows to skip notifications for events that already arrive
        # via this socket. Mirror in ``on_leave_session`` /
        # ``on_disconnect`` keeps the registry in sync.
        try:
            from digitorn.core.events import presence as _presence
            _presence.mark_user_in_session(sid, user_id, session_id)
        except Exception as exc:
            logger.debug("presence_mark_join_failed sid=%s: %s", sid, exc)

        # Total session isolation: leave every other room this socket
        # is currently joined to (the user inbox, any app room, any
        # prior session room). While joined to a session the client
        # MUST only receive events tagged with this exact session_id.
        # Without this step the socket also stays in:
        #   - ``user:<uid>``   - inbox / approval fanout, leaks events
        #                        from other sessions of the same user
        #   - ``app:<app_id>`` - app-scope module events, leaks across
        #                        every session of the same app
        #   - ``session:<X>``  - any prior session the client navigated
        #                        away from without an explicit leave
        # The room layer is the only correct place to do this filter -
        # client-side filtering by session_id still pays the network
        # round-trip and the dedup CPU. Leaving the rooms means those
        # events are never serialised onto this socket in the first
        # place. The user room is rejoined in ``on_leave_session``.
        try:
            current_rooms = sio.rooms(sid, namespace="/events")
        except Exception:
            current_rooms = []
        for r in list(current_rooms):
            if r == sid:
                # Default room: the sid itself. Required for direct
                # ``to=sid`` emits (replay, hydrations). Never leave.
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

        # NOTE: the durable per-event replay path that lived here was
        # removed - clients load history through the paginated HTTP
        # ``GET /sessions/{sid}/history`` route now, and any in-flight
        # operation is hydrated from the ``LiveOpsRegistry`` snapshot
        # below. Streaming the full session log over the socket on
        # every join was duplicating the HTTP load and visibly
        # re-streaming finished events into the timeline.

        # Per-session counter, not per-user: the client de-dups
        # against the session-scoped seq, so a user-scope number here
        # would cause it to either skip an event (latest_seq too high)
        # or trigger a needless full replay (latest_seq=0 because the
        # bucket is wrong, see ``EventBuffer.get_latest_seq``).
        latest = session_bus.user_latest_seq(user_id, session_id) if session_bus else 0

        async def _make_hydration_envelope(
            evt_type: str, payload: dict[str, Any],
        ) -> dict[str, Any]:
            """Mint a per-client hydration envelope.

            Hydration snapshots are sent ``to=sid`` (one specific
            socket) at the moment that socket joins the session
            room. They are NOT persisted to ``history_log`` because:

              * They are client-bound, not session-bound. A second
                client joining the same room receives its OWN fresh
                snapshot - persisting one client's snapshot then
                replaying it to another client would feed it a stale
                view of the server state.
              * They are derived state (the canonical truth for
                preview is ``state.json`` on disk; for queue / turn
                / approvals it is the in-memory store). Replay
                rebuilds the same view from the source data when
                the client joins.

            The envelope still consumes a ``seq`` from the per-
            session counter so its ordering is consistent with the
            other live events the client receives over the same
            socket - just no history_log row.
            """
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

        # NOTE: ``preview:snapshot`` used to be emitted here. The disk
        # hydration (``activate_session`` + ``hydrate_files_from_disk``)
        # was the slow part of join_session - tens of files re-read off
        # disk before the room-join could complete and the agent could
        # answer the first message. Both calls now live in HTTP
        # ``GET /sessions/{sid}/preview`` which the client hits only
        # when its YAML manifest declares a workspace / preview mode.
        # join_session is back to a pure room-join + live-ops emission.

        # Queue snapshot + turn status - lets the client rebuild its
        # pending-messages UI and the "turn in progress" indicator
        # without a separate HTTP round-trip. Sent after preview
        # snapshot so the messages pane is already hydrated before
        # the queue chips render on top.
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

            # Resume-after-crash: if the session has queued messages and
            # nothing running, kick the dispatcher NOW. Covers the case
            # where the daemon restarted with a non-empty queue - the
            # user just reconnected, they shouldn't have to send a new
            # message to unblock the old ones.
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

        # NOTE: ``state:snapshot`` used to be emitted here. The same
        # envelope is now served by HTTP ``GET /sessions/{sid}/state``
        # which the client calls via ``useSessionStateStore.onSessionEntered``
        # in ``initSession``. Building the envelope holds the manager
        # lock briefly - keeping it out of join_session removes the
        # last blocking call before the room-join completes.

        # ── Hydration - everything a reconnecting client needs ──
        # The whole point of the universal event contract is that a
        # client who lost the connection can rebuild ALL of its UI in
        # one join. The events below are computed server-side and
        # emitted on the same Socket.IO channel with the full
        # envelope contract (event_id / seq / op_id / op_type /
        # op_state / correlation_id). Payload is free-form but
        # stable per snapshot type.
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

        # All four snapshot types below reuse ``_make_hydration_envelope``
        # defined at the top of this handler. That helper persists the
        # envelope before returning, so a reconnect-replay finds the
        # row in ``history_log`` instead of phantom-skipping the seq.

        # (a) active_ops:snapshot - non-terminal tool / agent / approval
        # / compact / turn operations. Primary answer to "what was
        # running when I lost the connection?".
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
        # ``approval_request`` event won't replay; the modal would
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

        # ── In-progress ops: replay each non-terminal envelope ─────
        # The ``LiveOpsRegistry`` keeps the latest envelope for every
        # currently-running op (tools, agents, approvals, thinking,
        # turns). On join we emit each one back to the joining socket
        # under its original event type - the client's reducer treats
        # them as live events and reconstructs the in-progress
        # bubbles. Each emit goes through the same ``event`` channel
        # the live socket uses, so dedup by ``event_id`` against the
        # paginated HTTP load is automatic.
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

        return {"ok": True, "room": room, "latest_seq": latest}

    @sio.on("leave_session", namespace="/events")
    async def on_leave_session(sid: str, data: dict) -> dict:
        session_id = _as_dict(data).get("session_id")
        if not session_id:
            return {"ok": False, "error": "session_id required"}
        await sio.leave_room(sid, f"session:{session_id}", namespace="/events")
        # Drop the live-in-session flag so the inbox producer starts
        # promoting events into notifications again.
        try:
            from digitorn.core.events import presence as _presence
            _presence.mark_user_left_session(
                sid, _sid_user(sid), session_id,
            )
        except Exception as exc:
            logger.debug("presence_mark_leave_failed sid=%s: %s", sid, exc)
        # Restore the user inbox room - ``on_join_session`` removed it
        # for total isolation, so leaving the session means the socket
        # has no rooms to receive on (apart from the implicit sid
        # default room). Rejoining ``user:<uid>`` brings back inbox
        # / approval / notification fanout the way ``on_connect``
        # originally set it up.
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

    # ── Send message (equivalent of POST /messages) ────────────────

    @sio.on("send_message", namespace="/events")
    async def on_send_message(sid: str, data: dict) -> dict:
        """Run an agent turn. ``{app_id, session_id, message, images?, workspace?}``.

        Returns immediately; events flow through the normal session
        room. Concurrency is bounded by the shared semaphore in
        ``api/apps.py`` to keep the daemon stable under load.
        """
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

        # Route through the per-session queue - same contract as the
        # REST ``POST /messages`` endpoint. Without this the Socket.IO
        # path bypassed the queue entirely: concurrent sends spawned
        # parallel turns on the same session (races), and queued
        # messages from REST never got drained because the chain hook
        # only fires on entries that went through the queue. Now both
        # transports funnel into the same FIFO: enqueue → drain chain
        # → ``_drain_queue_next`` fires the next entry the instant the
        # current turn's ``finally`` runs.
        import asyncio as _asyncio
        from digitorn.core.app import message_queue as _mq
        from digitorn.core.config import get_settings as _get_settings

        _qcfg = _get_settings().session.queue
        if not _qcfg.enabled:
            # Queue disabled by config - fall back to direct chat.
            async def _run_direct():
                try:
                    await manager.chat(
                        app_id, session_id, message, user_id=user_id,
                        workspace=workspace,
                        image_refs=image_refs or None,
                    )
                except Exception as exc:
                    await logger.awarning(
                        "socketio_chat_failed",
                        app_id=app_id, session_id=session_id,
                        error=str(exc),
                    )
            _asyncio.create_task(_run_direct())
            return {"ok": True, "accepted": True}

        # Persist the workspace on the session BEFORE enqueueing so
        # the drain → manager.chat() pipeline reads it from
        # ``session.workspace``. Without this, the queue carries the
        # message but loses the ``workspace`` field, and apps in
        # ``workspace_mode: required`` (digitorn-code, etc.) reject
        # the turn with "This app requires a workspace" - even though
        # the caller passed it in the send_message payload.
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

        # Kick the drain chain if nothing is currently running.
        # ``drain_session_queue`` iterates the queue until empty and
        # handles crash-safe state transitions + event emission for
        # each entry (message_started / message_done / error).
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

    # ── Replay on demand ───────────────────────────────────────────

    @sio.on("replay", namespace="/events")
    async def on_replay(sid: str, data: dict) -> dict:
        """Replay missed events. ``{since, app_id?, session_id?}``."""
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

    @sio.on("web_preview:attach_ack", namespace="/events")
    async def on_web_preview_attach_ack(sid: str, data: Any = None) -> dict:
        """Client → server confirmation that the iframe rendered.

        Resolves the pending future on the ``WebPreviewModule`` so the
        agent's ``PreviewProxy`` / ``PreviewStatic`` call returns with
        ``client_rendered=true``. Without this handler the daemon would
        hit the 8 s timeout on every attach and the agent would always
        believe the user hasn't seen the preview.
        """
        d = _as_dict(data)
        request_id = (d.get("request_id") or "").strip()
        if not request_id:
            return {"ok": False, "error": "missing request_id"}
        try:
            from digitorn.modules.web_preview.module import WebPreviewModule

            # Look up the singleton instance via the module registry on
            # app.state. Any FastAPI request would expose it; for
            # Socket.IO handlers we have to climb to the daemon's
            # registry directly.
            if manager is None:
                return {"ok": False, "error": "manager unavailable"}
            mgr = manager() if callable(manager) else manager
            if mgr is None:
                return {"ok": False, "error": "manager not ready"}
            registry = getattr(mgr, "_module_registry", None) or getattr(mgr, "_registry", None)
            mod = None
            if registry is not None:
                try:
                    mod = registry.get("web_preview")
                except Exception:
                    mod = None
            if mod is None:
                return {"ok": False, "error": "web_preview module not loaded"}
            resolved = mod.handle_ack(request_id, d)
            return {"ok": resolved}
        except Exception as exc:
            logger.warning("web_preview_attach_ack_handler_failed", error=str(exc))
            return {"ok": False, "error": str(exc)}

    return sio
