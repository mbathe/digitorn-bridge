"""Digitorn - FastAPI application factory and CLI entry point.

CLI commands:
    digitorn start          Start the daemon
    digitorn status         Check if the daemon is running
    digitorn version        Show version info
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, AsyncGenerator

# ── Windows asyncio event loop policy ───────────────────────────────
# MUST run BEFORE any asyncio loop is created (i.e. before uvicorn,
# socketio, or anything that touches asyncio).
#
# Why: ``asyncio.create_subprocess_exec`` raises
# ``NotImplementedError()`` (with an empty message) on the
# ``SelectorEventLoop`` - that's the loop you end up with when uvicorn
# spawns a worker via ``--reload`` (watchfiles supervisor) on Windows
# unless the policy is set explicitly. Result: every shell tool call
# fails silently with ``success=False, error=""`` because ``str(exc)``
# of a bare ``NotImplementedError`` is empty.
#
# Windows event-loop policy installer.
#
# Preference order:
#   1. ``winloop`` (libuv-based, no synchronous WSASend/WSARecvInto
#      stalls on the main loop -- the entire reason we depend on it)
#   2. ``WindowsProactorEventLoopPolicy`` as fallback (supports
#      subprocesses but suffers from the IOCP sync-syscall pathology
#      that's caused our recurring loop stalls)
#
# Called at module import AND just before ``uvicorn.run`` so every
# fresh worker (uvicorn's reload supervisor, multiprocessing-spawned
# children) gets the same policy. No-op on non-Windows.
def _install_windows_event_loop_policy() -> None:
    if sys.platform != "win32":
        return
    # Try winloop first. Falls through to Proactor if winloop is not
    # installed (e.g. user skipped ``digitorn windows-setup`` or
    # ``poetry install`` hasn't picked it up yet).
    try:
        import winloop
        asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
        return
    except ImportError:
        pass
    except Exception:
        # winloop install fail (rare). Fall through to Proactor
        # rather than leave the daemon with no policy at all.
        pass
    try:
        policy = asyncio.WindowsProactorEventLoopPolicy()  # type: ignore[attr-defined]
        asyncio.set_event_loop_policy(policy)
    except AttributeError:
        # Pre-3.7 Python or non-standard build - nothing we can do.
        pass


_install_windows_event_loop_policy()


# uvicorn 0.36+ no longer honors the global asyncio policy: its
# ``asyncio_loop_factory`` hard-codes ``asyncio.ProactorEventLoop`` on
# Windows, so the policy install above is silently ignored when uvicorn
# instantiates its own loop. The only way to inject winloop is via a
# custom loop_factory registered in ``uvicorn.config.LOOP_FACTORIES``
# and passed as ``loop=<name>`` to ``uvicorn.run``.
#
# This module-level constant is the loop name to hand uvicorn: either
# ``"winloop"`` (Windows + winloop installed) or ``"asyncio"`` (every
# other case). Computed once at import.
def _winloop_loop_factory(use_subprocess: bool = False):  # type: ignore[no-untyped-def]
    """uvicorn loop_factory shim. Mirrors ``uvloop_loop_factory``
    since winloop is uvloop API-compatible. ``use_subprocess`` is
    accepted for signature parity but ignored (winloop handles
    subprocess child watchers via libuv automatically).
    """
    import winloop  # noqa: F401 (validates availability at call time)
    return winloop.new_event_loop


def _select_uvicorn_loop_name() -> str:
    if sys.platform != "win32":
        return "auto"
    try:
        import winloop  # noqa: F401
        from uvicorn.config import LOOP_FACTORIES
        LOOP_FACTORIES["winloop"] = (
            "digitorn.core.server:_winloop_loop_factory"
        )
        return "winloop"
    except Exception:
        return "asyncio"


_UVICORN_LOOP = _select_uvicorn_loop_name()


def _patch_uvicorn_send_for_winloop() -> None:
    """Swallow winloop's ``Cannot call write() when UVStream is closing``
    RuntimeError at the SOURCE -- inside uvicorn's
    ``RequestResponseCycle.send``, before the error propagates up into
    starlette's middleware chain where anyio wraps it in an
    ExceptionGroup and starlette logs it as an unhandled_exception.

    The error fires when a client closes the connection mid-response
    (very common with frontend polling tabs being closed, browser
    refreshes, dropped network). asyncio's default ProactorEventLoop
    silently dropped these writes; winloop / uvloop are stricter and
    raise. Functionally there's no impact -- the request already
    completed -- but the log spam is severe under polling load.

    Windows-only (winloop's specific phrasing). Safe no-op if uvicorn
    changes the API or if winloop isn't active.
    """
    if sys.platform != "win32":
        return
    try:
        from uvicorn.protocols.http.httptools_impl import RequestResponseCycle
    except Exception:
        return
    _orig_send = RequestResponseCycle.send

    async def _safe_send(self, message):  # type: ignore[no-untyped-def]
        try:
            return await _orig_send(self, message)
        except RuntimeError as exc:
            # Narrow match: only the winloop close-race error. Anything
            # else (Response content longer/shorter than Content-Length,
            # Unexpected ASGI message, etc.) MUST still propagate so
            # we don't mask real bugs in the response generation.
            msg = str(exc)
            if (
                "UVStream is closing" in msg
                or "handle is closed" in msg
            ):
                return  # client gone -- pretend the write succeeded
            raise

    RequestResponseCycle.send = _safe_send  # type: ignore[method-assign]


_patch_uvicorn_send_for_winloop()


def _patch_websockets_close_for_winloop() -> None:
    """Swallow ``Cannot call write() when UVStream is closing`` on the
    WebSocket close-handshake path.

    Twin sister of ``_patch_uvicorn_send_for_winloop`` but for a
    different call chain. When a client sends a CLOSE frame and then
    immediately tears down its TCP connection, the server's
    ``websockets.legacy.protocol.WebSocketCommonProtocol`` tries to
    echo the close (RFC 6455 close-handshake). winloop refuses the
    write because the stream is already in CLOSING state -- raises
    ``RuntimeError`` which bubbles up through ``transfer_data`` and
    floods the logs with ``data transfer failed`` tracebacks.

    The error is cosmetic: the client is already gone, the close
    handshake serves no purpose at that point. asyncio's default
    ProactorEventLoop dropped these writes silently; winloop is
    stricter. We restore parity by catching and ignoring the specific
    error message.

    Patched at module import so every WebSocket connection inherits
    the safer ``write_close_frame``. Narrow string match -- any other
    RuntimeError keeps propagating.
    """
    if sys.platform != "win32":
        return
    try:
        from websockets.legacy.protocol import WebSocketCommonProtocol
    except Exception:
        return
    _orig_write_close = WebSocketCommonProtocol.write_close_frame

    async def _safe_write_close_frame(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            return await _orig_write_close(self, *args, **kwargs)
        except RuntimeError as exc:
            msg = str(exc)
            if (
                "UVStream is closing" in msg
                or "handle is closed" in msg
            ):
                return  # client gone -- skip the close-handshake echo
            raise

    WebSocketCommonProtocol.write_close_frame = _safe_write_close_frame  # type: ignore[method-assign]


_patch_websockets_close_for_winloop()


def _relaunch_in_project_venv_if_needed() -> None:
    """Re-exec ``digitorn start`` through the project venv when the
    current interpreter is not the venv.

    Users typically have multiple Python installs (global, project
    venv, system). PATH resolution is unpredictable -- typing
    ``digitorn start`` can land on the global Python's launcher,
    which often carries STALE deps (old uvicorn without
    ``websockets-sansio``, missing winloop, etc.) and the daemon
    fails to boot or runs degraded.

    By re-execing through ``<venv>/Scripts/digitorn.exe`` (or the
    Unix equivalent) we guarantee the daemon always runs against
    the fresh deps pinned in the project venv.

    No-op when:
      - we are already in the project venv
      - no venv exists next to the source tree (PyPI-style install)
      - ``DIGITORN_VENV_RELAUNCHED=1`` is set (the relaunched
        process must not relaunch again -- prevents loops)
    """
    if os.environ.get("DIGITORN_VENV_RELAUNCHED") == "1":
        return

    try:
        # ``server.py`` lives at packages/digitorn/core/server.py;
        # parents[3] = repo root containing pyproject.toml + venvs.
        project_root = Path(__file__).resolve().parents[3]
    except Exception:
        return
    if not (project_root / "pyproject.toml").is_file():
        return  # not running from a source checkout

    for venv_name in (".venv312", ".venv311", ".venv", "venv", ".venv313"):
        venv_dir = project_root / venv_name
        if not venv_dir.is_dir():
            continue
        try:
            already_in_venv = (
                Path(sys.prefix).resolve() == venv_dir.resolve()
            )
        except Exception:
            already_in_venv = False
        if already_in_venv:
            return  # nothing to do

        if sys.platform == "win32":
            launcher = venv_dir / "Scripts" / "digitorn.exe"
        else:
            launcher = venv_dir / "bin" / "digitorn"
        if not launcher.is_file():
            continue  # venv exists but doesn't carry our launcher

        new_env = dict(os.environ)
        new_env["DIGITORN_VENV_RELAUNCHED"] = "1"

        cmd = [str(launcher)] + sys.argv[1:]
        print(
            f"[digitorn] relaunching through project venv: {launcher}",
            flush=True,
        )
        try:
            # ``os.execvpe`` replaces the current process image.
            # PID changes on Windows (per CPython docs); the shell
            # follows the new process. stdout / stderr stay attached.
            os.execvpe(str(launcher), cmd, new_env)
        except Exception as exc:
            print(
                f"[digitorn] venv relaunch failed ({exc}); "
                "continuing with current interpreter",
                flush=True,
            )
        return  # only attempt the first matching venv


# ``_OverlappedFuture.set_exception`` / ``set_result`` raise
# ``InvalidStateError`` when the I/O callback completes AFTER the
# awaiting task has already cancelled the future. The exception
# escapes ``_poll`` → ``_run_once`` → kills the daemon mid-turn.
# We routinely hit this during long LLM calls where an HTTP client
# resets the socket while the agent loop is still running
# (cpython#87419 and friends).
#
# Treat double-completion as a no-op: the awaiter has already moved
# on with the cancellation result, the second write would be
# discarded anyway. Idempotent + no-op on non-Windows.
def _patch_overlapped_future_idempotent_completion() -> None:
    if sys.platform != "win32":
        return
    try:
        from asyncio import windows_events as _we  # type: ignore[attr-defined]
    except ImportError:
        return
    target = getattr(_we, "_OverlappedFuture", None)
    if target is None or getattr(target, "_digitorn_idempotent", False):
        return

    orig_set_exception = target.set_exception
    orig_set_result = target.set_result

    def _safe_set_exception(self, exception):  # type: ignore[no-untyped-def]
        if self.done():
            return
        orig_set_exception(self, exception)

    def _safe_set_result(self, result):  # type: ignore[no-untyped-def]
        if self.done():
            return
        orig_set_result(self, result)

    target.set_exception = _safe_set_exception  # type: ignore[method-assign]
    target.set_result = _safe_set_result  # type: ignore[method-assign]
    target._digitorn_idempotent = True  # type: ignore[attr-defined]


_patch_overlapped_future_idempotent_completion()

import socketio
import typer
import uvicorn
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

__version__ = "1.0.0"

console = Console()


def create_app(settings: "Settings | None" = None) -> "socketio.ASGIApp":
    """Create and configure the FastAPI + Socket.IO application.

    Returns a Socket.IO ASGIApp that wraps the FastAPI app.
    Socket.IO handles /socket.io/* paths, everything else falls through to FastAPI.
    """
    import socketio

    from digitorn.core.config import Settings, get_settings
    from digitorn.core.events.bus import FanoutEventBus, LogEventBus
    from digitorn.core.events.event_buffer import EventBuffer
    from digitorn.core.events.session_bus import SocketIOBus
    from digitorn.core.events.socketio_bus import SocketIOEventBus, create_socketio_server

    if settings is None:
        settings = get_settings()

    cors_origins = settings.server.cors_origins
    # Remote auth verifier shared with Socket.IO. Populated below once
    # the central JWKS URL is known. None when auth is disabled.
    _auth_holder: dict[str, Any] = {}

    class _LazyAuth:
        """Proxy so Socket.IO can use the remote-auth verifier built
        later in setup. Evaluates falsy when auth is disabled, so the
        Socket.IO connect handler skips token validation.
        """
        def __bool__(self) -> bool:
            return _auth_holder.get("service") is not None

        def verify_access_token(self, token: str):
            svc = _auth_holder.get("service")
            if svc is None:
                raise ValueError("Auth not initialized")
            return svc.verify_access_token(token)

    # Manager holder - initialized during lifespan, used by Socket.IO for session validation
    _manager_holder: dict[str, Any] = {}

    class _LazyManager:
        """Proxy so Socket.IO can use AppManager methods after lifespan init.

        Forwards all attribute access to the real manager once it's ready.
        Before that, ``get_session`` returns None and ``chat`` raises.
        """
        def __getattr__(self, name: str):
            mgr = _manager_holder.get("manager")
            if mgr is None:
                raise AttributeError(f"Manager not initialized (accessing .{name})")
            return getattr(mgr, name)

        async def get_session(self, app_id: str, session_id: str, user_id: str | None = None):
            mgr = _manager_holder.get("manager")
            if mgr is None:
                return None
            return await mgr.get_session(app_id, session_id, user_id=user_id)

    # Session event bus - shared by AppManager (publish) and Socket.IO
    # handlers (replay). Created BEFORE sio so the handlers can reach
    # the replay buffer; the sio reference is injected after construction.
    session_event_bus = SocketIOBus(sio=None, buffer=EventBuffer())

    # If kv_backend is Redis, wire it as Socket.IO's pub/sub adapter
    # so events emitted by one worker reach clients on ALL workers.
    # Always include the daemon's own origin so preview iframes (served from
    # the same host) can connect to Socket.IO without CORS errors.
    _daemon_origin = f"http://{settings.server.host}:{settings.server.port}"
    _effective_cors: list[str] | str = list(cors_origins)
    if _daemon_origin not in _effective_cors:
        _effective_cors.append(_daemon_origin)
    # Always include the official production origins so Socket.IO from
    # ``digitorn.ai`` / ``api.digitorn.ai`` / preview subdomains connects
    # without operator-side CORS tweaks. Mirrors the HTTP allow_origin_regex.
    for _prod in (
        "https://digitorn.ai",
        "https://api.digitorn.ai",
        "https://hub.digitorn.ai",
    ):
        if _prod not in _effective_cors:
            _effective_cors.append(_prod)


    _bind_host = str(getattr(settings.server, "host", "") or "")
    _is_loopback = _bind_host in ("127.0.0.1", "localhost", "::1", "")
    if _is_loopback:
        _effective_cors = "*"

    _kv_url = getattr(settings.server, "kv_backend", None)
    sio = create_socketio_server(
        cors_allowed_origins=_effective_cors,
        auth_service=_LazyAuth(),
        manager=_LazyManager(),
        session_bus=session_event_bus,
        redis_url=_kv_url if _kv_url and str(_kv_url).startswith("redis") else None,
    )
    session_event_bus._sio = sio  # wire the emitter now that sio exists

    # Wire the web_preview module's handshake emitter + URL template.
    # The module is ``isolation=shared`` (one instance per daemon)
    # so single class-level references are enough. The URL template
    # decides how proxy attachments are addressed by user browsers:
    # local dev = loopback (default), cloud = wildcard subdomain
    # (operator-configured via ``settings.web_preview``).
    try:
        from digitorn.modules.web_preview.module import WebPreviewModule
        WebPreviewModule.attach_sio(sio)
        WebPreviewModule.configure(
            public_url_template=getattr(
                settings.web_preview, "public_url_template",
                "http://{host}:{port}",
            ),
            enabled=getattr(settings.web_preview, "enabled", True),
        )
    except Exception as _exc:
        logger.warning("web_preview_sio_wire_failed: %s", _exc)

    # In-progress ops registry. Same KV configured for sessions/rate-
    # limit (Redis when ``kv_backend`` is set, DiskCache otherwise).
    # Wired into the session event bus AFTER construction so every
    # ``emit()`` records non-terminal envelopes for ``join_session`` to
    # read. Failure to construct is non-fatal: the bus simply emits
    # without tracking, and the legacy fallback (no in-progress ops on
    # join) takes over.
    try:
        from digitorn.core.kv import create_backend as _create_kv
        from digitorn.core.events.live_ops import LiveOpsRegistry as _LiveOps
        _live_ops_kv = _create_kv(_kv_url) if _kv_url else _create_kv(None)
        _live_ops_registry = _LiveOps(_live_ops_kv)
        session_event_bus.set_live_ops(_live_ops_registry)
    except Exception as _exc:
        logger.warning("live_ops_registry_init_failed: %s", _exc)
        _live_ops_registry = None

    socketio_bus = SocketIOEventBus(sio, session_bus=session_event_bus)
    log_bus = LogEventBus()
    event_bus = FanoutEventBus([log_bus, socketio_bus])

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Daemon resource protocol: stamp the process-wide instance
        # id on app.state at the very top of lifespan so every route
        # / middleware / Socket.IO handler can read it. Generated by
        # the singleton getter (cached on first call) — restart =
        # new process = fresh id, which is the signal clients use
        # to detect daemon restarts and wipe stale local state.
        try:
            from digitorn.core.instance import get_instance_id
            app.state.instance_id = get_instance_id()
        except Exception:
            app.state.instance_id = ""

        # Sanity check on Windows: the running event loop MUST be a
        # ProactorEventLoop. The module-level
        # ``_install_windows_event_loop_policy`` should have set the
        # policy at import time, but uvicorn / multiprocessing edge
        # cases can still land us on the SelectorEventLoop where
        # ``asyncio.create_subprocess_exec`` raises
        # ``NotImplementedError()`` (with empty message) - bricking
        # every shell tool call invisibly. Fail loud instead.
        if sys.platform == "win32":
            try:
                _running_loop = asyncio.get_running_loop()
                _loop_cls = type(_running_loop).__name__
                if "Proactor" not in _loop_cls:
                    logger.error(
                        "WRONG_EVENT_LOOP loop=%s - subprocess will fail "
                        "(asyncio.create_subprocess_exec raises "
                        "NotImplementedError on SelectorEventLoop). "
                        "Restart the daemon without --reload, or check "
                        "_install_windows_event_loop_policy in server.py.",
                        _loop_cls,
                    )
                else:
                    logger.info("event_loop_ok loop=%s", _loop_cls)
            except Exception:
                logger.debug("event_loop_check_failed", exc_info=True)

        from digitorn.core.database import close_db, init_db
        from digitorn.core.loader import load_modules
        from digitorn.core.state_store import JsonStateStore
        from digitorn.core.watcher.service import SourceWatcherService
        from digitorn.modules.lifecycle import ModuleLifecycleManager
        from digitorn.modules.registry import ModuleRegistry
        from digitorn.modules.service_bus import ServiceBus

        # Phase timings - surfaces which phase eats the boot budget so
        # a regression in module init / DB schema check / module
        # on_start is obvious in the logs without having to attach a
        # profiler. Each phase logs its own duration; the final
        # "lifespan_ready" line gives the total HTTP-server-blocked
        # time. Background tasks (node, mcp_pool, inbox, credentials,
        # bootstrap_builtins, reload_from_db) run in parallel and don't
        # count toward this total.
        _t0 = time.monotonic()
        def _phase(name: str, since: float) -> None:
            logger.info("boot_phase %s elapsed_ms=%d", name, int((time.monotonic() - since) * 1000))

        # Long-phase progress ticker. ``await _slow_phase("init_db", coro)``
        # logs an INFO every 10s while ``coro`` is running, so a 90s
        # cold start no longer looks like a hang to whoever's watching
        # the daemon stdout. The ticker is cancelled the moment the
        # phase resolves so successful boots stay clean (one final
        # ``boot_phase`` line, no spam). Surfaces the same payload
        # ``/healthz`` consumers see via ``app.state.boot_phase``.
        async def _slow_phase(name: str, coro: Any) -> Any:
            start = time.monotonic()
            app.state.boot_phase = name
            cancelled = False
            async def _ticker() -> None:
                nonlocal cancelled
                while not cancelled:
                    try:
                        await asyncio.sleep(10)
                    except asyncio.CancelledError:
                        return
                    if cancelled:
                        return
                    elapsed = time.monotonic() - start
                    logger.info(
                        "boot_phase %s WAITING elapsed_s=%.0f - "
                        "still running, hold on",
                        name, elapsed,
                    )
            ticker = asyncio.create_task(_ticker(), name=f"boot-tick:{name}")
            try:
                result = await coro
                return result
            finally:
                cancelled = True
                ticker.cancel()
                try:
                    await ticker
                except (asyncio.CancelledError, Exception):
                    pass
                _phase(name, start)

        engine = await _slow_phase("init_db", init_db(settings))
        app.state.engine = engine

        # Boot the agent-run tracker BEFORE any code that might call
        # ``agent_turn``. The worker drains a queue in the background;
        # the runtime hot path enqueues events without awaiting any I/O.
        # Backend selection comes from settings.runtime.tracking.
        _t = time.monotonic()
        try:
            from digitorn.core.runtime import run_tracker as _run_tracker
            from digitorn.core.runtime.run_tracker.backends import select_backend
            tracking_cfg = settings.runtime.tracking
            backend = select_backend(tracking_cfg.backend, tracking_cfg.config)
            await _run_tracker.install_and_start(backend)
            app.state.run_tracker_backend = type(backend).__name__
            _phase("run_tracker_start", _t)
        except Exception as exc:
            logger.warning("run_tracker_start_failed exc=%s", exc, exc_info=True)
            app.state.run_tracker_backend = None

        # Warm the JWKS cache before the HTTP server accepts traffic so
        # the first request doesn't pay the discovery + key-fetch cost.
        # Tolerates a network failure - the middleware retries lazily.
        if getattr(settings.server, "auth_enabled", True):
            try:
                from digitorn_auth.fastapi import install_remote_auth
                await _slow_phase(
                    "remote_auth_warm",
                    install_remote_auth(
                        app,
                        issuer=app.state.auth_service_url,
                        accept_issuers=app.state.auth_accept_issuers,
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("remote_auth_warm_failed exc=%s", exc)

        _t = time.monotonic()
        try:
            from digitorn.core.runtime.session_store.bootstrap import (
                init_session_store,
            )

            # Wire the SessionStore's internal-allocation hook into
            # the wire-side EventBuffer so allocations made by
            # ``store.compact_session`` / ``spawn_child`` (which don't
            # transit through ``bus.emit``) are reflected in the wire
            # allocator's high-water mark. Without this hook, an
            # internal allocation at seq=K leaves the EventBuffer
            # still at K-1, and the next wire emit would also return
            # K -> the client would see two events with the same seq
            # (one live, one on history replay).
            _bus_buffer = session_event_bus._buffer
            def _sync_buffer_after_internal_alloc(sid: str, seq: int) -> None:
                try:
                    _bus_buffer.bump_to(session_id=sid, value=seq)
                except Exception as exc:
                    logger.debug(
                        "session_store->buffer seq sync failed sid=%s seq=%s: %s",
                        sid, seq, exc,
                    )

            app.state.session_store = await init_session_store(
                on_internal_seq_alloc=_sync_buffer_after_internal_alloc,
            )
            _phase("session_store_start", _t)
        except Exception as exc:
            logger.warning(
                "session_store_start_failed exc=%s (falling back to "
                "Postgres-only history path)", exc, exc_info=True,
            )
            app.state.session_store = None

        _t = time.monotonic()
        registry = ModuleRegistry()
        results = load_modules(
            registry,
            enabled=settings.modules.enabled or None,
            disabled=settings.modules.disabled or None,
            load_all=settings.modules.load_all,
        )
        app.state.registry = registry
        app.state.module_load_results = results
        _phase("load_modules", _t)

        watcher_service = SourceWatcherService(event_bus)
        await watcher_service.start()
        app.state.watcher_service = watcher_service

        service_bus = ServiceBus()
        app.state.service_bus = service_bus

        state_dir = Path.home() / ".digitorn" / "state"
        state_store = JsonStateStore(state_dir)
        app.state.state_store = state_store

        lifecycle = ModuleLifecycleManager(
            registry=registry,
            event_bus=event_bus,
            service_bus=service_bus,
            state_store=state_store,
        )
        app.state.lifecycle = lifecycle

        from digitorn.core.sidecar_pool import DaemonSidecarPool

        _t = time.monotonic()
        sidecar_pool = DaemonSidecarPool()
        await sidecar_pool.start()
        app.state.sidecar_pool = sidecar_pool
        _phase("sidecar_pool", _t)

        from digitorn.core.runtime.node_runtime import get_node_runtime
        node_runtime = get_node_runtime()
        node_runtime.set_auto_install(settings.server.node_auto_install)
        # Backgrounded: ``ensure_installed`` may download Node (30+ s on
        # cold machines) which doesn't need to block HTTP startup. Routes
        # that consume Node (preview servers, web build) check
        # ``node_runtime.info`` lazily and surface a clear error if Node
        # isn't ready yet.
        async def _ensure_node_bg() -> None:
            try:
                await node_runtime.ensure_installed()
                logger.info(
                    "node_runtime_ready version=%s source=%s",
                    node_runtime.version,
                    node_runtime.info.source if node_runtime.info else "?",
                )
            except Exception as exc:
                logger.warning(
                    "node_runtime_unavailable: %s - Node-dependent features "
                    "will be disabled until Node is installed", exc,
                )
        asyncio.create_task(_ensure_node_bg())
        app.state.node_runtime = node_runtime

        # Warm tiktoken in a background thread so the first agent turn
        # doesn't eat 100-300 ms on the cl100k_base encoding build (file
        # read of the BPE table + Python imports). Without this, the
        # first token-count call from ``streaming.py`` / ``session_metrics``
        # blocks the main loop just long enough to trip the slow-callback
        # WARNING (threshold 100 ms). Background-only; if it fails the
        # lazy path in ``_get_tiktoken_enc`` still works (it just pays
        # the cost on the first real call).
        async def _warm_tiktoken_bg() -> None:
            try:
                from digitorn.core.runtime.session_metrics import (
                    _get_tiktoken_enc,
                )
                await asyncio.to_thread(_get_tiktoken_enc)
                logger.info("tiktoken_warmed_up encoding=cl100k_base")
            except Exception as exc:
                logger.debug(
                    "tiktoken_warmup_failed: %s -- lazy path will retry "
                    "on first call", exc,
                )
        asyncio.create_task(_warm_tiktoken_bg())

        from digitorn.modules.context import ModuleContext

        for module_id in registry.list_available():
            module = registry.get(module_id)
            module._watcher_service = watcher_service
            ctx = ModuleContext(
                module_id=module_id,
                event_bus=event_bus,
                service_bus=service_bus,
                settings=settings,
                sidecar_pool=sidecar_pool,
            )
            module.set_context(ctx)

        # When out-of-process workers host a module (``workers.workers[].modules``),
        # the worker subprocess runs the real ``on_start`` -- the daemon-side
        # singleton must NOT. Without this guard, every workered module fires
        # its full lifecycle on the daemon (e.g. fastembed model load for
        # rag/vector, MCP server scan, ast.parse warm-up for index), wasting
        # 500ms-2s per module of boot time + RAM, and risking double-start
        # for state-bearing modules (open files, opened ports, background
        # tasks). We monkey-patch a no-op on_start/on_stop ONLY on the
        # daemon-side singleton instances of workered modules; the worker's
        # own instance is untouched and runs the real lifecycle.
        try:
            _hosted = settings.workers.hosted_module_names()
            # Exclusion list: modules whose daemon-side state MUST stay
            # populated even though they're hosted by a worker.
            #
            # ``llm_provider`` is the canonical case: bootstrap's
            # ``_resolve_provider`` reads from ``llm_module._providers``
            # to find the right provider for an agent's brain. The
            # provider object then gets wrapped with an
            # ``LLMProviderProxy`` in ``_build_single_agent_context``
            # (bootstrap.py:564). If we skipped on_config_update for
            # llm_provider, ``_providers`` would stay empty and every
            # agent would fail with ``provider 'X' not found
            # (available: [])``. The daemon pays the SSL-handshake
            # cost ONCE at deploy time per provider; runtime
            # chat_stream calls still go to the worker via the proxy.
            _DAEMON_LIFECYCLE_REQUIRED = {"llm_provider"}
            _skip_targets = [m for m in _hosted if m not in _DAEMON_LIFECYCLE_REQUIRED]
            if _skip_targets:
                async def _skip_lifecycle_for_workered() -> None:
                    """No-op coroutine used to replace on_start/on_stop on
                    daemon-side singletons of workered modules.
                    """
                    return None

                _patched: list[str] = []
                for _mid in _skip_targets:
                    _inst = registry._instances.get(_mid)
                    if _inst is None:
                        continue
                    _inst.on_start = _skip_lifecycle_for_workered  # type: ignore[assignment]
                    _inst.on_stop = _skip_lifecycle_for_workered  # type: ignore[assignment]
                    _inst._skip_on_start = True  # type: ignore[attr-defined]
                    _inst._skip_on_stop = True  # type: ignore[attr-defined]
                    _patched.append(_mid)
                if _patched:
                    logger.info(
                        "workered_modules_lifecycle_skipped daemon-side "
                        "modules=%s reason=hosted_by_worker", _patched,
                    )
        except Exception as exc:
            # Defensive: never let a misconfiguration here break boot.
            logger.warning(
                "workered_modules_lifecycle_patch_failed: %s -- "
                "daemon will run full on_start for every module", exc,
            )

        await _slow_phase("lifecycle.start_all", lifecycle.start_all())

        from digitorn.core.mcp_pool import DaemonMCPPool
        from digitorn.core.database import _session_factory as db_session_factory

        mcp_pool = DaemonMCPPool()
        # Backgrounded: spawning MCP child processes + DB query for the
        # config can add 1-3 s to boot. Routes that need MCP servers
        # await on ``mcp_pool.is_ready()`` so they get a clear "not
        # ready yet" instead of a half-initialised pool.
        async def _start_mcp_bg() -> None:
            try:
                await mcp_pool.start(db_session_factory)
            except Exception as exc:
                logger.warning("mcp_pool_start_failed: %s", exc, exc_info=True)
        asyncio.create_task(_start_mcp_bg())
        app.state.mcp_pool = mcp_pool

        # Hub-served catalog cache. Per the App Store model, the Hub is
        # the source of truth for which MCP servers are officially
        # supported; the daemon caches the list in-process and refreshes
        # every 5 min. Disabled when ``settings.hub.url`` is empty
        # (offline / dev) — in that case the baked-in ``catalog.CATALOG``
        # dict serves as the fallback.
        hub_url = getattr(settings.hub, "url", "") or ""
        if hub_url:
            from digitorn.modules.mcp import hub_catalog_client
            hub_catalog = hub_catalog_client.init(hub_url)

            async def _start_hub_catalog_bg() -> None:
                try:
                    ok = await hub_catalog.refresh()
                    logger.info(
                        "hub_catalog_prewarm ok=%s size=%d url=%s",
                        ok, hub_catalog.size, hub_url,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("hub_catalog_prewarm_failed: %s", exc)

            asyncio.create_task(_start_hub_catalog_bg())
            app.state.hub_catalog_task = asyncio.create_task(
                hub_catalog.run_refresh_loop()
            )
            app.state.hub_catalog = hub_catalog
        else:
            logger.info("hub_catalog_disabled (settings.hub.url empty)")
            app.state.hub_catalog = None
            app.state.hub_catalog_task = None

        from digitorn.core.app.runtime import AppRuntimeStore

        runtime_store = AppRuntimeStore(registry)
        app.state.runtime_store = runtime_store

        from digitorn.core.app.manager_v2 import AppManager

        app_manager = AppManager(
            registry,
            service_bus,
            runtime_store,
            stop_on_error=settings.app.stop_on_error,
            event_bus=session_event_bus,
        )
        app_manager._settings = settings
        app_manager._daemon_mcp_pool = mcp_pool
        mcp_pool.set_on_event(app_manager._on_mcp_event)
        app.state.app_manager = app_manager
        # Quota enforcement is owned by the digitorn LLM gateway.

        # ── Credential store - foundation of the universal secrets system ──
        # The master key is auto-generated on the first boot at
        # ~/.digitorn/master.key. If DIGITORN_MASTER_KEY is set, that
        # takes precedence (Docker / k8s deployment model).
        try:
            from digitorn.core.credentials import CredentialStore
            from digitorn.core.credentials.bootstrap import (
                import_env_vars_into_store,
            )
            from digitorn.core.credentials.master_key import (
                build_provider_from_config,
            )
            from digitorn.core.credentials.cipher import VersionedCipher
            from digitorn.core.credentials.audit import (
                install_global_scrubber,
            )
            from digitorn.core.credentials.catalog import (
                load_builtin_catalog,
            )
            # Register all built-in handlers (api_key, oauth2, etc.).
            # Module-level register() calls fire on import.
            from digitorn.core.credentials import handlers as _handlers  # noqa: F401
            from digitorn.core.database import get_session_factory

            # Install the log scrubber early so any subsequent log line
            # that accidentally embeds a secret gets redacted before
            # leaving the process. Idempotent.
            install_global_scrubber()

            # Load the built-in provider catalog (TOML files in
            # core/credentials/catalog/builtins/). Idempotent - re-import
            # at hot reload doesn't duplicate.
            try:
                n_loaded = load_builtin_catalog()
                logger.info("credential_catalog_loaded count=%d", n_loaded)
            except Exception as exc:
                logger.warning("credential_catalog_load_failed: %s", exc)

            # Choose the master key provider from config / env. Falls
            # back to FileKeyProvider with ~/.digitorn/master.key when
            # nothing is set, matching the legacy default behaviour.
            kms_cfg = (
                getattr(settings, "kms", None) or {}
                if hasattr(settings, "kms")
                else {}
            )
            kms_provider = build_provider_from_config(kms_cfg)
            try:
                # Verify the provider is reachable BEFORE serving
                # traffic. KMS misconfiguration surfaces here, not at
                # the first credential operation.
                ok = await kms_provider.healthcheck()
                if not ok:
                    logger.warning(
                        "kms_healthcheck_failed backend=%s key_id=%s - "
                        "credential operations will fail until fixed",
                        kms_provider.backend.value,
                        kms_provider.key_id,
                    )
            except Exception as exc:
                logger.warning("kms_healthcheck_error: %s", exc)
            cipher = VersionedCipher(kms_provider)
            app.state.kms_provider = kms_provider
            credential_store = CredentialStore(get_session_factory(), cipher)
            app.state.credential_store = credential_store
            app_manager._credential_store = credential_store

            # SQL-backed credential audit log (hash chained). Read via
            # /api/admin/credentials/audit; written by the deploy- and
            # session-time credential injectors.
            try:
                from digitorn.core.credentials.audit import SqlAuditLog
                cred_audit = SqlAuditLog(get_session_factory())
                app.state.credential_audit = cred_audit
                app_manager._credential_audit = cred_audit
            except Exception as audit_exc:
                logger.warning(
                    "credential_audit_init_failed: %s - audit endpoints disabled",
                    audit_exc,
                )
                app.state.credential_audit = None

            # OAuth provider registry + background refresh loop.
            # Registry reads `~/.digitorn/oauth_providers.toml`; refresh
            # loop wakes every 5 min and renews tokens whose expiry
            # falls within the buffer window.
            try:
                from digitorn.core.credentials.oauth_providers import (
                    get_default_registry as _oauth_registry,
                )
                from digitorn.core.credentials.oauth_refresh_loop import (
                    OAuthRefreshLoop,
                )
                oauth_registry = _oauth_registry()
                refresh_loop = OAuthRefreshLoop(
                    store=credential_store,
                    registry=oauth_registry,
                    interval_seconds=300,
                )
                refresh_loop.start()
                app.state.oauth_registry = oauth_registry
                app.state.oauth_refresh_loop = refresh_loop
                logger.info(
                    "oauth_subsystem_ready providers_configured=%s",
                    oauth_registry.list_configured(),
                )
            except Exception as oauth_exc:
                logger.warning(
                    "oauth_subsystem_init_failed: %s - oauth flows disabled",
                    oauth_exc,
                )
                app.state.oauth_registry = None
                app.state.oauth_refresh_loop = None

            # Inbox store + producer - persistent cross-device notification
            # inbox. The producer subscribes to the bus's per-user fan-out
            # and materializes events into rows via the store. The API
            # routes in core/api/user.py read from the same store.
            # Quota and usage tracking are owned by the digitorn LLM
            # gateway (`packages/gateway/`). The daemon does not
            # maintain any usage_store / quota_store anymore.

            try:
                from digitorn.core.inbox import (
                    InboxStore,
                    InboxStoreFileAdapter,
                    InboxProducer,
                    NotificationDispatcher,
                )
                # Backend selection:
                #   - DB url present -> Postgres-backed store (cloud)
                #   - DB url empty   -> file-backed adapter (self-hosted
                #     local runtime where ~/.digitorn/digitorn.db doesn't
                #     even exist). Notifications persist as JSON files
                #     under ~/.digitorn/inbox/<user_id>/<item_id>.json.
                _db_url = (settings.database.url or "").strip()
                if _db_url:
                    inbox_store = InboxStore(get_session_factory(), sio=sio)
                else:
                    from pathlib import Path as _Path
                    _inbox_root = _Path.home() / ".digitorn" / "inbox"
                    inbox_store = InboxStoreFileAdapter(
                        root=_inbox_root, sio=sio,
                    )
                    logger.info(
                        "inbox_backend=file root=%s "
                        "(database.url empty, self-hosted mode)",
                        _inbox_root,
                    )
                app.state.inbox_store = inbox_store

                # Notification dispatcher - gracefully degrades if
                # firebase-admin / SMTP creds aren't configured.
                # At minimum, the "desktop" channel (Socket.IO stream)
                # is always covered because the event is already on
                # the user fan-out path.
                dispatcher = NotificationDispatcher(store=inbox_store)
                app.state.notification_dispatcher = dispatcher

                inbox_producer = InboxProducer(
                    store=inbox_store,
                    event_bus=app_manager.event_bus,
                    dispatcher=dispatcher,
                )
                # Inbox producer subscribes to the event bus and writes
                # rows on the fly - its ``start()`` does a DB schema
                # check that adds a few hundred ms. Background it: any
                # event published before the producer is ready is still
                # delivered to clients via the live SocketIO stream;
                # only the persistent inbox row would be missed during
                # the short window, which is acceptable.
                async def _start_inbox_bg(producer):
                    try:
                        await producer.start()
                        logger.info("inbox_subsystem_started")
                    except Exception as exc:
                        logger.warning("inbox start failed: %s", exc, exc_info=True)
                asyncio.create_task(_start_inbox_bg(inbox_producer))
                app.state.inbox_producer = inbox_producer

                # Daily prune of archived items older than 30 days.
                # Without this loop the ``inbox_items`` table grows
                # unbounded (the model has no TTL, the API never
                # hard-deletes - DELETE archives, archived items just
                # accumulate). 30 days matches what the policy
                # surfaces in the UI; tunable via the call below.
                async def _prune_loop(store):
                    # Initial sleep so the first prune fires ~24h after
                    # boot rather than at startup, when the process is
                    # already under DB load from create_all + migrations.
                    sleep_secs = 24 * 3600
                    try:
                        await asyncio.sleep(sleep_secs)
                    except asyncio.CancelledError:
                        return
                    while True:
                        try:
                            n = await store.prune_old(older_than_days=30)
                            if n > 0:
                                logger.info(
                                    "inbox_prune_swept rows=%d "
                                    "older_than_days=30", n,
                                )
                        except asyncio.CancelledError:
                            return
                        except Exception as exc:
                            logger.warning(
                                "inbox_prune_failed: %s", exc,
                            )
                        try:
                            await asyncio.sleep(sleep_secs)
                        except asyncio.CancelledError:
                            return
                app.state.inbox_prune_task = asyncio.create_task(
                    _prune_loop(inbox_store),
                )
            except Exception as exc:
                logger.warning("inbox init failed: %s", exc, exc_info=True)
                app.state.inbox_store = None
                app.state.inbox_producer = None
                app.state.notification_dispatcher = None
                app.state.inbox_prune_task = None

            # Both env-import and the legacy-scope migration touch the
            # DB but neither blocks any first-request feature - the
            # secrets they import are read lazily at agent-turn time.
            # Bundle them in one background task so the bootstrap log
            # still surfaces the import summary, just slightly later.
            async def _credential_bg(store):
                try:
                    import_summary = await import_env_vars_into_store(store)
                    logger.info(
                        "credential bootstrap: imported=%d skipped=%d not_in_env=%d",
                        len(import_summary["imported"]),
                        len(import_summary["skipped_already_present"]),
                        import_summary["not_in_env"],
                    )
                except Exception as exc:
                    logger.warning("credential bootstrap failed: %s", exc)
                try:
                    migrated = await store.migrate_legacy_scopes()
                    if any(migrated.values()):
                        logger.info(
                            "credential migration: user_rows=%d system_rows=%d grants_created=%d",
                            migrated["user_rows"],
                            migrated["system_rows"],
                            migrated["grants_created"],
                        )
                except Exception as exc:
                    logger.warning("credential migration skipped: %s", exc)
            asyncio.create_task(_credential_bg(credential_store))

            # Load the OAuth provider registry - writes a template at
            # ~/.digitorn/oauth_providers.toml on first boot. Having
            # zero configured providers is fine (OAuth features will
            # just 503 with a clear error until the admin configures
            # something).
            from digitorn.core.credentials.oauth_providers import (
                get_default_registry as get_oauth_registry,
            )
            oauth_registry = get_oauth_registry()
            app.state.oauth_provider_registry = oauth_registry
            logger.info(
                "OAuth providers loaded: configured=%s total=%d",
                oauth_registry.list_configured(),
                len(oauth_registry.list_all()),
            )
        except Exception as exc:
            logger.error(
                "credential store init FAILED: %s - the daemon will "
                "fall back to the legacy secret_store and credentials "
                "routes will be unavailable",
                exc,
                exc_info=True,
            )
            app.state.credential_store = None
        _manager_holder["manager"] = app_manager

        # ── App loading - MUST NEVER crash the daemon ─────────────────────
        # Catch BaseException (not just Exception) to survive SystemExit
        # from MCP subprocesses or other edge cases.
        app.state.bootstrap_result = None
        if settings.app.yaml_path:
            try:
                deployed = await app_manager.deploy(Path(settings.app.yaml_path))
                app.state.bootstrap_result = deployed.bootstrap_result
                app.state.compiled_app = deployed.compiled
            except BaseException as exc:
                logger.error("initial_app_deploy_failed: %s", exc, exc_info=True)

        # Wire the PackageRegistry onto the manager BEFORE reloading
        # deployed apps from the DB. The reload path
        # (``_reload_one_app``) needs the package's on-disk
        # install_dir to resolve relative paths (web/dist, workspace
        # sync_path, etc). Without the registry wired in time,
        # ``_resolve_install_dir`` returns None and the install dir
        # falls back to ``Path.cwd()`` which is usually wrong.
        try:
            from digitorn.core.packages import (
                PackageRegistry,
                classify_existing_apps,
            )
            from digitorn.core.packages.bootstrap import bootstrap_builtins
            from digitorn.core.database import get_session_factory

            await classify_existing_apps(get_session_factory())
            package_registry = PackageRegistry(get_session_factory())
            app.state.package_registry = package_registry
            app_manager._package_registry = package_registry
        except Exception as exc:
            logger.error(
                "package_registry_wire_failed: %s", exc, exc_info=True,
            )
            package_registry = None

        # ── reload_from_db - fire-and-forget in the background ──
        # We DON'T await this in the lifespan so Uvicorn starts
        # accepting HTTP connections immediately. Apps come online
        # concurrently (bounded by reload_from_db's internal semaphore)
        # and typically finish within a few seconds after boot. A
        # ``warming_up`` flag on app.state exposes the progress to
        # clients that want to wait before making their first request.
        app.state.warming_up = True
        app.state.warmup_started_at = time.monotonic()

        async def _run_reload_from_db_bg() -> None:
            try:
                # Workers (heavy / cpu / network ...) are spawned later
                # in this same lifespan (line ~1489 in this file).
                # ``reload_from_db`` triggers ``auto_index`` which calls
                # ``index.register_source`` on the ``cpu`` worker -- if
                # the worker isn't bound to its port yet, every per-app
                # auto_index in this batch fails with
                # ``All connection attempts failed`` and workspaces are
                # never indexed at boot. Wait for the worker lifecycle
                # to be installed AND for every worker to respond on
                # ``/health`` before kicking off the reload. Fail-open:
                # if the wait times out, proceed anyway -- reload's
                # internal error handling logs the per-app failures.
                wl = None
                for _i in range(120):  # up to 60s waiting for lifecycle install
                    wl = getattr(app.state, "worker_lifecycle", None)
                    if wl is not None:
                        break
                    await asyncio.sleep(0.5)
                if wl is not None:
                    try:
                        await wl.wait_ready(timeout=20.0)
                    except Exception as exc:
                        logger.warning(
                            "reload_from_db_bg: wait_ready raised %s -- "
                            "proceeding anyway",
                            exc,
                        )

                reloaded = await app_manager.reload_from_db()
                logger.info(
                    "reload_from_db completed in background: %d app(s) loaded",
                    len(reloaded),
                )
            except asyncio.CancelledError:
                # Expected during shutdown / Ctrl+C - let the cancel
                # propagate without polluting the logs with a bogus
                # "app_reload_from_db_failed" error. The old
                # ``except BaseException`` captured this case and made
                # every clean shutdown look like a crash.
                raise
            except Exception as exc:
                logger.error("app_reload_from_db_failed: %s", exc, exc_info=True)
            finally:
                app.state.warming_up = False
                app.state.warmup_duration = (
                    time.monotonic() - app.state.warmup_started_at
                )

        asyncio.create_task(_run_reload_from_db_bg())
        logger.info(
            "reload_from_db dispatched to background - HTTP server is up "
            "while apps finish loading (watch /health for warming_up flag)"
        )

        # ── AppPackages bootstrap - install / upgrade built-in packages ──
        # Runs after reload so reload sees the existing registry rows,
        # and bootstrap can upgrade any stale builtins.
        #
        # Set DIGITORN_SKIP_BUILTINS=1 to skip entirely - useful for
        # isolated test daemons that want a bare registry.
        if os.environ.get("DIGITORN_SKIP_BUILTINS"):
            logger.info("bootstrap_builtins skipped (DIGITORN_SKIP_BUILTINS set)")
        elif package_registry is not None:
            async def _bootstrap_deploy(yaml_path, package_id):
                return await app_manager.deploy(yaml_path, force=True)

            async def _run_bootstrap_in_background():
                try:
                    # Same race fix as ``_run_reload_from_db_bg``: wait
                    # until ``app.state.worker_lifecycle`` is installed
                    # and every worker is bound on its port. Without
                    # this, ``bootstrap_builtins`` -> ``_bootstrap_deploy``
                    # -> ``deploy()`` triggers ``auto_index`` which
                    # talks to the cpu worker, and a not-yet-ready
                    # worker yields ``All connection attempts failed``
                    # for every builtin app in the boot batch.
                    wl = None
                    for _i in range(120):  # up to 60s
                        wl = getattr(app.state, "worker_lifecycle", None)
                        if wl is not None:
                            break
                        await asyncio.sleep(0.5)
                    if wl is not None:
                        try:
                            await wl.wait_ready(timeout=20.0)
                        except Exception as exc:
                            logger.warning(
                                "bootstrap_builtins_bg: wait_ready raised %s "
                                "-- proceeding anyway",
                                exc,
                            )

                    await asyncio.wait_for(
                        bootstrap_builtins(
                            registry=package_registry,
                            on_deploy=_bootstrap_deploy,
                        ),
                        timeout=600.0,
                    )
                    logger.info("AppPackages bootstrap completed in background")
                except asyncio.TimeoutError:
                    logger.error(
                        "AppPackages bootstrap exceeded 10-min budget - "
                        "broken builtins are marked accordingly, daemon unaffected",
                    )
                except Exception as exc:
                    logger.error(
                        "AppPackages bootstrap (background) failed: %s",
                        exc, exc_info=True,
                    )

            asyncio.create_task(_run_bootstrap_in_background())
            logger.info(
                "AppPackages bootstrap dispatched to background task - "
                "daemon HTTP is starting NOW without waiting"
            )
        else:
            logger.warning(
                "package_registry not wired - skipping AppPackages bootstrap"
            )

        # ── Legacy built-in apps - kept for backwards compat ──
        # The old core/builtin_apps/ dir is now empty after v2
        # migration; this call is a no-op but stays for one release
        # as a safety net in case the new bootstrap had a partial
        # failure.
        await _deploy_builtin_apps(app_manager)

        # ── Eager preload of the Whisper model (transcribe module) ──
        # When transcribe.preload=true (default) we load the local model
        # in a background task so the first /api/transcribe request is
        # instant. Fire-and-forget: the daemon never blocks on it.
        if settings.transcribe.preload and settings.transcribe.enabled \
                and settings.transcribe.provider == "local":
            async def _preload_transcribe_model():
                try:
                    from digitorn.core.api.transcribe import preload_model
                    await preload_model()
                except Exception as exc:
                    logger.warning("transcribe_preload_task_failed: %s", exc)
            asyncio.create_task(_preload_transcribe_model())
            logger.info(
                "transcribe_preload_dispatched model=%s device=%s compute=%s",
                settings.transcribe.model,
                settings.transcribe.device,
                settings.transcribe.compute_type,
            )

        app.state.session_store = getattr(app_manager, "_session_store", None)

        # Message queue boot recovery:
        #   1. Reset rows stuck in ``running`` → ``queued`` (crash
        #      mid-turn rehydration) - they'd otherwise block the
        #      queue forever with a stale lease.
        #   2. Cancel every still-``queued`` row. Auto-dispatching
        #      them would fire stale prompts (cost tokens, confuse
        #      the user who may not remember sending them). Safer
        #      default: clean slate - the user can resend whatever
        #      they actually want. Cancelled rows are kept for
        #      audit (status + error_code).
        try:
            from digitorn.core.app.message_queue import (
                rehydrate_on_boot,
                purge_queued_on_boot,
                setup_from_settings as _queue_setup,
            )
            # Pick the backend (sql / redis / memory) before any
            # boot-recovery call, so rehydrate / purge run against the
            # right store. Always succeeds - falls back to SQL if Redis
            # is unreachable, mirroring the Socket.IO Redis fallback.
            await _queue_setup()
            _recovered = await rehydrate_on_boot()
            if _recovered:
                logger.info(
                    "message_queue rehydrated %d stuck rows on boot",
                    _recovered,
                )
            _purged = await purge_queued_on_boot(reason="daemon_restart")
            if _purged:
                logger.info(
                    "message_queue purged %d queued rows on boot",
                    _purged,
                )
        except Exception as exc:
            logger.warning("message_queue boot recovery failed: %s", exc)

        app.state._active_requests = 0
        app.state._active_agent_turns = 0
        app.state._shutting_down = False

        # Active event-loop watchdog - detects stalls > 2s and dumps
        # stacks at the moment of the stall. Also sets
        # ``loop.slow_callback_duration = 0.1`` so any callback > 100ms
        # is logged as a WARNING.
        from digitorn.core.runtime.loop_watchdog import install as _install_wd
        app.state.loop_watchdog = _install_wd()

        # Periodic session eviction - prevent memory leak from expired sessions
        async def _session_evictor() -> None:
            while not app.state._shutting_down:
                await asyncio.sleep(60)
                try:
                    store = getattr(app_manager, "_session_store", None)
                    if store and hasattr(store, "evict_expired"):
                        store.evict_expired()
                except Exception:
                    pass
        _eviction_task = asyncio.create_task(_session_evictor())

        async def _queue_reaper() -> None:
            from digitorn.core.app.message_queue import reap_expired_leases
            while not app.state._shutting_down:
                await asyncio.sleep(15)
                try:
                    await reap_expired_leases()
                except Exception as exc:
                    logger.debug("queue_reaper_iteration_failed: %s", exc)
        _reaper_task = asyncio.create_task(_queue_reaper())

        async def _activation_sweeper() -> None:
            """Mark zombie activations (``status=running`` past their
            natural timeout) as failed. Prior to BUG-054 these rows
            piled up forever whenever agent_turn crashed - they broke
            the dashboard success_rate metric and the cron dashboard's
            running counter. Runs once a minute; the ``older_than``
            window exceeds any realistic turn timeout so a truly
            running activation is never stolen.

            BUG-107: also emit a ``background_app_rot`` event when any
            app's recent activations are 100% failures - silent rot on
            background-only apps (no user sitting in a session to
            notice) used to go unreported until someone opened the
            dashboard and saw ``success_rate: 0.0``.

            ALL Postgres traffic for this sweeper is routed through
            ``persist_worker`` so the daemon's main asyncio loop never
            touches asyncpg / SQLAlchemy directly. asyncpg has known
            cross-loop / TLS-renegotiation pathologies on Windows that
            can stall the main loop for 5-25 seconds when triggered
            from the main pool. The worker has a dedicated psycopg3
            loop on its own thread immune to those issues.
            """
            # When the ``cron`` module is hosted by a Digitorn worker
            # (``workers.workers[].modules: [cron]``), the worker
            # runs the activation sweep under a file-based leader
            # lock and the daemon must stay out of it -- otherwise
            # both processes hammer the same rows once a minute. The
            # registry returns ``None`` when no worker hosts cron
            # (the default), so the early-return is a no-op in legacy
            # mode and behaviour matches today byte-for-byte.
            #
            # NOTE: this skips BOTH the stuck-running sweep and the
            # in-memory rot detector. The rot detector requires
            # ``app.state.app_manager`` which is daemon-side; if you
            # need it in production, run cron in-process (default) or
            # add a separate rot watcher to the cron module that
            # queries the apps table instead of the live dict.
            try:
                from digitorn.workers.registry import (
                    get_default_registry,
                )
                if get_default_registry().route("cron") is not None:
                    logger.info(
                        "activation_sweeper_skipped reason=cron_workered",
                    )
                    return
            except Exception:
                # If the workers package isn't importable for any
                # reason, fall through to the legacy in-process path.
                pass

            from digitorn.core.runtime.persist_worker import get_default_worker

            # Per-iteration coroutine. Captures ``app`` via closure but
            # only READS from app.state (snapshot ``list(_deployed)``)
            # so cross-thread access is safe under the GIL.
            async def _sweep_iteration() -> None:
                from digitorn.core.app.activation_store import ActivationStore
                from digitorn.core.database import get_session_factory
                try:
                    store = ActivationStore(get_session_factory())
                    n = await store.sweep_stuck_running(older_than_seconds=600)
                    if n:
                        logger.info(
                            "activation_sweeper marked_failed=%d", n,
                        )
                except Exception as exc:
                    logger.debug(
                        "activation_sweeper_iteration_failed: %s", exc,
                    )

                # BUG-107: silent-rot detector. Iterate over a snapshot
                # of the currently deployed apps to find background-only
                # apps whose recent activation window is 100% failure.
                try:
                    mgr = getattr(app.state, "app_manager", None)
                    if mgr is None:
                        return
                    store_rot = ActivationStore(get_session_factory())
                    for dep in list(getattr(mgr, "_deployed", {}).values()):
                        app_id = getattr(dep, "app_id", None)
                        if not app_id:
                            continue
                        mode = getattr(
                            getattr(dep, "compiled", None), "execution", None,
                        )
                        if getattr(mode, "mode", "") != "background":
                            continue
                        try:
                            stats = await store_rot.stats(app_id)
                        except Exception:
                            continue
                        total = int(stats.get("total") or 0)
                        if total < 5:
                            continue  # too small to judge
                        completed = int(stats.get("completed") or 0)
                        failed = int(stats.get("failed") or 0)
                        if completed == 0 and failed >= 5:
                            logger.warning(
                                "background_app_rot app=%s total=%d "
                                "failed=%d success_rate=0.0 - every "
                                "recent activation failed, investigate "
                                "credentials / provider / YAML",
                                app_id, total, failed,
                            )
                except Exception as exc:
                    logger.debug("rot_detector_iteration_failed: %s", exc)

            worker = get_default_worker()
            while not app.state._shutting_down:
                await asyncio.sleep(60)
                # Fire-and-forget: queue the sweep on the persist_worker
                # so the main loop never touches asyncpg directly. The
                # work executes on the worker's dedicated psycopg3 loop
                # with the session_factory override already pushed by
                # ``_run_one`` (persist_worker.py:572).
                worker.submit(_sweep_iteration)
        _activation_sweeper_task = asyncio.create_task(_activation_sweeper())

        # ── Worker Pool - dedicated thread pools for agent turns + I/O ──
        from digitorn.core.worker_pool import init_worker_pool, shutdown_worker_pool
        worker_pool = init_worker_pool(
            max_turn_workers=settings.server.turn_workers,
            max_io_workers=settings.server.io_workers,
        )
        worker_pool.set_as_default_executor()
        app.state.worker_pool = worker_pool

        try:
            notif_interval = float(
                getattr(settings.app, "notification_poll_interval", 1.0)
            )
        except Exception:
            notif_interval = 1.0
        await app_manager.start_notification_poller(interval=notif_interval)

        try:
            await app_manager.start_stale_turn_watchdog()
        except Exception as exc:
            logger.warning("stale_turn_watchdog_start_failed: %s", exc)

        # Workers subsystem: spawn the configured worker subprocesses
        # (shell / llm_provider / cron / ...) AFTER the daemon's own
        # init is done. When ``settings.workers.enabled`` is False
        # (default) this is a fast no-op; the daemon continues to host
        # every module in-process exactly as today. When enabled, the
        # lifecycle handle is stored on ``app.state.worker_lifecycle``
        # for introspection (``/health`` and admin endpoints).
        try:
            from digitorn.workers.lifecycle import (
                start_workers_if_enabled,
            )
            await start_workers_if_enabled(app, settings)
        except Exception as exc:
            logger.warning(
                "workers_lifecycle_start_failed err=%s -- daemon "
                "continues without workers (routes fall back to "
                "in-process via empty registry)", exc,
            )

        # Admin dashboard foundation: telemetry hub + config registry.
        # Both are background-only / read-mostly subsystems with zero
        # impact on the hot path. ``install_telemetry`` spawns its own
        # collector + lag-probe tasks; ``install_config_registry``
        # introspects Settings synchronously (microseconds). A failure
        # here MUST NOT break boot - we degrade to "no admin dashboard"
        # rather than refusing to serve traffic.
        try:
            from digitorn.core.runtime.telemetry_hub import install_telemetry
            from digitorn.core.config_registry import install_config_registry
            await install_telemetry(app)
            install_config_registry()
        except Exception as exc:
            logger.warning(
                "admin_dashboard_foundation_init_failed err=%s -- "
                "daemon continues; admin diagnostics endpoints will "
                "return empty data", exc,
            )

        # Mark the lifespan complete so external supervisors / health
        # probes can stop counting boot time. ``warming_up`` is still
        # set by the background reload task at line ~740 - that one
        # tracks per-app deploy reloading, not lifespan.
        app.state.boot_phase = "ready"
        app.state.boot_completed_at = time.monotonic()
        logger.info(
            "lifespan_ready total_ms=%d (HTTP serving NOW; node/mcp/inbox/"
            "credentials/builtins/reload still warming in background)",
            int((time.monotonic() - _t0) * 1000),
        )
        yield

        # Workers subsystem: terminate worker subprocesses BEFORE the
        # rest of shutdown so in-flight tool calls drain cleanly (the
        # worker_lifecycle uses 10s grace + SIGKILL). When workers are
        # disabled this is a fast no-op.
        try:
            from digitorn.workers.lifecycle import (
                stop_workers_if_running,
            )
            await stop_workers_if_running()
        except Exception as exc:
            logger.warning(
                "workers_lifecycle_stop_failed err=%s -- continuing "
                "shutdown anyway", exc,
            )

        # Telemetry + config registry shutdown. Cancels the collector
        # tasks cleanly so we don't leave a dangling background coroutine
        # on a partially-torn-down loop.
        try:
            from digitorn.core.runtime.telemetry_hub import shutdown_telemetry
            from digitorn.core.config_registry import shutdown_config_registry
            await shutdown_telemetry()
            shutdown_config_registry()
        except Exception as exc:
            logger.warning("admin_dashboard_foundation_stop_failed: %s", exc)

        try:
            await app_manager.stop_notification_poller()
        except Exception as exc:
            logger.warning("notification_poller_stop_failed: %s", exc)

        try:
            await app_manager.stop_stale_turn_watchdog()
        except Exception as exc:
            logger.warning("stale_turn_watchdog_stop_failed: %s", exc)

        _eviction_task.cancel()
        _reaper_task.cancel()
        _activation_sweeper_task.cancel()
        try:
            wd = getattr(app.state, "loop_watchdog", None)
            if wd is not None:
                wd.stop()
        except Exception:
            pass

        app.state._shutting_down = True
        logger.info("shutdown_started draining_active_requests=%d", app.state._active_requests)

        drain_timeout = 30.0
        drain_start = time.monotonic()
        while app.state._active_requests > 0 and (time.monotonic() - drain_start) < drain_timeout:
            await asyncio.sleep(0.5)
        if app.state._active_requests > 0:
            logger.warning(
                "shutdown_drain_timeout remaining_requests=%d", app.state._active_requests,
            )

        # Drain in-progress agent turns
        turn_drain_start = time.monotonic()
        while app.state._active_agent_turns > 0 and (time.monotonic() - turn_drain_start) < drain_timeout:
            await asyncio.sleep(0.5)
        if app.state._active_agent_turns > 0:
            logger.warning("shutdown_agent_drain_timeout remaining_turns=%d", app.state._active_agent_turns)

        inbox_producer = getattr(app.state, "inbox_producer", None)
        if inbox_producer is not None:
            try:
                await inbox_producer.stop()
            except Exception as exc:
                logger.warning("inbox_producer stop failed: %s", exc)

        inbox_prune_task = getattr(app.state, "inbox_prune_task", None)
        if inbox_prune_task is not None and not inbox_prune_task.done():
            inbox_prune_task.cancel()
            try:
                await inbox_prune_task
            except (asyncio.CancelledError, Exception):
                pass

        for app_id in list(app_manager._deployed.keys()):
            await app_manager.undeploy(app_id)

        hub_catalog_task = getattr(app.state, "hub_catalog_task", None)
        if hub_catalog_task is not None and not hub_catalog_task.done():
            hub_catalog = getattr(app.state, "hub_catalog", None)
            if hub_catalog is not None:
                hub_catalog.stop()
            hub_catalog_task.cancel()
            try:
                await hub_catalog_task
            except (asyncio.CancelledError, Exception):
                pass

        await mcp_pool.stop()
        await sidecar_pool.stop()
        await lifecycle.stop_all()
        await watcher_service.shutdown()

        # Stop every workspace-cache FS watcher cleanly so the daemon
        # doesn't leak inotify / FSEvents handles between restarts.
        try:
            ws_cache = getattr(app.state, "workspace_cache", None)
            if ws_cache is not None and hasattr(ws_cache, "shutdown"):
                ws_cache.shutdown()
        except Exception as exc:
            logger.warning("workspace_cache_shutdown_failed: %s", exc)

        # Phase 4c: history_writer removed; SessionStore drain happens
        # below via shutdown_session_store().

        try:
            from digitorn.core.runtime.session_store.bootstrap import (
                shutdown_session_store,
            )
            await shutdown_session_store(
                getattr(app.state, "session_store", None),
            )
        except Exception as exc:
            logger.warning("session_store_stop_failed: %s", exc)

        # Stop the index module's CPU-extract ProcessPoolExecutor so
        # daemon shutdown doesn't leak subprocess workers.
        try:
            from digitorn.modules.index.module import _shutdown_extract_pool
            _shutdown_extract_pool()
        except Exception as exc:
            logger.warning("index_extract_pool_stop_failed: %s", exc)

        # Stop the OAuth refresh background loop cleanly.
        try:
            refresh_loop = getattr(app.state, "oauth_refresh_loop", None)
            if refresh_loop is not None:
                await refresh_loop.stop()
        except Exception as exc:
            logger.warning("oauth_refresh_loop_stop_failed: %s", exc)

        # Stop the remote-auth revocation sync loop.
        try:
            remote_client = getattr(app.state, "remote_auth_client", None)
            if remote_client is not None and hasattr(remote_client, "close"):
                await remote_client.close()
        except Exception as exc:
            logger.warning("remote_auth_close_failed: %s", exc)

        # Drain remaining tracker events BEFORE we tear down the DB
        # engine (the Postgres backend uses the same factory).
        try:
            from digitorn.core.runtime import run_tracker as _run_tracker
            await _run_tracker.stop()
        except Exception as exc:
            logger.warning("run_tracker_stop_failed: %s", exc)

        await close_db()

        # Worker pool shutdown LAST - modules may use it during cleanup
        await shutdown_worker_pool()

        logger.info("shutdown_complete")

    # Disable OpenAPI docs in production (auth enabled) - the full API schema
    # is an attacker's best friend.  In dev mode (auth disabled) docs are served.
    _auth_enabled = getattr(settings.server, "auth_enabled", True)
    _expose_docs = not _auth_enabled or getattr(settings.server, "expose_docs", False)

    app = FastAPI(
        title="Digitorn",
        description="Modular agent OS bridging AI to operating systems, applications, and devices.",
        version=__version__,
        docs_url="/docs" if _expose_docs else None,
        redoc_url="/redoc" if _expose_docs else None,
        openapi_url="/openapi.json" if _expose_docs else None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        # Allow:
        #   - any localhost port (Flutter web, Next dev, vite previews, …)
        #   - the official ``digitorn.ai`` apex + any subdomain
        #     (``api.digitorn.ai``, ``hub.digitorn.ai``, preview wildcards,
        #     ``staging.digitorn.ai``, …). Operators who run a private
        #     deploy add their own origin via
        #     ``DIGITORN_SERVER__CORS_ORIGINS`` or ``server.cors_origins``
        #     in ``~/.digitorn/config.yaml``.
        allow_origin_regex=(
            r"^https?://("
            r"localhost|127\.0\.0\.1"
            r"|digitorn\.ai|[A-Za-z0-9-]+\.digitorn\.ai"
            r")(:\d+)?$"
        ),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Request-ID",
            "X-Digitorn-Client",
        ],
    )

    # ── Combined middleware (security + context + shutdown + metrics) ───
    # Merging 3 lightweight middlewares into one reduces ASGI middleware
    # stack traversals per request from 4 to 2 (this + rate_limit).

    from digitorn.core.metrics import metrics as _metrics
    from digitorn.core.clients import (
        CLIENT_HEADER as _CLIENT_HEADER,
        parse_client_kind as _parse_client_kind,
    )
    import uuid as _uuid

    # Pre-import JSONResponse and structlog to avoid per-request imports
    from fastapi.responses import JSONResponse as _JSONResponse
    try:
        import structlog as _structlog
        _has_structlog = True
    except ImportError:
        _structlog = None  # type: ignore[assignment]
        _has_structlog = False

    # BUG-062 + BUG-063: hard cap on request body size. The value is
    # generous (16 MiB - enough room for a modest image upload payload)
    # but rejects the 50-MiB-text-message DoS that stalled the event
    # loop ~60s in production testing.
    _MAX_BODY_BYTES = 16 * 1024 * 1024
    # Upload endpoints legitimately move more bytes (images, archives,
    # avatar blobs). The /messages path also moves real bytes now that
    # the composer accepts up to 25 MiB of attachments in a single
    # POST - base64 inflation (~4/3) plus the JSON envelope means we
    # need a cap around 40 MiB so a legitimate 25 MiB upload doesn't
    # hit a stray 413. Still far below the 50 MiB plain-text DoS that
    # justified the guard in the first place.
    _MAX_BODY_BY_PATH_PREFIX = {
        "/messages": 40 * 1024 * 1024,
    }

    # ── Zombie-poll throttle ─────────────────────────────────────────
    # Defends against clients that keep polling a route returning 404
    # in tight loop. We saw this with a stale Flutter session id pinging
    # ``/web-preview?session_id=<gone>`` ~1000 times/min, drowning the
    # event loop and starving real requests. The throttle:
    #
    # 1. Tracks (client_ip, path) → 404 count over a 60s sliding window.
    # 2. After 30 404s in 60s we return 429 with Retry-After: 60 fast,
    #    so the client backs off without us paying for the route call.
    # 3. State is in-memory, per-process. A new process instance starts
    #    clean - safe for restarts. Memory bound by ``_ZOMBIE_MAX_KEYS``.
    #
    # This is policy-side: legitimate clients with bursty traffic on a
    # 200-returning route are NOT impacted.
    _ZOMBIE_THRESHOLD = 30
    _ZOMBIE_WINDOW_S = 60.0
    _ZOMBIE_MAX_KEYS = 2000
    _zombie_counts: dict[tuple[str, str], list[float]] = {}

    def _zombie_should_block(ip: str, path: str, now_ts: float) -> bool:
        key = (ip, path)
        bucket = _zombie_counts.get(key)
        if bucket is None:
            return False
        # Drop entries older than window
        cutoff = now_ts - _ZOMBIE_WINDOW_S
        fresh = [t for t in bucket if t >= cutoff]
        if len(fresh) != len(bucket):
            _zombie_counts[key] = fresh
        return len(fresh) >= _ZOMBIE_THRESHOLD

    def _zombie_record_404(ip: str, path: str, now_ts: float) -> int:
        key = (ip, path)
        # Drop oldest keys if cache is full - cheap LRU-ish.
        if len(_zombie_counts) > _ZOMBIE_MAX_KEYS:
            for stale in list(_zombie_counts.keys())[:200]:
                _zombie_counts.pop(stale, None)
        bucket = _zombie_counts.setdefault(key, [])
        cutoff = now_ts - _ZOMBIE_WINDOW_S
        bucket[:] = [t for t in bucket if t >= cutoff]
        bucket.append(now_ts)
        return len(bucket)

    @app.middleware("http")
    async def combined_middleware(request: Request, call_next):
        # ── Phase 0: Zombie-poll fast-reject ───────────────────────
        # Cheap O(1) check before any other work. Only kicks in for
        # paths that have crossed the 404-spam threshold; everything
        # else falls through unchanged.
        _client = request.client
        _ip = _client.host if _client else "unknown"
        _path = request.url.path
        _now = time.monotonic()
        if _zombie_should_block(_ip, _path, _now):
            return _JSONResponse(
                status_code=429,
                content={
                    "success": False,
                    "error": (
                        "zombie_poll_blocked: same path returned 404 "
                        f">={_ZOMBIE_THRESHOLD} times in last "
                        f"{int(_ZOMBIE_WINDOW_S)}s. Stop polling or fix "
                        "your client's session id."
                    ),
                },
                headers={"Retry-After": "60"},
            )

        # ── Phase 1: Graceful shutdown gate ────────────────────────
        if getattr(app.state, "_shutting_down", False):
            if request.url.path not in ("/health", "/healthz", "/readyz"):
                return _JSONResponse(
                    status_code=503,
                    content={"success": False, "error": "Server is shutting down"},
                    headers={"Retry-After": "5"},
                )

        # ── Phase 1b: Body size guard ──────────────────────────────
        try:
            _cl_header = request.headers.get("content-length")
            _cl = int(_cl_header) if _cl_header else 0
        except ValueError:
            _cl = 0
        if _cl > 0:
            _path = request.url.path
            _limit = _MAX_BODY_BYTES
            for suffix, cap in _MAX_BODY_BY_PATH_PREFIX.items():
                if _path.endswith(suffix):
                    _limit = cap
                    break
            if _cl > _limit:
                return _JSONResponse(
                    status_code=413,
                    content={
                        "success": False,
                        "error": "payload_too_large",
                        "max_bytes": _limit,
                        "got_bytes": _cl,
                    },
                    headers={"Connection": "close"},
                )

        # ── Phase 2: Request context (ID + structlog bind) ────────
        request_id = request.headers.get("x-request-id") or _uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        # Client kind hint - lets downstream handlers branch on
        # ``web`` vs ``flutter-desktop`` etc. without UA sniffing.
        # Read once here, surfaced via ``clients.client_kind_of`` /
        # ``request.state.client_kind`` in handlers.
        request.state.client_kind = _parse_client_kind(
            request.headers.get(_CLIENT_HEADER),
        )
        user_id = getattr(request.state, "user_id", None)
        path = request.url.path
        app_id = None
        if path.startswith("/api/apps/"):
            parts = path.split("/")
            if len(parts) >= 4:
                app_id = parts[3]
        if _has_structlog:
            _structlog.contextvars.clear_contextvars()
            _structlog.contextvars.bind_contextvars(
                request_id=request_id,
                user_id=user_id,
                app_id=app_id,
            )

        # ── Phase 3: Track active requests + call downstream ──────
        app.state._active_requests = getattr(app.state, "_active_requests", 0) + 1
        _metrics.inc_gauge("active_requests")
        t0 = time.monotonic()
        try:
            response = await call_next(request)
            _metrics.inc("http_requests_total", status=str(response.status_code))
        except Exception:
            _metrics.inc("http_requests_total", status="500")
            raise
        finally:
            elapsed = time.monotonic() - t0
            _metrics.observe("http_latency_seconds", elapsed)
            _metrics.inc_gauge("active_requests", delta=-1.0)
            app.state._active_requests = max(0, app.state._active_requests - 1)

        # Track 404s per (ip, path) so we can short-circuit zombie
        # pollers on their next request. We only count 404 (not 401/403/
        # 500) because the symptom we defend against is "client polls a
        # path that is permanently gone". Once the count reaches the
        # threshold the next request gets the 429 fast-reject above.
        if response.status_code == 404:
            count = _zombie_record_404(_ip, _path, _now)
            if count == _ZOMBIE_THRESHOLD:
                logger.warning(
                    "zombie_poll_detected ip=%s path=%s "
                    "(threshold reached at %d 404s/%.0fs window) - "
                    "returning 429 to subsequent requests",
                    _ip, _path, count, _ZOMBIE_WINDOW_S,
                )

        # ── Phase 4: Security headers (on the way out) ────────────
        response.headers["x-request-id"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        # Preview iframes (Vite/HMR dev servers + bundled SDK apps)
        # need inline scripts, eval, and to be embeddable inside the
        # web/Flutter chat panel. The strict CSP/X-Frame-Options stays
        # on every other route.
        # ``/web-static/`` is the new home for bundled SDK app dist
        # served by the daemon (digitorn-builder, digitorn-react-sandbox,
        # ...). Without this carve-out the iframe gets blocked by
        # ``frame-ancestors 'none'``.
        is_preview = (
            "/preview-server/proxy" in path
            or "/preview/" in path
            or "/web-static/" in path
            or "/template-assets/" in path
            # Per-session published static build served at
            # ``/api/apps/{app_id}/sessions/{sid}/published/...`` —
            # iframe-friendly CSP applies same as bundled apps.
            or "/published/" in path
        )
        if is_preview:
            allowed_ancestors = " ".join([
                "'self'",
                "http://localhost:*",
                "http://127.0.0.1:*",
                "https://localhost:*",
                "https://127.0.0.1:*",
            ])
            # Permissive CSP for preview iframes:
            # - script/style/img are wide so app code can use whatever
            #   it wants (data URIs, eval, inline styles).
            # - connect-src is intentionally ``*`` so apps can talk to
            #   third-party APIs (Auth0, Stripe, OpenAI, custom backends)
            #   from the iframe. Risk: a malicious agent could exfiltrate.
            #   Mitigation: per-session sandboxing + the agent has no way
            #   to inject arbitrary code here (only via WsWrite into
            #   files the SDK reads); same threat surface as a normal
            #   PR-merged change.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob: *; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: *; "
                "style-src 'self' 'unsafe-inline' *; "
                "img-src 'self' data: blob: *; "
                "connect-src 'self' ws: wss: http: https: data: blob:; "
                "worker-src 'self' blob:; "
                f"frame-ancestors {allowed_ancestors}"
            )
            # Service-Worker-Allowed lets apps register their service
            # worker at the iframe root path even though the script
            # is served from a sub-path. Without this, registering
            # a SW from /web-static/index.html scopes it under
            # /api/apps/{id}/web-static/ which breaks Vite/CRA SW
            # routing assumptions.
            response.headers["Service-Worker-Allowed"] = "/"
        else:
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' data:; "
                "connect-src 'self' ws: wss: https://cdn.jsdelivr.net; "
                "worker-src 'self' blob: https://cdn.jsdelivr.net; "
                "frame-ancestors 'none'"
            )
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Daemon resource protocol: every HTTP response carries the
        # process-wide instance id. Clients compare to their stored
        # copy on each request — mismatch ⇒ daemon restarted ⇒ wipe
        # local state + re-seed via /snapshot. Cheap (32 bytes), and
        # paired with the same field in the Socket.IO ``connected``
        # handshake so REST-only clients (curl, scripts) get the same
        # restart-detection signal as the live UIs.
        try:
            from digitorn.core.instance import get_instance_id
            response.headers["X-Digitorn-Instance"] = get_instance_id()
        except Exception:
            pass
        return response

    from digitorn.core.rate_limiter import RateLimiter, RateLimitExceeded

    kv_url = settings.server.kv_backend
    rate_limiter = RateLimiter(
        default_rpm=settings.server.rate_limit_rpm,
        backend_url=kv_url,
    )

    # Admin endpoints get stricter rate limits (keyed by prefix).
    _ADMIN_RATE_LIMITS: dict[str, str] = {
        "/api/mcp/":     "__admin_mcp__",       # MCP search/install/remove
        "/api/modules/": "__admin_modules__",    # Module execute
    }
    # Exact-match admin paths
    _ADMIN_EXACT: dict[str, str] = {
        "/api/apps/deploy":        "__admin_deploy__",
        "/api/apps/deploy/upload": "__admin_deploy__",
    }

    # Set admin quotas: half the default RPM (vs full RPM for chat)
    _admin_rpm = max(10, settings.server.rate_limit_rpm // 2)
    for _admin_key in set(_ADMIN_RATE_LIMITS.values()) | set(_ADMIN_EXACT.values()):
        rate_limiter.set_quota(_admin_key, _admin_rpm)

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        path = request.url.path
        rl_key: str | None = None

        # 1. Message / run endpoints → keyed by app_id
        if (
            path.startswith("/api/apps/") and (
                "/messages" in path or path.endswith("/run")
            )
        ):
            parts = path.split("/")
            rl_key = parts[3] if len(parts) >= 4 else None

        # 2. Auth endpoints → fixed key
        elif path in ("/auth/login", "/auth/register"):
            rl_key = "__auth__"

        # 3. Admin endpoints → fixed keys with stricter limits
        else:
            # Exact match first
            rl_key = _ADMIN_EXACT.get(path)
            if rl_key is None:
                # Prefix match
                for prefix, key in _ADMIN_RATE_LIMITS.items():
                    if path.startswith(prefix):
                        rl_key = key
                        break

        # Catch-all ``__api_generic__`` bucket was REMOVED - it caused
        # legitimate high-throughput clients (Flutter UI polling
        # /history, multi-tab sessions, dev tooling) to hit 429 under
        # normal use. Attack-surface paths that actually need pushback
        # still get their specific bucket above (auth, admin, messages,
        # deploy, mcp, modules).

        if rl_key:
            user_id = getattr(request.state, "user_id", None)
            try:
                rate_limiter.check(rl_key, user_id=user_id)
            except RateLimitExceeded as exc:
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=429,
                    content={
                        "success": False,
                        "error": str(exc),
                        "retry_after": round(exc.retry_after, 1),
                    },
                    headers={"Retry-After": str(int(exc.retry_after) + 1)},
                )
        return await call_next(request)

    # Per-session preview snapshot cache. Keeps GET /preview under a
    # millisecond on warm sessions, ~5-15 ms on signature re-validation,
    # falls back to full disk hydration only on cold misses or when the
    # signature mismatches. Watchers pick up external edits in real time
    # for the top ``max_watchers`` most-recently-used sessions, so disk
    # stays the source of truth without per-request IO.
    try:
        from digitorn.core.cache import WorkspaceCacheService as _WorkspaceCache
        _workspace_cache = _WorkspaceCache(
            max_watchers=getattr(settings.server, "workspace_cache_watchers", 1000),
        )
    except Exception as _exc:
        logger.warning("workspace_cache_init_failed: %s", _exc)
        _workspace_cache = None

    app.state.settings = settings
    app.state.sio = sio
    app.state.session_bus = session_event_bus
    app.state.event_bus = event_bus
    app.state.rate_limiter = rate_limiter
    app.state.live_ops = _live_ops_registry
    app.state.workspace_cache = _workspace_cache

    auth_enabled = getattr(settings.server, "auth_enabled", True)

    bind_host = getattr(settings.server, "host", "127.0.0.1")
    if not auth_enabled and bind_host not in ("127.0.0.1", "localhost", "::1"):
        insecure = getattr(settings.server, "insecure", False)
        if not insecure:
            raise RuntimeError(
                f"Refusing to bind to {bind_host} without authentication. "
                "Set server.auth_enabled=true, bind to 127.0.0.1, "
                "or set server.insecure=true to override."
            )
        logger.warning(
            "INSECURE: daemon bound to %s without authentication", bind_host,
        )

    auth_mode = getattr(settings.auth, "mode", "remote")

    if auth_enabled:
        # The daemon does not sign tokens. It trusts JWTs issued by the
        # configured digitorn-auth service and verifies them against
        # that service's RSA public key (JWKS). Optionally loads
        # LocalDeviceAuth so the user's identity can be authenticated
        # even when the central is unreachable.
        if auth_mode != "remote":
            raise RuntimeError(
                f"auth.mode={auth_mode!r} is invalid - the only "
                "supported mode is 'remote'. The daemon consumes "
                "tokens from a central digitorn-auth service. Set "
                "auth.service_url=https://<your-auth-service>."
            )

        from digitorn_auth.fastapi import RemoteAuthMiddleware
        service_url = (getattr(settings.auth, "service_url", "") or "").rstrip("/")
        if not service_url:
            raise RuntimeError(
                "auth.mode='remote' requires auth.service_url to be set"
            )
        accept_issuers = list(getattr(settings.auth, "accept_issuers", []) or [])
        # ``allow_paths`` extension - skip bearer-token enforcement on
        # the preview iframe routes. Once the iframe HTML is loaded
        # the browser fetches its assets (JS / CSS / images) WITHOUT
        # being able to attach the bearer token (browsers don't carry
        # the token automatically across <script src> / <link href>
        # requests). Forcing auth on assets means a black screen.
        #
        # Risk model: the assets served here are compiled JS/CSS from
        # the app's own ``web/dist/``, content shipped publicly by
        # design. The ``app_id`` in the URL is also public (visible
        # in any deployed-apps list). No PII / no secrets / no
        # workspace data is exposed.
        #
        # Workspace API and Socket.IO remain auth-enforced - those DO
        # carry session data and need a real token (still works via
        # ``Authorization`` header for fetch / ``?token=`` for WS).
        preview_allow = [
            "/health",
            "/health/*",   # /health/web_preview, future /health/<module>
            "/healthz",
            "/.well-known/*",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/auth/*",
            # Preview iframe + its asset loads (compiled bundle only).
            "/api/apps/*/preview",
            "/api/apps/*/preview/",
            "/api/apps/*/preview/*",
            "/api/apps/*/preview-server/proxy",
            "/api/apps/*/preview-server/proxy/",
            "/api/apps/*/preview-server/proxy/*",
            # Bundled SDK app dist (digitorn-builder, digitorn-react-sandbox,
            # ...). Same risk model as ``/preview/``: compiled JS/CSS
            # shipped publicly by design, no PII or workspace data here.
            # The browser can't attach the bearer token to <script src>
            # / <link href> requests, so without this allow rule the
            # iframe loads a blank page (HTML 200 but assets all 401).
            "/api/apps/*/web-static",
            "/api/apps/*/web-static/",
            "/api/apps/*/web-static/*",
            # Template gallery previews (iframe-loaded). Same risk
            # model: inert pre-built dist/index.html + bundled JS/CSS
            # the app ships publicly. Browsers can't attach the bearer
            # token to <iframe src=> loads, so auth means a black box.
            "/api/apps/*/template-assets/*",
            # Per-session published static builds (output of
            # ``PreviewPublish``). Same iframe constraint: assets
            # under ``/published/`` are fetched without the bearer
            # token. The ``session_id`` in the URL is the only access
            # gate; treat it as a capability token. Hardening with
            # a crypto-derived URL token is a v1.1 item — for the
            # launch, the session_id (UUID, ~128 bits of entropy)
            # is a reasonable cap on guessability.
            "/api/apps/*/sessions/*/published",
            "/api/apps/*/sessions/*/published/",
            "/api/apps/*/sessions/*/published/*",
            # Anonymous-friendly read-only views (system-scoped apps
            # catalogue for the public landing). The endpoint itself
            # filters to ``scope=system`` so no per-user data leaks.
            "/api/public",
            "/api/public/",
            "/api/public/*",
        ]
        app.add_middleware(
            RemoteAuthMiddleware,
            issuer=service_url,
            accept_issuers=accept_issuers,
            allow_paths=preview_allow,
        )
        app.state.auth_service_url = service_url
        app.state.auth_accept_issuers = accept_issuers
        logger.info("auth_enabled mode=remote service_url=%s", service_url)

        # Adapter exposing the legacy ``verify_access_token`` API used
        # by Socket.IO and the preview WS upgrade. Forwards to the
        # ``RemoteAuthClient`` installed on app.state during lifespan.
        class _RemoteVerifier:
            def __init__(self, target_app):
                self._app = target_app

            def verify_access_token(self, token: str):
                client = getattr(self._app.state, "remote_auth_client", None)
                if client is None:
                    raise RuntimeError("remote_auth_client not yet installed")
                return client.verify(token)

        _auth_holder["service"] = _RemoteVerifier(app)

        # Optional offline identity. Loaded best-effort: if the daemon
        # hasn't been paired yet (`digitorn install-local`), we skip
        # without crashing - the user can still authenticate online
        # against the central auth service the standard way.
        if getattr(settings.auth, "enable_local_device", False):
            try:
                from digitorn.core.auth.local_device import (
                    LocalDeviceAuth,
                    NotPaired,
                )
                from digitorn.core.auth.device_revalidator import revalidate_loop
                import asyncio as _asyncio

                try:
                    local_auth = LocalDeviceAuth.load()
                    app.state.local_auth = local_auth
                    app.state.local_device_revalidator = _asyncio.create_task(
                        revalidate_loop(local_auth, interval_s=3600),
                    )
                    logger.info(
                        "local_device_auth_loaded user=%s device=%s expires_in_days=%d",
                        local_auth.user_email,
                        local_auth.device_id,
                        local_auth.days_until_expiry,
                    )
                except NotPaired:
                    app.state.local_auth = None
                    logger.info(
                        "local_device_auth_not_paired - run `digitorn install-local` "
                        "to enable offline auth"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("local_device_auth_init_failed: %s", exc)
                app.state.local_auth = None
    else:
        # Auth disabled - dev / single-machine mode. No middleware,
        # no token verification. Socket.IO falls back to a synthetic
        # 'local' admin user.
        logger.warning("auth_disabled - all requests treated as 'local'")

    # Switched to the apps_v2 split package on 2026-04-25 - same routes,
    # smaller files. The legacy apps.py is left intact as a fallback;
    # to roll back, swap the import below back to ``apps``.
    from digitorn.core.api.apps_v2 import router as apps_router
    from digitorn.core.api.builder import router as builder_router
    from digitorn.core.api.config import router as config_router
    from digitorn.core.api.credentials import (
        oauth_callback_router,
        router as credentials_router,
    )
    from digitorn.core.api.discovery import router as discovery_router
    from digitorn.core.api.mcp import router as mcp_router
    from digitorn.core.api.modules import router as modules_router
    # /api/packages was physically removed on 2026-04-21 - all install
    # lifecycle routes now live under /api/apps/* via apps_install_router.
    from digitorn.core.api.apps_install import router as apps_install_router
    from digitorn.core.api.hub import router as hub_router
    from digitorn.core.api.public import router as public_router
    from digitorn.core.api.requires import router as requires_router
    from digitorn.core.api.security import router as security_router
    from digitorn.core.api.transcribe import router as transcribe_router
    from digitorn.core.api.ui import router as ui_router
    from digitorn.core.api.user import (
        admin_router as user_admin_router,
        router as user_router,
    )

    # Daemon-side /auth/* routes (login, register, refresh, /me,
    # oauth/*, …) are owned by the central auth service. We redirect
    # every /auth/* call to the central; 308 preserves method + body
    # so a `POST /auth/login` from a stale client lands cleanly. This
    # is a passthrough proxy - it MUST stay active even when
    # auth_enabled=False (dev mode), otherwise the web app gets a 404
    # on every login attempt routed through the daemon URL.
    _service_url = (getattr(settings.auth, "service_url", "") or "").rstrip("/")
    if _service_url:
        from fastapi import APIRouter
        from fastapi.responses import RedirectResponse
        _auth_redirect_router = APIRouter(prefix="/auth", tags=["auth-redirect"])

        @_auth_redirect_router.api_route(
            "/{rest:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            include_in_schema=False,
        )
        async def _auth_redirect(rest: str, request: Request):
            target = f"{_service_url}/auth/{rest}"
            qs = request.url.query
            if qs:
                target = f"{target}?{qs}"
            return RedirectResponse(url=target, status_code=308)

        app.include_router(_auth_redirect_router)
        logger.info("auth_routes_redirected_to=%s", _service_url)
    app.include_router(apps_router)
    app.include_router(modules_router)
    app.include_router(requires_router)
    app.include_router(config_router)
    app.include_router(security_router)
    app.include_router(mcp_router)
    app.include_router(discovery_router)
    app.include_router(builder_router)
    app.include_router(credentials_router)
    app.include_router(oauth_callback_router)
    app.include_router(apps_install_router)
    app.include_router(hub_router)
    app.include_router(public_router)
    app.include_router(user_router)
    app.include_router(user_admin_router)
    from digitorn.core.api.gateway_admin import router as gateway_admin_router
    app.include_router(gateway_admin_router)
    from digitorn.core.api.daemon_admin import router as daemon_admin_router
    app.include_router(daemon_admin_router)
    app.include_router(transcribe_router)
    app.include_router(ui_router)

    # --- Global exception handler - structured JSON for unhandled errors ---
    from fastapi.responses import JSONResponse
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if isinstance(exc.detail, dict):
            body = {"success": False, "detail": exc.detail, "status_code": exc.status_code}
        else:
            # Keep "detail" key for backward compat with existing consumers
            body = {"success": False, "error": exc.detail, "detail": exc.detail, "status_code": exc.status_code}
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        # Pydantic v2 error entries can carry a non-JSON-serialisable
        # ``ctx`` (the raw ValueError/AssertionError that the validator
        # raised). Sanitise the structure before the JSONResponse
        # serialiser chokes on it and turns a clean 422 into a 500
        # "Internal server error" - that's what a recent
        # ``@model_validator(mode="before")`` regression exposed.
        raw_errors = exc.errors()
        details: list[dict[str, Any]] = []
        for err in raw_errors:
            safe: dict[str, Any] = {}
            for k, v in err.items():
                try:
                    import json as _json
                    _json.dumps(v)
                    safe[k] = v
                except (TypeError, ValueError):
                    safe[k] = repr(v)[:500]
            details.append(safe)
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Validation error",
                "details": details,
                "status_code": 422,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error(
            "unhandled_exception request_id=%s path=%s error=%s",
            request_id, request.url.path, exc, exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Internal server error",
                "request_id": request_id,
                "status_code": 500,
            },
        )

    @app.get("/health")
    async def health(request: Request) -> dict:
        result: dict[str, Any] = {
            "status": "ok",
            "version": __version__,
            "socketio": True,
            # True while reload_from_db is still populating the app
            # registry in the background. Clients that want to wait
            # before issuing /api/apps calls should poll this flag.
            "warming_up": bool(getattr(request.app.state, "warming_up", False)),
        }

        # System metrics (best-effort - psutil may not be installed).
        # ``proc.open_files()`` and ``proc.connections()`` are slow on
        # Windows (~500 ms combined - they enumerate every handle /
        # TCP socket via slow NTFS / Winsock APIs). Skip them by
        # default and expose them only when the caller opts in with
        # ``?detailed=1`` so the hot path stays <10 ms.
        detailed = request.query_params.get("detailed") in ("1", "true", "yes")
        try:
            import psutil
            proc = psutil.Process()
            sys_info: dict[str, Any] = {
                "cpu_percent": proc.cpu_percent(interval=0),
                "memory_mb": round(proc.memory_info().rss / 1024 / 1024, 1),
                "threads": proc.num_threads(),
            }
            if detailed:
                sys_info["open_files"] = len(proc.open_files())
                sys_info["connections"] = len(proc.connections())
            result["system"] = sys_info
        except (ImportError, Exception):
            result["system"] = {"available": False}

        # Event loop lag - measures how responsive the loop is
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.sleep(0)
        lag_ms = (loop.time() - t0) * 1000
        result["event_loop_lag_ms"] = round(lag_ms, 2)

        # Cumulative stall counters from the active watchdog.
        wd = getattr(request.app.state, "loop_watchdog", None)
        if wd is not None:
            result["event_loop_watchdog"] = wd.get_state()

        # Worker pool stats
        wp = getattr(request.app.state, "worker_pool", None)
        if wp:
            result["workers"] = wp.stats

        # Degraded-mode signal for reverse proxies / load balancers.
        # When the loop is currently lagging >500ms we flip status to
        # "degraded" so upstream can drain / shed load instead of
        # piling new requests onto a stuck worker. Also surfaces when
        # the turn pool is saturated.
        degraded = False
        if lag_ms > 500:
            degraded = True
        try:
            if wp and wp.stats.get("turn_pool", {}).get("active_turns", 0) \
                    >= wp.stats.get("turn_pool", {}).get("max_workers", 0):
                degraded = True
        except Exception:
            pass
        if degraded:
            result["status"] = "degraded"

        return result

    @app.get("/healthz")
    async def healthz(request: Request) -> dict:
        """Lightweight liveness probe for external supervisors.

        Designed to be called every few seconds by:
          - container orchestrators (k8s livenessProbe, Docker HEALTHCHECK)
          - the ``digitorn-supervisor`` external watchdog
          - load balancers (ALB / nginx) doing health-based routing

        Cheap (no psutil, no proc enumeration). Cap at sub-millisecond
        so calling it every 5s adds negligible load. Returns:

          - ``status``: ``alive | warming | degraded`` (machine-readable)
          - ``boot_phase``: which lifespan step is currently running
            (``init_db | remote_auth_warm | lifecycle.start_all | ready``)
          - ``warming_up``: True while ``reload_from_db`` is still
            populating the app registry
          - ``uptime_s``: seconds since the lifespan completed
            (``-1`` if still booting)
          - ``loop_lag_ms``: instantaneous loop lag - external watchdogs
            consider >1000ms persistent as a restart trigger
          - ``loop_stalls_total``: cumulative stalls since boot (from
            the in-process ``LoopWatchdog``)
        """
        boot_phase = getattr(request.app.state, "boot_phase", "booting")
        boot_completed_at = getattr(
            request.app.state, "boot_completed_at", None,
        )
        warming_up = bool(getattr(request.app.state, "warming_up", False))

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.sleep(0)
        lag_ms = round((loop.time() - t0) * 1000, 2)

        wd = getattr(request.app.state, "loop_watchdog", None)
        stalls_total = 0
        if wd is not None:
            try:
                stalls_total = int(wd.get_state().get("stalls_total", 0))
            except Exception:
                pass

        if boot_phase != "ready":
            status = "warming"
        elif lag_ms > 500:
            status = "degraded"
        else:
            status = "alive"

        uptime_s = (
            round(time.monotonic() - boot_completed_at, 1)
            if boot_completed_at is not None else -1
        )
        return {
            "status": status,
            "boot_phase": boot_phase,
            "warming_up": warming_up,
            "uptime_s": uptime_s,
            "loop_lag_ms": lag_ms,
            "loop_stalls_total": stalls_total,
        }

    @app.get("/health/web_preview")
    async def health_web_preview() -> dict:
        """Operational metrics for the ``web_preview`` module.

        Public alias of ``GET /api/modules/web_preview/health``,
        allow-listed by the auth middleware so an operator can curl
        it from outside without minting an admin token. Exposes only
        operational counts (no user data, no session payloads):

            {
              "status": "ok",
              "module_id": "web_preview",
              "version": "1.0.0",
              "count": 12,
              "by_type": {"proxy": 4, "static": 8},
              "by_user": {"u1": 3, "u2": 4, "anonymous": 5},
              "by_user_count": 3,
              "session_count": 7,
              "oldest_age_seconds": 3421.5,
              "oldest_idle_seconds": 1287.3,
              "limits": {
                "max_per_session": 5,
                "max_per_user": 20,
                "idle_reap_after_seconds": 1800
              }
            }
        """
        from digitorn.modules.web_preview.module import WebPreviewModule
        try:
            mgr = getattr(app.state, "app_manager", None)
            registry = getattr(mgr, "_module_registry", None) or getattr(mgr, "_registry", None) if mgr else None
            mod = None
            if registry is not None:
                try:
                    mod = registry.get("web_preview")
                except Exception:
                    mod = None
            if mod is None:
                return {
                    "status": "unloaded",
                    "module_id": "web_preview",
                    "detail": "web_preview module not loaded by any deployed app",
                }
            return await mod.health_check()
        except Exception as exc:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=500,
                content={"status": "error", "module_id": "web_preview", "error": str(exc)},
            )

    @app.get("/readyz")
    async def readyz(request: Request) -> dict:
        if getattr(request.app.state, "_shutting_down", False):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=503, content={"status": "draining"})
        mgr = getattr(request.app.state, "app_manager", None)
        wp = getattr(request.app.state, "worker_pool", None)
        return {
            "status": "ready",
            "database": getattr(request.app.state, "engine", None) is not None,
            "deployed_apps": len(mgr._deployed) if mgr else 0,
            "active_requests": getattr(request.app.state, "_active_requests", 0),
            "workers": wp.stats if wp else None,
        }

    @app.get("/api/metrics")
    async def api_metrics() -> dict:
        return _metrics.snapshot()

    @app.get("/api/metrics/prometheus")
    async def api_metrics_prometheus() -> "Response":
        from fastapi.responses import Response
        return Response(
            content=_metrics.prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # Conventional Prometheus endpoint location - many scrapers look for
    # `/metrics` by default and don't let you configure a path. Mirror
    # the prometheus variant here so standard deployments work.
    @app.get("/metrics")
    async def metrics_prometheus() -> "Response":
        from fastapi.responses import Response
        return Response(
            content=_metrics.prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/api/metrics/sessions")
    async def api_metrics_sessions(app_id: str | None = None) -> list[dict]:
        from digitorn.core.runtime.session_metrics import list_active_metrics
        return list_active_metrics(app_id)

    @app.get("/api/metrics/sessions/{session_id}")
    async def api_metrics_session(session_id: str, app_id: str | None = None) -> dict:
        from digitorn.core.runtime.session_metrics import _sessions
        for sm in _sessions.values():
            if sm.session_id == session_id and (not app_id or sm.app_id == app_id):
                return sm.snapshot()
        return {"error": "session not found"}

    @app.get("/api/metrics/apps/{app_id}")
    async def api_metrics_app(app_id: str) -> dict:
        from digitorn.core.runtime.session_metrics import app_summary
        return app_summary(app_id)

    @app.post("/api/admin/sessionstore/loadtest")
    async def api_admin_sessionstore_loadtest(payload: dict) -> dict:
        """Phase 4 capacity probe: writes ``sessions x events_per_session``
        events directly into the in-memory SessionStore via the in-process
        bridge, then returns throughput/latency metrics.

        Bypasses HTTP per-event + the LLM gateway -- isolates store
        capacity from upstream slowness. Used by the
        ``phase4_sustained_load`` baseline scenario.
        """
        import asyncio as _aio
        import time as _t
        import uuid as _uuid
        import hashlib as _hl
        from pathlib import Path as _Path
        from digitorn.core.runtime.session_store.bridge import (
            get_default_bridge,
        )
        from digitorn.core.runtime.session_store.types import Event

        sessions = int(payload.get("sessions") or 100)
        events_per_session = int(payload.get("events_per_session") or 5)
        sessions = min(max(sessions, 1), 100_000)
        events_per_session = min(max(events_per_session, 1), 1000)

        bridge = get_default_bridge()
        if bridge is None:
            return {"success": False, "error": "no bridge registered"}

        store = bridge.store
        sids = [
            f"loadtest-{_uuid.uuid4().hex[:6]}-{i:05d}"
            for i in range(sessions)
        ]

        for sid in sids:
            await store.open(
                sid, app_id="phase4-load", user_id="phase4-user",
                create_if_missing=True, pin=True,
            )

        async def _write_one(sid: str, idx: int) -> None:
            ev = Event(
                type="user_message",
                kind="event",
                content=f"sustained-{idx}",
                payload={"content": f"sustained-{idx}"},
                user_id="phase4-user",
                app_id="phase4-load",
            )
            await store.append_event(sid, ev)

        flusher_dropped_before = int(store.stats().get("flusher_dropped") or 0)

        t0 = _t.perf_counter()
        await _aio.gather(*[
            _write_one(sid, e)
            for sid in sids
            for e in range(events_per_session)
        ])
        write_elapsed = _t.perf_counter() - t0

        await store.flusher.flush()
        flush_elapsed = _t.perf_counter() - t0

        # Verify a sample of sessions on disk.
        sample = sids[: min(50, len(sids))]
        durable_total = 0
        bad_sessions: list[str] = []
        for sid in sample:
            h = _hl.sha256(sid.encode("utf-8")).hexdigest()
            sdir = store.root / h[:2] / h[2:4] / sid
            evs_file = sdir / "events.jsonl"
            if not evs_file.exists():
                bad_sessions.append(f"{sid}: events.jsonl missing")
                continue
            with evs_file.open("r", encoding="utf-8") as f:
                count = sum(1 for _ in f)
            durable_total += count
            if count != events_per_session:
                bad_sessions.append(
                    f"{sid}: {count} events (expected {events_per_session})"
                )

        # Drop pin so the test sessions can be evicted normally.
        for sid in sids:
            st = store.state(sid)
            if st is not None:
                st.pinned = False

        stats = store.stats()
        shard_stats = store.flusher.shard_stats()
        # Aggregate per-shard view to expose which shards wrote vs.
        # which silently dropped (errors caught by the resilient _run
        # would show as written < expected for that shard).
        total_shard_written = sum(s["written"] for s in shard_stats)
        total_shard_dropped = sum(s["dropped"] for s in shard_stats)
        empty_shards = sum(1 for s in shard_stats if s["written"] == 0)
        max_queue = max(s["queue_size"] for s in shard_stats)
        return {
            "success": True,
            "data": {
                "write_seconds": round(write_elapsed, 3),
                "flush_seconds": round(flush_elapsed, 3),
                "events_per_sec_in": round(
                    (sessions * events_per_session) / max(write_elapsed, 0.001), 1,
                ),
                "events_per_sec_total": round(
                    (sessions * events_per_session) / max(flush_elapsed, 0.001), 1,
                ),
                "durable_sample": durable_total,
                "expected_sample": len(sample) * events_per_session,
                "bad_sessions": bad_sessions[:5],
                "flusher_dropped_delta": (
                    int(stats.get("flusher_dropped") or 0) - flusher_dropped_before
                ),
                "append_event_p50_ms": stats.get("append_event_p50_ms"),
                "append_event_p95_ms": stats.get("append_event_p95_ms"),
                "append_event_p99_ms": stats.get("append_event_p99_ms"),
                "append_event_samples": stats.get("append_event_samples"),
                "shard_total_written": total_shard_written,
                "shard_total_dropped": total_shard_dropped,
                "shard_empty_count": empty_shards,
                "shard_max_queue": max_queue,
                "shard_count": len(shard_stats),
            },
        }

    @app.get("/api/metrics/session_store")
    async def api_metrics_session_store() -> dict:
        """Phase 6: hot-path health for the in-memory SessionStore.

        Includes ``append_event`` p50/p95/p99 latency, in-memory session
        count, byte budget, flusher write/drop counters, bridge mode and
        routed/dropped tallies. Returns ``{}`` when the bridge is OFF
        (legacy session store still in use)."""
        from digitorn.core.runtime.session_store.bridge import (
            get_default_bridge,
        )
        bridge = get_default_bridge()
        if bridge is None:
            return {"mode": "off"}
        return {
            **bridge.stats(),
            **bridge.store.stats(),
        }

    asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)

    return asgi_app


cli = typer.Typer(
    name="digitorn",
    help="Digitorn - Modular agent OS.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

# Server-side commands only - client commands are in the digitorn_cli package
from digitorn.core.cli.doctor import doctor as doctor_command  # noqa: E402
from digitorn.core.cli.init import init as init_command  # noqa: E402
from digitorn.core.cli.app import app_cli  # noqa: E402
from digitorn.core.cli.modules import modules_cli  # noqa: E402
from digitorn.core.cli.package import package_cli  # noqa: E402
from digitorn.core.cli.hub import hub_cli  # noqa: E402
from digitorn.core.cli.requires import requires_cli  # noqa: E402
from digitorn.core.cli.secret import secret_cli  # noqa: E402
from digitorn.core.cli.credentials import credentials_cli  # noqa: E402
from digitorn.core.cli.yaml_migrate import yaml_cli  # noqa: E402
from digitorn.core.cli.mcp_cli import mcp_cli  # noqa: E402
from digitorn.core.cli.middleware_cli import middleware_cli  # noqa: E402
from digitorn.core.cli.dev import dev_cli  # noqa: E402
from digitorn.core.cli.install import install_cli  # noqa: E402
from digitorn.core.cli.db import db_cli  # noqa: E402

cli.command(name="init")(init_command)
cli.command(name="doctor")(doctor_command)
cli.add_typer(modules_cli)
cli.add_typer(requires_cli)
cli.add_typer(app_cli)
cli.add_typer(secret_cli)
cli.add_typer(credentials_cli)
cli.add_typer(yaml_cli)
cli.add_typer(mcp_cli)
cli.add_typer(middleware_cli)
cli.add_typer(package_cli)
cli.add_typer(hub_cli)
cli.add_typer(dev_cli)
cli.add_typer(install_cli)
cli.add_typer(db_cli)


_DEFAULT_DAEMON = "http://127.0.0.1:8000"


def _run_first_time_setup_if_needed() -> None:
    """Interactive first-time setup if no config file exists."""
    config_path = Path.home() / ".digitorn" / "config.yaml"
    if config_path.exists():
        return

    import yaml as _yaml

    console.print()
    console.print("[bold cyan]Digitorn[/bold cyan] - First run setup\n")

    console.print("  Database backend:")
    console.print("    [bold]1[/bold] SQLite (local, zero config) - recommended for development")
    console.print("    [bold]2[/bold] PostgreSQL (production, multi-user)")
    console.print()

    choice = Prompt.ask("  Choose", choices=["1", "2"], default="1")

    config_data: dict = {}

    if choice == "2":
        console.print()
        pg_host = Prompt.ask("  PostgreSQL host", default="localhost")
        pg_port = Prompt.ask("  PostgreSQL port", default="5432")
        pg_db = Prompt.ask("  Database name", default="digitorn")
        pg_user = Prompt.ask("  Username", default="digitorn_user")
        pg_pass = Prompt.ask("  Password", password=True)

        db_url = f"postgresql+asyncpg://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"

        console.print()
        console.print("  Testing connection...", end=" ")

        import asyncio
        try:
            import asyncpg
            conn = asyncio.get_event_loop().run_until_complete(
                asyncpg.connect(
                    host=pg_host,
                    port=int(pg_port),
                    database=pg_db,
                    user=pg_user,
                    password=pg_pass,
                    timeout=5,
                )
            )
            asyncio.get_event_loop().run_until_complete(conn.close())
            console.print("[bold green]OK[/bold green]")
        except asyncpg.InvalidCatalogNameError:
            console.print("[yellow]database not found[/yellow]")
            console.print(f"  Creating database '{pg_db}'...", end=" ")
            try:
                sys_conn = asyncio.get_event_loop().run_until_complete(
                    asyncpg.connect(
                        host=pg_host,
                        port=int(pg_port),
                        database="postgres",
                        user=pg_user,
                        password=pg_pass,
                        timeout=5,
                    )
                )
                import re as _re
                if not _re.fullmatch(r"[A-Za-z0-9_]+", pg_db):
                    console.print(f"[bold red]INVALID[/bold red] - database name must be alphanumeric: {pg_db!r}")
                    raise typer.Exit(1)
                asyncio.get_event_loop().run_until_complete(
                    sys_conn.execute(f'CREATE DATABASE "{pg_db}"')
                )
                asyncio.get_event_loop().run_until_complete(sys_conn.close())
                console.print("[bold green]OK[/bold green]")
            except Exception as create_exc:
                console.print(f"[bold red]FAILED[/bold red] - {create_exc}")
                console.print(f"  Create the database manually: CREATE DATABASE {pg_db};")
                console.print("  Then run [cyan]digitorn start[/cyan] again.")
                raise typer.Exit(1)
        except Exception as exc:
            console.print(f"[bold red]FAILED[/bold red] - {exc}")
            console.print("  Fix the connection and run [cyan]digitorn start[/cyan] again.")
            raise typer.Exit(1)

        config_data["database"] = {"url": db_url}
    else:
        db_path = str(Path.home() / ".digitorn" / "digitorn.db")
        config_data["database"] = {"url": f"sqlite+aiosqlite:///{db_path}"}

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_yaml.dump(config_data, default_flow_style=False))

    console.print()
    console.print(f"  Configuration saved to [cyan]{config_path}[/cyan]")
    console.print()


async def _deploy_builtin_apps(manager: Any) -> None:
    """Deploy built-in apps that are always available.

    Built-in apps live in packages/digitorn/core/builtin_apps/.
    They are deployed with force=True (idempotent) and marked as builtin
    so they cannot be undeployed by the user.

    The default model config from settings is injected into the YAML
    before deployment - nothing is hardcoded.
    """
    builtin_dir = Path(__file__).parent / "builtin_apps"
    if not builtin_dir.exists():
        return

    # Read default model from global config
    from digitorn.core.config import get_settings
    cfg = get_settings().default_model

    for yaml_file in sorted(builtin_dir.glob("*.yaml")):
        app_id = yaml_file.stem.replace("_", "-")
        try:
            if manager.is_deployed(app_id):
                logger.debug("builtin_app_already_deployed app=%s", app_id)
                continue

            # Inject default model config into the YAML before deploying.
            # YAML 1.2 strict bool rules via the central loader.
            from digitorn.core.app.yaml_loader import safe_load_strict
            raw = safe_load_strict(yaml_file.read_text(encoding="utf-8"))
            agents = raw.get("agents", [])
            for agent in agents:
                brain = agent.get("brain", {})
                brain["provider"] = cfg.provider
                brain["model"] = cfg.model
                if cfg.backend:
                    brain["backend"] = cfg.backend
                brain["config"] = {"api_key": cfg.api_key}
                if cfg.base_url:
                    brain["config"]["base_url"] = cfg.base_url
                brain["temperature"] = cfg.temperature
                brain["max_tokens"] = cfg.max_tokens
                brain.setdefault("context", {})["max_tokens"] = cfg.context_window
                agent["brain"] = brain

            # Write patched YAML to temp file and deploy from it
            import tempfile
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False, encoding="utf-8",
            ) as tmp:
                _yaml.dump(raw, tmp, allow_unicode=True, default_flow_style=False)
                tmp_path = Path(tmp.name)

            try:
                deployed = await manager.deploy(tmp_path, force=True)
                if deployed:
                    deployed.builtin = True
                    logger.info(
                        "builtin_app_deployed app=%s model=%s/%s",
                        app_id, cfg.provider, cfg.model,
                    )
            finally:
                tmp_path.unlink(missing_ok=True)

        except Exception as exc:
            logger.warning("builtin_app_deploy_failed app=%s: %s", app_id, exc)


