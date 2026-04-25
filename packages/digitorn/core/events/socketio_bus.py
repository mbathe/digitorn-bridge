"""Digitorn — Socket.IO server + event bridge.

Two things live here:

1. ``create_socketio_server()`` — builds the Socket.IO AsyncServer and
   installs all session-level handlers on the ``/events`` namespace:
   connect (auth + auto-join user room + handshake), join_app,
   leave_app, join_session, leave_session, send_message, replay,
   disconnect. Rooms match the client spec::

        user:{user_id}       (auto-joined on connect)
        app:{app_id}         (join_app)
        session:{session_id} (join_session)

2. ``SocketIOEventBus`` — a ``FanoutEventBus`` backend that forwards
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


class SocketIOEventBus(EventBus):
    """FanoutEventBus backend for module-level ``UniversalEvent``s.

    Session-level events (tokens, tool calls, results) flow through
    ``SocketIOBus`` in ``session_bus.py``. Both buses MUST emit
    envelopes with the same shape — the frontend sorts by ``seq`` and
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
        # ``session_bus`` is a ``SocketIOBus`` — we use it as the single
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
        ``event_type`` (info|error|progress|result) and ``data`` — we
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

        # Fallback — no session bus yet (early bootstrap). Still emit
        # the standard shape with seq=0 so the client parser doesn't
        # choke; it'll be replaced by the real seq on the next tick.
        return {
            "type": event_type,
            "seq": 0,
            "kind": _EVENT_KIND_MAP_FALLBACK.get(event_type, "session"),
            "app_id": event.app_id,
            "session_id": event.session_id,
            "payload": payload,
            "ts": datetime.now(timezone.utc).isoformat(),
        }

    async def publish(self, event: "UniversalEvent") -> None:
        from digitorn.core.events.models import UniversalEvent  # noqa: F401

        envelope = self._envelope(event)
        namespace = "/events"

        try:
            if event.app_id:
                await self._sio.emit(
                    "event", envelope,
                    room=f"app:{event.app_id}",
                    namespace=namespace,
                )
                if event.session_id:
                    await self._sio.emit(
                        "event", envelope,
                        room=f"session:{event.session_id}",
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

    # Per-IP rate limiter — only counts REJECTED connections.
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

        # Handshake — advertise capabilities + latest seq so reconnecting
        # clients can immediately replay what they missed.
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
        await logger.ainfo("socketio_disconnected", sid=sid, user_id=user_id)

    # ── Room joins (with replay) ───────────────────────────────────

    @sio.on("join_app", namespace="/events")
    async def on_join_app(sid: str, data: dict) -> dict:
        """Join an app room. ``{app_id, since?}`` — replays missed events."""
        app_id = (data or {}).get("app_id")
        since = int((data or {}).get("since", 0) or 0)
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
        app_id = (data or {}).get("app_id")
        if not app_id:
            return {"ok": False, "error": "app_id required"}
        await sio.leave_room(sid, f"app:{app_id}", namespace="/events")
        return {"ok": True}

    @sio.on("join_session", namespace="/events")
    async def on_join_session(sid: str, data: dict) -> dict:
        """Join a session room. ``{app_id, session_id, since?}``.

        Verifies session ownership via ``manager.get_session`` before
        letting the client into the room.
        """
        app_id = (data or {}).get("app_id")
        session_id = (data or {}).get("session_id")
        since = int((data or {}).get("since", 0) or 0)
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

        # Durable replay from the session_events DB table — single
        # source of truth, survives daemon restart and ring-buffer
        # rollover. Client passes `since: <last_seq>` (0 for full
        # history) and gets every persisted event back in order.
        if session_bus is not None:
            try:
                missed = await session_bus.async_replay(
                    user_id, since, session_id=session_id,
                )
                for env in missed:
                    await sio.emit("event", env, to=sid, namespace="/events")
                if missed:
                    logger.debug(
                        "join_session replayed %d events sid=%s since=%d",
                        len(missed), session_id, since,
                    )
            except Exception as exc:
                await logger.awarning(
                    "replay_failed", scope="session", error=str(exc),
                )

        latest = session_bus.user_latest_seq(user_id) if session_bus else 0

        # Push the current preview snapshot so the client can render the
        # session identically on reopen. Hydration order:
        #
        #   1. ``preview.activate_session(sid)`` — loads the durable
        #      ``session_workspace_snapshots`` row from DB into the
        #      in-memory store if needed, returns the resulting state.
        #   2. Legacy fallback: ``session.preview_snapshot`` stored in
        #      the session manager (pre-persistence-table builds).
        #
        # In both cases the same ``preview:snapshot`` Socket.IO event
        # carries the full state to the client.
        if manager is not None:
            try:
                # Use the user-scoped lookup — ``_deployed`` keys by
                # ``user:<uid>:<app_id>`` / ``system:<app_id>``, so a bare
                # ``get(app_id)`` only ever matches the legacy layout and
                # returns None for apps deployed through the normal API.
                deployed = manager._get_deployed(app_id, user_id=user_id)
                if deployed is not None:
                    preview_mod = deployed.modules.get("preview")
                    if preview_mod is not None:
                        snap: dict[str, Any] = {}
                        if hasattr(preview_mod, "hydrate_session") or hasattr(
                            preview_mod, "activate_session",
                        ):
                            try:
                                # Resolve workspace for fs-backed sessions.
                                _ws = ""
                                try:
                                    _sess = await manager.get_session(
                                        app_id, session_id, user_id=user_id,
                                    )
                                    _ws = getattr(_sess, "workspace", "") or "" if _sess else ""
                                except Exception:
                                    _ws = ""
                                if hasattr(preview_mod, "hydrate_session"):
                                    state_obj = await preview_mod.hydrate_session(
                                        session_id, user_id=user_id,
                                        workspace=_ws or None,
                                    )
                                else:
                                    state_obj = await preview_mod.activate_session(
                                        session_id, user_id=user_id,
                                        workspace=_ws or None,
                                    )
                                ws_mod = deployed.modules.get("workspace")
                                if ws_mod is not None and hasattr(
                                    ws_mod, "hydrate_files_from_disk",
                                ):
                                    try:
                                        loaded = await ws_mod.hydrate_files_from_disk(
                                            session_id,
                                        )
                                        if loaded:
                                            logger.info(
                                                "workspace_hydrated sid=%s files=%d",
                                                session_id, loaded,
                                            )
                                    except Exception as exc:
                                        logger.warning(
                                            "workspace_hydrate_failed sid=%s: %s",
                                            session_id, exc,
                                        )
                                snap = state_obj.snapshot()
                            except Exception as exc:
                                logger.warning(
                                    "preview_activate_session_failed sid=%s: %s",
                                    session_id, exc,
                                )

                        if not snap.get("state") and not snap.get("resources"):
                            # Legacy fallback path.
                            try:
                                persisted_session = await manager.get_session(
                                    app_id, session_id, user_id=user_id,
                                )
                                if persisted_session and getattr(
                                    persisted_session, "preview_snapshot", None,
                                ):
                                    ps = persisted_session.preview_snapshot
                                    pstate = preview_mod._store.get_or_create(session_id)
                                    pstate.state = dict(ps.get("state", {}))
                                    pstate.resources = {
                                        name: dict(items)
                                        for name, items in ps.get("resources", {}).items()
                                    }
                                    pstate._seq = ps.get("seq", 0)
                                    snap = preview_mod.snapshot_for(session_id, user_id=user_id)
                            except Exception:
                                pass

                        if snap.get("state") or snap.get("resources"):
                            ps_seq = session_bus._buffer.next_seq(user_id) if session_bus else latest
                            await sio.emit("event", {
                                "type": "preview:snapshot",
                                "seq": ps_seq,
                                "kind": "session",
                                "app_id": app_id,
                                "session_id": session_id,
                                "payload": snap,
                                "ts": _utc_iso(),
                            }, to=sid, namespace="/events")
            except Exception as exc:
                logger.warning("preview_snapshot_on_join failed: %s", exc)

        # Queue snapshot + turn status — lets the client rebuild its
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
            qs_seq = session_bus._buffer.next_seq(user_id) if session_bus else latest
            await sio.emit("event", {
                "type": "queue:snapshot",
                "seq": qs_seq,
                "kind": "session",
                "app_id": app_id,
                "session_id": session_id,
                "payload": {
                    "entries": [e.to_dict() for e in entries],
                    "depth": len(entries),
                    "is_active": running is not None,
                    "running_correlation_id": (
                        running.correlation_id if running else None
                    ),
                },
                "ts": _utc_iso(),
            }, to=sid, namespace="/events")

            # Resume-after-crash: if the session has queued messages and
            # nothing running, kick the dispatcher NOW. Covers the case
            # where the daemon restarted with a non-empty queue — the
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

        return {"ok": True, "room": room, "latest_seq": latest}

    @sio.on("leave_session", namespace="/events")
    async def on_leave_session(sid: str, data: dict) -> dict:
        session_id = (data or {}).get("session_id")
        if not session_id:
            return {"ok": False, "error": "session_id required"}
        await sio.leave_room(sid, f"session:{session_id}", namespace="/events")
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

        app_id = (data or {}).get("app_id")
        session_id = (data or {}).get("session_id")
        message = (data or {}).get("message")
        if not app_id or not session_id or message is None:
            return {"ok": False, "error": "app_id, session_id and message required"}

        user_id = _sid_user(sid)
        workspace = (data or {}).get("workspace")
        raw_images = (data or {}).get("images") or []

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

        # Fire-and-forget: errors are published to the session bus
        # and surface as `error` events on the same Socket.IO room the
        # client is already listening to.
        import asyncio as _asyncio

        async def _run():
            try:
                await manager.chat(
                    app_id, session_id, message,
                    user_id=user_id,
                    workspace=workspace,
                    image_refs=image_refs or None,
                )
            except Exception as exc:
                await logger.awarning(
                    "socketio_chat_failed",
                    app_id=app_id, session_id=session_id, error=str(exc),
                )
                if session_bus is not None:
                    try:
                        key = session_bus.session_key(app_id, session_id, user_id)
                        await session_bus.publish(key, {
                            "type": "error",
                            "data": {"error": str(exc), "fatal": True},
                        })
                    except Exception:
                        pass

        _asyncio.create_task(_run())
        return {"ok": True, "accepted": True}

    # ── Replay on demand ───────────────────────────────────────────

    @sio.on("replay", namespace="/events")
    async def on_replay(sid: str, data: dict) -> dict:
        """Replay missed events. ``{since, app_id?, session_id?}``."""
        if session_bus is None:
            return {"ok": False, "error": "bus unavailable"}
        user_id = _sid_user(sid)
        since = int((data or {}).get("since", 0) or 0)
        app_id = (data or {}).get("app_id")
        session_id = (data or {}).get("session_id")
        try:
            missed = session_bus.user_replay(
                user_id, since, app_id=app_id, session_id=session_id,
            )
            for env in missed:
                await sio.emit("event", env, to=sid, namespace="/events")
            latest = session_bus.user_latest_seq(user_id)
            return {"ok": True, "replayed": len(missed), "latest_seq": latest}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @sio.on("latest_seq", namespace="/events")
    async def on_latest_seq(sid: str, data: Any = None) -> dict:
        if session_bus is None:
            return {"ok": False, "error": "bus unavailable"}
        user_id = _sid_user(sid)
        return {"ok": True, "latest_seq": session_bus.user_latest_seq(user_id)}

    @sio.on("ping")
    async def on_ping(sid: str, data: Any = None) -> dict:
        return {"pong": True}

    return sio
