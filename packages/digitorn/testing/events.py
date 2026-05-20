"""Live Socket.IO event listener for the testing SDK.

Hardened against hangs: every blocking operation has a strict timeout,
the background thread is daemon, the event loop is explicitly stopped
on shutdown, and `stop()` never waits more than the timeout it's given.
Process exit is guaranteed even if the daemon or network misbehaves.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import threading
import time
import weakref
from typing import Any, Callable

import socketio

logger = logging.getLogger(__name__)


_all_streams: "weakref.WeakSet[LiveEventStream]" = weakref.WeakSet()


def _stop_all_on_exit() -> None:
    for s in list(_all_streams):
        try:
            s.stop(timeout=1.0)
        except Exception as exc:
            logger.debug("events best-effort block failed: %s", exc)


atexit.register(_stop_all_on_exit)


class LiveEventStream:
    """Blocking facade around a socketio.AsyncClient in a background thread."""

    def __init__(
        self,
        daemon_url: str,
        token: str,
        app_id: str,
        session_id: str,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        since_seq: int = 0,
        also_join_app: bool = False,
        also_join_user: bool = False,
    ) -> None:
        self.daemon_url = daemon_url.rstrip("/")
        self.token = token
        self.app_id = app_id
        self.session_id = session_id
        self._on_event = on_event
        self._since_seq = int(since_seq)
        self._also_join_app = also_join_app
        self._also_join_user = also_join_user
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._new_event_cv = threading.Condition(self._lock)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: socketio.AsyncClient | None = None
        self._connected = threading.Event()
        self._joined = threading.Event()
        self._stop = threading.Event()
        self._stopped = threading.Event()
        _all_streams.add(self)

    def __enter__(self) -> "LiveEventStream":
        try:
            self.start()
        except Exception:
            self.stop(timeout=1.0)
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def start(self, timeout: float = 10.0) -> None:
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name=f"LiveStream-{self.session_id[:8]}",
        )
        self._thread.start()
        try:
            if not self._connected.wait(timeout):
                raise RuntimeError(f"LiveEventStream: failed to connect within {timeout}s")
            if not self._joined.wait(timeout):
                raise RuntimeError(f"LiveEventStream: failed to join session within {timeout}s")
        except Exception:
            self.stop(timeout=1.0)
            raise

    def stop(self, timeout: float = 3.0) -> None:
        if self._stopped.is_set():
            return
        self._stop.set()
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception as exc:
                logger.debug("events best-effort block failed: %s", exc)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._stopped.set()
        with self._new_event_cv:
            self._new_event_cv.notify_all()

    def _run_loop(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            main_task = self._loop.create_task(self._async_main())
            try:
                self._loop.run_forever()
            finally:
                if not main_task.done():
                    main_task.cancel()
                    try:
                        self._loop.run_until_complete(
                            asyncio.wait_for(main_task, timeout=1.0)
                        )
                    except Exception as exc:
                        logger.debug("events best-effort block failed: %s", exc)
                try:
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                except Exception as exc:
                    logger.debug("events best-effort block failed: %s", exc)
        except Exception as exc:
            logger.warning("LiveEventStream loop crashed: %s", exc)
        finally:
            try:
                if self._loop is not None:
                    self._loop.close()
            except Exception as exc:
                logger.debug("events best-effort block failed: %s", exc)

    async def _async_main(self) -> None:
        sio = socketio.AsyncClient(
            reconnection=False, logger=False, engineio_logger=False,
        )
        self._client = sio

        @sio.event(namespace="/events")
        async def connect() -> None:
            self._connected.set()

        @sio.event(namespace="/events")
        async def disconnect() -> None:
            pass

        @sio.on("event", namespace="/events")
        async def on_event(payload: dict[str, Any]) -> None:
            env = payload if isinstance(payload, dict) else {"raw": payload}
            with self._new_event_cv:
                self._events.append(env)
                self._new_event_cv.notify_all()
            if self._on_event:
                try:
                    self._on_event(env)
                except Exception as exc:
                    logger.debug("events best-effort block failed: %s", exc)

        try:
            await asyncio.wait_for(
                sio.connect(
                    self.daemon_url,
                    auth={"token": self.token},
                    namespaces=["/events"],
                    transports=["websocket"],
                    wait_timeout=8,
                ),
                timeout=10,
            )
        except Exception as exc:
            logger.warning("socketio connect failed: %s", exc)
            return

        try:
            ack = await asyncio.wait_for(
                sio.call(
                    "join_session",
                    {"app_id": self.app_id, "session_id": self.session_id, "since": self._since_seq},
                    namespace="/events",
                    timeout=5,
                ),
                timeout=8,
            )
            if isinstance(ack, dict) and ack.get("ok"):
                self._joined.set()
            else:
                logger.warning("join_session failed: %s", ack)
        except Exception as exc:
            logger.warning("join_session error: %s", exc)

        if self._also_join_app:
            try:
                await asyncio.wait_for(
                    sio.call(
                        "join_app",
                        {"app_id": self.app_id, "since": self._since_seq},
                        namespace="/events", timeout=5,
                    ),
                    timeout=8,
                )
            except Exception as exc:
                logger.warning("join_app error: %s", exc)

        try:
            while not self._stop.is_set():
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass

        try:
            await asyncio.wait_for(sio.disconnect(), timeout=2.0)
        except Exception as exc:
            logger.debug("events best-effort block failed: %s", exc)

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def events_by_type(self, event_type: str) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e.get("type") == event_type]

    def last_seq(self) -> int:
        with self._lock:
            if not self._events:
                return 0
            return max(int(e.get("seq", 0) or 0) for e in self._events)

    def wait_for(
        self,
        event_type: str,
        timeout: float = 30.0,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self._new_event_cv:
            while True:
                if self._stopped.is_set():
                    return None
                for env in self._events:
                    if env.get("type") != event_type:
                        continue
                    if predicate and not predicate(env):
                        continue
                    return env
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._new_event_cv.wait(timeout=min(remaining, 1.0))

    def wait_for_any(
        self,
        types: list[str],
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        type_set = set(types)
        with self._new_event_cv:
            while True:
                if self._stopped.is_set():
                    return None
                for env in self._events:
                    if env.get("type") in type_set:
                        return env
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._new_event_cv.wait(timeout=min(remaining, 1.0))

    def wait_until_idle(
        self,
        quiet_seconds: float = 2.0,
        total_timeout: float = 180.0,
    ) -> bool:
        deadline = time.monotonic() + total_timeout
        while time.monotonic() < deadline:
            if self._stopped.is_set():
                return True
            with self._new_event_cv:
                count_before = len(self._events)
                notified = self._new_event_cv.wait(timeout=min(quiet_seconds, 1.0))
                count_after = len(self._events)
            if not notified and count_after == count_before:
                return True
        return False

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def send_message(
        self,
        message: str,
        workspace: str = "",
        images: list[dict[str, Any]] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        if self._loop is None or self._client is None or self._stopped.is_set():
            raise RuntimeError("LiveEventStream not running")
        payload: dict[str, Any] = {
            "app_id": self.app_id,
            "session_id": self.session_id,
            "message": message,
        }
        if workspace:
            payload["workspace"] = workspace
        if images:
            payload["images"] = images

        async def _call() -> dict[str, Any]:
            ack = await asyncio.wait_for(
                self._client.call(
                    "send_message", payload,
                    namespace="/events", timeout=min(timeout, 10),
                ),
                timeout=timeout,
            )
            return ack if isinstance(ack, dict) else {"ack": ack}

        fut = asyncio.run_coroutine_threadsafe(_call(), self._loop)
        return fut.result(timeout=timeout + 2)

    def request_replay(self, since: int, timeout: float = 10.0) -> int:
        if self._loop is None or self._client is None or self._stopped.is_set():
            raise RuntimeError("LiveEventStream not running")

        async def _call() -> dict[str, Any]:
            ack = await asyncio.wait_for(
                self._client.call(
                    "replay",
                    {"since": since, "app_id": self.app_id, "session_id": self.session_id},
                    namespace="/events", timeout=min(timeout, 10),
                ),
                timeout=timeout,
            )
            return ack if isinstance(ack, dict) else {}

        fut = asyncio.run_coroutine_threadsafe(_call(), self._loop)
        ack = fut.result(timeout=timeout + 2)
        return int(ack.get("replayed", 0) or 0)