def _banner(host: str, port: int, db_url: str) -> None:
    """Print the startup banner."""
    banner_text = r"""
     ____  _       _ _
    |  _ \(_) __ _(_) |_ ___  _ __ _ __
    | | | | |/ _` | | __/ _ \| '__| '_ \
    | |_| | | (_| | | || (_) | |  | | | |
    |____/|_|\__, |_|\__\___/|_|  |_| |_|
             |___/
    """

    console.print(banner_text, style="bold cyan")

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold white")
    table.add_column(style="green")
    table.add_row("Version", __version__)
    table.add_row("Server", f"http://{host}:{port}")
    table.add_row("Docs", f"http://{host}:{port}/docs")
    table.add_row("Database", _mask_db_url(db_url))

    console.print(Panel(table, title="[bold]Digitorn Daemon[/bold]", border_style="cyan"))
    console.print()


def _mask_db_url(url: str) -> str:
    """Mask password in database URL for display."""
    if "@" in url and ":" in url.split("@")[0]:
        parts = url.split("@")
        creds = parts[0]
        scheme_user = creds.rsplit(":", 1)[0]
        return f"{scheme_user}:****@{parts[1]}"
    return url


@cli.command()
def start(
    host: str = typer.Option("127.0.0.1", help="Host to bind to."),
    port: int = typer.Option(8000, help="Port to listen on."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of worker processes (1-16)."),
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.yaml.")
    ] = None,
    app_yaml: Annotated[
        Path | None, typer.Option("--app", "-a", help="Path to app YAML to bootstrap at startup.")
    ] = None,
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload (dev only)."),
    log_level: str = typer.Option("info", help="Log level."),
    web: bool = typer.Option(False, "--web/--no-web", help="Start the web client dev server."),
    web_port: int = typer.Option(5173, "--web-port", help="Web client port."),
    tls_cert: Annotated[
        Path | None, typer.Option("--tls-cert", help="Path to TLS certificate file (.pem).")
    ] = None,
    tls_key: Annotated[
        Path | None, typer.Option("--tls-key", help="Path to TLS private key file (.pem).")
    ] = None,
    sandbox: bool | None = typer.Option(
        None, "--sandbox/--no-sandbox",
        help="OS-level sandbox. Use --no-sandbox to disable. Default: config file value.",
    ),
) -> None:
    """Start the Digitorn daemon."""
    # ── Self-relaunch through the project venv ────────────────────
    # Make sure ``digitorn start`` always runs against the fresh
    # dependency set pinned in the project venv (uvicorn >= 0.35
    # with websockets-sansio, winloop, latest fastapi/starlette,
    # ...) regardless of which ``digitorn.exe`` the user's PATH
    # happened to resolve (typically the global Python's, which has
    # stale versions). If we are not already in the venv AND the
    # venv has its own ``digitorn`` launcher, re-exec through it.
    # Sentinel env var prevents infinite recursion on second pass.
    _relaunch_in_project_venv_if_needed()

    import subprocess as _sp
    from digitorn.core.config import Settings, override_settings
    from digitorn.core.process_group import install as _install_process_group
    from digitorn.core import windows_setup as _winsetup

    # ── Windows: loop policy + AV hint ───────────────────────────
    # The actual policy install lives in
    # ``_install_windows_event_loop_policy()`` (called at module
    # import and re-called just before ``uvicorn.run`` below) so a
    # SINGLE code path picks winloop > Proactor consistently across
    # the main process, the reload supervisor, and worker children.
    # Here we just surface a visible "which loop is active" line for
    # the operator, plus a hint when Defender exclusions are missing.
    if _winsetup.is_windows():
        if _winsetup.winloop_available():
            console.print(
                "[dim]Windows: winloop event loop active (libuv).[/dim]"
            )
        else:
            console.print(
                "[yellow]Windows: winloop not installed -- falling back to "
                "ProactorEventLoop. Run `digitorn windows-setup` once for "
                "a stall-free dev experience.[/yellow]"
            )
        if not _winsetup.check_exclusions_present():
            console.print(
                "[yellow]Windows: Defender exclusions missing for "
                "python.exe / ~/.digitorn. Every socket() and file read "
                "is being scanned. Run `digitorn windows-setup` once "
                "(admin) for ~10x better local-dev performance.[/yellow]"
            )

    _install_process_group()

    # ── Stack-dump watchdog (diagnostic) ────────────────────────
    # A daemon thread dumps all Python thread stacks every 30s to
    # ~/.digitorn/logs/stacks.log. Works even when the event loop
    # is frozen because it runs in its own OS thread.
    import faulthandler as _fh
    import threading as _th

    _stack_log = Path.home() / ".digitorn" / "logs" / "stacks.log"
    _stack_log.parent.mkdir(parents=True, exist_ok=True)

    def _stack_watchdog() -> None:
        while True:
            import time as _t
            _t.sleep(30)
            try:
                with open(_stack_log, "w", encoding="utf-8") as f:
                    f.write(f"=== stack dump at {_t.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                    _fh.dump_traceback(file=f, all_threads=True)
            except Exception:
                pass

    _wd = _th.Thread(target=_stack_watchdog, daemon=True, name="stack-watchdog")
    _wd.start()

    _run_first_time_setup_if_needed()

    settings = Settings.load(config_file=config)
    settings.server.host = host
    settings.server.port = port
    settings.server.workers = workers
    settings.server.reload = reload
    settings.logging.level = log_level  # type: ignore[assignment]
    if app_yaml is not None:
        settings.app.yaml_path = str(app_yaml)
    if sandbox is not None:
        settings.server.sandbox = sandbox
    override_settings(settings)

    # ── TLS setup ──
    # CLI flags override config file; config file is fallback.
    ssl_certfile = str(tls_cert) if tls_cert else getattr(settings.server, "tls_cert", None)
    ssl_keyfile = str(tls_key) if tls_key else getattr(settings.server, "tls_key", None)

    if ssl_certfile and not ssl_keyfile:
        console.print("[red]--tls-cert requires --tls-key[/red]")
        raise SystemExit(1)
    if ssl_keyfile and not ssl_certfile:
        console.print("[red]--tls-key requires --tls-cert[/red]")
        raise SystemExit(1)
    if ssl_certfile:
        if not Path(ssl_certfile).exists():
            console.print(f"[red]TLS cert not found: {ssl_certfile}[/red]")
            raise SystemExit(1)
        if not Path(ssl_keyfile).exists():
            console.print(f"[red]TLS key not found: {ssl_keyfile}[/red]")
            raise SystemExit(1)
        # Warn if key file is world-readable
        key_mode = Path(ssl_keyfile).stat().st_mode & 0o777
        if key_mode & 0o044:
            console.print(
                f"[yellow]WARNING: TLS key '{ssl_keyfile}' is readable by group/others "
                f"(mode {oct(key_mode)}). Consider: chmod 600 {ssl_keyfile}[/yellow]"
            )

    # Warn if auth enabled on non-localhost without TLS
    _auth_on = getattr(settings.server, "auth_enabled", True)
    if _auth_on and not ssl_certfile and host not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            "[yellow]WARNING: auth enabled on non-localhost without TLS. "
            "Tokens are sent in plaintext. Use --tls-cert/--tls-key for production.[/yellow]"
        )

    _protocol = "https" if ssl_certfile else "http"
    _banner(host, port, settings.database.url)

    # --- Start web client dev server if requested ---
    web_proc: _sp.Popen | None = None
    if web:
        web_dir = Path(__file__).resolve().parents[2] / "digitorn-web"
        if not (web_dir / "package.json").exists():
            console.print(f"[yellow]Web client not found at {web_dir}, skipping.[/yellow]")
        else:
            # Prefer bun if available, fallback to npx
            import shutil as _shutil
            vite_cmd = (
                ["bun", "run", "dev", "--port", str(web_port), "--host", host]
                if _shutil.which("bun")
                else ["npx", "vite", "--port", str(web_port), "--host", host]
            )
            console.print(f"[cyan]Starting web client on port {web_port}…[/cyan]")
            web_proc = _sp.Popen(
                vite_cmd,
                cwd=str(web_dir),
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
            console.print(f"[green]Web client → http://{host}:{web_port}[/green]\n")

    workers = settings.server.workers

    # Check if port is already in use before attempting to start
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _sock:
        _sock.settimeout(1)
        if _sock.connect_ex((host, port)) == 0:
            console.print(
                f"[bold red]Port {port} is already in use.[/bold red]\n"
                f"A daemon may already be running. Check with: [cyan]digitorn status[/cyan]\n"
                f"Stop it with: [cyan]digitorn stop[/cyan]"
            )
            raise SystemExit(1)

    # Build common uvicorn SSL kwargs (empty dict if no TLS)
    _ssl_kwargs: dict[str, Any] = {}
    if ssl_certfile:
        _ssl_kwargs["ssl_certfile"] = ssl_certfile
        _ssl_kwargs["ssl_keyfile"] = ssl_keyfile
        console.print(f"[green]TLS enabled → {_protocol}://{host}:{port}[/green]")

    # Re-assert the Windows event loop policy here, just before
    # uvicorn.run. Module-level install (see top of file) covers most
    # cases, but uvicorn's reload supervisor and multiprocessing-spawned
    # workers can still create their loop before our import-time hook
    # runs in the worker process. The ``loop=_UVICORN_LOOP`` kwarg
    # below routes uvicorn through our registered winloop factory
    # (Windows + winloop installed) or stdlib asyncio (every other
    # case). It does NOT rely on the global policy -- uvicorn 0.36+
    # ignores ``set_event_loop_policy`` and reads its loop from
    # ``LOOP_FACTORIES`` directly. The policy install is kept for
    # any code that runs outside uvicorn (persist_worker thread,
    # multiprocessing children).
    _install_windows_event_loop_policy()

    # Sansio WebSocket backend (uvicorn >= 0.35). The legacy
    # ``websockets`` backend is deprecated upstream and has the
    # ``write_frame_sync`` -> winloop ``UVStream is closing`` race
    # that floods logs with ``data transfer failed`` tracebacks on
    # client mid-close. The sansio backend decouples the protocol
    # state machine from the I/O layer so close handshakes never
    # try to push a frame onto a stream that's already in CLOSING
    # state. Same wire protocol, same client compat -- modernised
    # plumbing only.
    #
    # Defensive selection: if ``websockets-sansio`` isn't in this
    # uvicorn's protocol registry (pre-0.35), fall back to ``auto``.
    # Lets the daemon boot on stale Python environments / global
    # interpreters where ``pip install -U uvicorn`` hasn't been run
    # yet. The monkey-patch on ``WebSocketCommonProtocol.write_close_
    # frame`` (a few lines above this function) still catches the
    # legacy close-race silently on the fallback path.
    try:
        from uvicorn.config import WS_PROTOCOLS as _UV_WS_PROTOS
        if "websockets-sansio" in _UV_WS_PROTOS:
            _ws_backend = "websockets-sansio"
        else:
            _ws_backend = "auto"
            logger.warning(
                "uvicorn %s lacks websockets-sansio backend (need >=0.35); "
                "falling back to legacy 'websockets'. Run "
                "`pip install -U uvicorn` to get the modern API.",
                getattr(uvicorn, "__version__", "?"),
            )
    except Exception:
        _ws_backend = "auto"
    try:
        if reload:
            uvicorn.run(
                "digitorn.core.server:create_app",
                factory=True,
                host=host,
                port=port,
                log_level=log_level,
                reload=True,
                loop=_UVICORN_LOOP,
                ws=_ws_backend,
                **_ssl_kwargs,
            )
        elif workers > 1:
            uvicorn.run(
                "digitorn.core.server:create_app",
                factory=True,
                host=host,
                port=port,
                log_level=log_level,
                workers=workers,
                loop=_UVICORN_LOOP,
                ws=_ws_backend,
                **_ssl_kwargs,
            )
        else:
            app = create_app(settings=settings)
            uvicorn.run(
                app,
                host=host,
                port=port,
                log_level=log_level,
                loop=_UVICORN_LOOP,
                ws=_ws_backend,
                **_ssl_kwargs,
            )
    finally:
        if web_proc is not None:
            console.print("[dim]Stopping web client…[/dim]")
            web_proc.terminate()
            web_proc.wait(timeout=5)


@cli.command()
def supervise(
    host: str = typer.Option("127.0.0.1", help="Host to bind to."),
    port: int = typer.Option(8000, help="Port to bind to."),
    log_level: str = typer.Option("info", help="uvicorn log level."),
    ssl_certfile: str = typer.Option("", help="TLS cert (PEM)."),
    ssl_keyfile: str = typer.Option("", help="TLS key (PEM)."),
    min_uptime_s: float = typer.Option(
        30.0,
        help=(
            "Seconds of stable uptime required before the restart "
            "backoff resets to 1s. Below this, each crash doubles "
            "the wait until the cap."
        ),
    ),
    max_backoff_s: float = typer.Option(
        60.0, help="Upper bound on the restart backoff.",
    ),
    max_restarts_per_hour: int = typer.Option(
        20,
        help=(
            "Hard ceiling on restarts within a rolling hour. Beyond "
            "this, the supervisor gives up so a runaway crash loop "
            "doesn't spam the disk + logs."
        ),
    ),
) -> None:
    """Run the daemon under a restart-on-crash supervisor.

    Spawns ``digitorn start`` in a subprocess and watches it. When
    the child dies (any exit code, including segfaults / uncaught
    asyncio errors), the supervisor logs the cause and respawns
    after an exponential backoff. A stable run (uptime >=
    ``--min-uptime-s``) resets the backoff to 1s.

    Use this for local "always on" usage. Stop with Ctrl+C - the
    supervisor forwards the signal to the child and exits cleanly.
    For prod, prefer your platform's process supervisor (systemd,
    NSSM, Fly machines) - this one targets the dev workstation.
    """
    import os
    import signal
    import subprocess
    import time as _time

    cmd: list[str] = [
        sys.executable, "-m", "digitorn", "start",
        "--host", host,
        "--port", str(port),
        "--log-level", log_level,
    ]
    if ssl_certfile:
        cmd += ["--ssl-certfile", ssl_certfile]
    if ssl_keyfile:
        cmd += ["--ssl-keyfile", ssl_keyfile]

    backoff_s = 1.0
    restart_log: list[float] = []  # rolling timestamps
    stopping = False

    # Windows uses CREATE_NEW_PROCESS_GROUP so we can deliver a
    # CTRL_BREAK_EVENT to the child without also signalling
    # ourselves. POSIX defaults are fine without flags.
    popen_kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        )

    child: subprocess.Popen | None = None

    def _forward_stop(signum, frame):  # type: ignore[no-untyped-def]
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            try:
                if sys.platform == "win32":
                    child.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    child.send_signal(signal.SIGTERM)
            except Exception:
                pass

    signal.signal(signal.SIGINT, _forward_stop)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _forward_stop)
        except Exception:
            # Windows raises on SIGTERM register from a non-main thread.
            pass

    console.print(
        f"[bold]Digitorn supervisor[/bold] watching ``digitorn start "
        f"--port {port}``. Ctrl+C to stop."
    )

    while not stopping:
        # Rolling-hour rate limit: too many crashes in one hour and
        # we bail out instead of spinning forever.
        now = _time.time()
        restart_log[:] = [t for t in restart_log if now - t < 3600]
        if len(restart_log) >= max_restarts_per_hour:
            console.print(
                f"[bold red]supervisor giving up:[/bold red] "
                f"{len(restart_log)} restart(s) in the last hour "
                f"(cap = {max_restarts_per_hour}). Something is "
                f"crashing on every start - investigate the daemon "
                f"logs before re-running."
            )
            return

        spawn_time = _time.monotonic()
        try:
            child = subprocess.Popen(cmd, **popen_kwargs)
        except Exception as exc:
            console.print(f"[red]spawn failed: {exc}[/red]")
            return

        try:
            exit_code = child.wait()
        except KeyboardInterrupt:
            # Ctrl+C in the supervisor itself - propagate to child.
            stopping = True
            try:
                child.terminate()
                child.wait(timeout=10)
            except Exception:
                pass
            break

        if stopping:
            break

        uptime = _time.monotonic() - spawn_time
        restart_log.append(now)

        if uptime >= min_uptime_s:
            # The daemon was stable before crashing - reset backoff.
            console.print(
                f"[yellow]daemon exited[/yellow] code={exit_code} "
                f"after {uptime:.1f}s. Restart in 1s."
            )
            backoff_s = 1.0
        else:
            console.print(
                f"[yellow]daemon crashed fast[/yellow] code={exit_code} "
                f"after {uptime:.1f}s. Restart in {backoff_s:.0f}s."
            )

        try:
            _time.sleep(backoff_s)
        except KeyboardInterrupt:
            stopping = True
            break

        backoff_s = min(backoff_s * 2, max_backoff_s)

    console.print("[dim]supervisor stopped[/dim]")


@cli.command()
def stop(
    host: str = typer.Option("127.0.0.1", help="Daemon host."),
    port: int = typer.Option(8000, help="Daemon port."),
) -> None:
    """Stop the Digitorn daemon."""
    import httpx
    import signal
    import os

    url = f"http://{host}:{port}/health"
    try:
        httpx.get(url, timeout=3.0)
    except Exception:
        console.print("[dim]Daemon is not running.[/dim]")
        return

    try:
        resp = httpx.post(f"http://{host}:{port}/shutdown", timeout=5.0)
        if resp.status_code == 200:
            console.print("[green]Daemon stopped.[/green]")
            return
    except Exception:
        logger.debug("graceful daemon shutdown request failed", exc_info=True)

    import subprocess
    import sys

    if sys.platform == "win32":
        # Windows: find and kill the daemon process by port
        try:
            # Find PID listening on the port
            netstat = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True,
            )
            pid = None
            for line in netstat.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    break
            if pid:
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                console.print(f"[green]Daemon stopped (PID {pid}).[/green]")
            else:
                console.print("[yellow]Could not find daemon process.[/yellow]")
        except Exception as exc:
            console.print(f"[yellow]Failed to stop daemon: {exc}[/yellow]")
    else:
        # Linux / macOS: try pkill first, then lsof fallback
        result = subprocess.run(
            ["pkill", "-f", "digitorn.*start"],
            capture_output=True,
        )
        subprocess.run(["pkill", "-f", "vite.*digitorn-web"], capture_output=True)
        if result.returncode == 0:
            console.print("[green]Daemon stopped.[/green]")
        else:
            # Fallback: find PID by port (works on macOS + Linux)
            try:
                lsof = subprocess.run(
                    ["lsof", "-ti", f":{port}"], capture_output=True, text=True,
                )
                pids = lsof.stdout.strip().split()
                if pids:
                    for pid in pids:
                        subprocess.run(["kill", "-9", pid], capture_output=True)
                    console.print(f"[green]Daemon stopped (PID {', '.join(pids)}).[/green]")
                else:
                    console.print("[yellow]Daemon may still be running. Use: kill $(lsof -ti :{port})[/yellow]")
            except FileNotFoundError:
                console.print(f"[yellow]Daemon may still be running. Use: kill $(lsof -ti :{port})[/yellow]")


@cli.command()
def status(
    host: str = typer.Option("127.0.0.1", help="Daemon host."),
    port: int = typer.Option(8000, help="Daemon port."),
) -> None:
    """Check if the daemon is running."""
    import httpx

    url = f"http://{host}:{port}/health"
    try:
        resp = httpx.get(url, timeout=5.0)
        data = resp.json()

        table = Table(title="Digitorn Status", border_style="green")
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="green")
        for k, v in data.items():
            table.add_row(str(k), str(v))
        console.print(table)

    except Exception:
        console.print(f"[bold red]Daemon not reachable at {url}[/bold red]")
        raise typer.Exit(1)


@cli.command()
def version() -> None:
    """Show Digitorn version."""
    console.print(f"[bold cyan]Digitorn[/bold cyan] v{__version__}")


@cli.command("windows-setup")
def windows_setup_cmd(
    skip_winloop: bool = typer.Option(
        False, "--skip-winloop", help="Don't pip install winloop."
    ),
    skip_defender: bool = typer.Option(
        False, "--skip-defender", help="Don't touch Defender exclusions."
    ),
) -> None:
    """One-shot setup for local Windows dev.

    Installs ``winloop`` (libuv-based event loop, avoids the
    ProactorEventLoop slow-socket / slow-WSASend stalls) and adds
    Defender exclusions for the Python interpreter and ``~/.digitorn``
    so AV stops scanning every socket() / file read. Requires admin
    once for the Defender step (UAC prompt). Idempotent.

    No-op on Linux/macOS -- prints a friendly message and exits 0.
    """
    from digitorn.core import windows_setup as _winsetup

    if not _winsetup.is_windows():
        console.print(
            "[green]Not on Windows -- nothing to do.[/green] "
            "On Linux/macOS the default asyncio loop already uses "
            "non-blocking sockets; install [cyan]uvloop[/cyan] in your "
            "Python env for max perf in production."
        )
        raise typer.Exit(0)

    # ── winloop install (no admin needed) ───────────────────────
    if not skip_winloop:
        console.print("[cyan]Installing winloop ...[/cyan]")
        if _winsetup.ensure_winloop_installed():
            console.print("[green]winloop ready.[/green]")
        else:
            console.print(
                "[yellow]winloop install failed; daemon will fall back "
                "to ProactorEventLoop. See logs for details.[/yellow]"
            )

    # ── Defender exclusions (admin required) ────────────────────
    if skip_defender:
        console.print("[dim]Skipping Defender exclusions per --skip-defender.[/dim]")
        return

    if _winsetup.check_exclusions_present():
        console.print(
            "[green]Defender exclusions already present -- nothing to do.[/green]"
        )
        return

    if not _winsetup.is_admin():
        console.print(
            "[yellow]Defender exclusions need admin. Re-launching with "
            "UAC prompt ...[/yellow]"
        )
        try:
            _winsetup.relaunch_as_admin()
        except PermissionError as exc:
            console.print(f"[red]Elevation refused: {exc}[/red]")
            console.print(
                "[dim]Re-run `digitorn windows-setup` from an "
                "admin PowerShell to finish setup.[/dim]"
            )
            raise typer.Exit(2)
        return  # elevated child takes over

    try:
        summary = _winsetup.install_exclusions()
    except Exception as exc:
        console.print(f"[red]Defender exclusion install failed: {exc}[/red]")
        raise typer.Exit(2)

    if summary["added_processes"]:
        console.print(
            "[green]Added process exclusions:[/green] "
            + ", ".join(summary["added_processes"])
        )
    if summary["added_paths"]:
        console.print(
            "[green]Added path exclusions:[/green] "
            + ", ".join(summary["added_paths"])
        )
    if summary["skipped"]:
        console.print(
            f"[dim]Skipped (already present): {len(summary['skipped'])}[/dim]"
        )
    console.print(
        "\n[bold green]Windows setup complete.[/bold green] "
        "Stalls should disappear after the next `digitorn start`."
    )


def main() -> None:
    """CLI entry point (called by pyproject.toml [tool.poetry.scripts])."""
    # Load .env from the current directory (project root)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    cli()


if __name__ == "__main__":
    main()
