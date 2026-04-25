"""Digitorn — FastAPI application factory and CLI entry point.

CLI commands:
    digitorn start          Start the daemon
    digitorn status         Check if the daemon is running
    digitorn version        Show version info
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, AsyncGenerator

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
    # auth_service is initialized later in lifespan; store a ref for Socket.IO
    _auth_holder: dict[str, Any] = {}

    class _LazyAuth:
        """Proxy so Socket.IO can use auth_service initialized after sio creation.

        Evaluates as falsy (``bool(self) == False``) when the underlying
        service is None (auth disabled), so that
        ``create_socketio_server``'s ``if auth_service is None`` /
        ``if not auth_service`` check correctly bypasses JWT validation.
        """
        def __bool__(self) -> bool:
            return _auth_holder.get("service") is not None

        def verify_access_token(self, token: str):
            svc = _auth_holder.get("service")
            if svc is None:
                raise ValueError("Auth not initialized")
            return svc.verify_access_token(token)

    # Manager holder — initialized during lifespan, used by Socket.IO for session validation
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

    # Session event bus — shared by AppManager (publish) and Socket.IO
    # handlers (replay). Created BEFORE sio so the handlers can reach
    # the replay buffer; the sio reference is injected after construction.
    session_event_bus = SocketIOBus(sio=None, buffer=EventBuffer())

    # If kv_backend is Redis, wire it as Socket.IO's pub/sub adapter
    # so events emitted by one worker reach clients on ALL workers.
    # Always include the daemon's own origin so preview iframes (served from
    # the same host) can connect to Socket.IO without CORS errors.
    _daemon_origin = f"http://{settings.server.host}:{settings.server.port}"
    _effective_cors = list(cors_origins)
    if _daemon_origin not in _effective_cors:
        _effective_cors.append(_daemon_origin)

    _kv_url = getattr(settings.server, "kv_backend", None)
    sio = create_socketio_server(
        cors_allowed_origins=_effective_cors,
        auth_service=_LazyAuth(),
        manager=_LazyManager(),
        session_bus=session_event_bus,
        redis_url=_kv_url if _kv_url and str(_kv_url).startswith("redis") else None,
    )
    session_event_bus._sio = sio  # wire the emitter now that sio exists

    # Wire the session bus into the module event bus so every module
    # event (including ``action_failed`` errors) gets the same
    # ``{type, seq, kind, app_id, session_id, payload, ts}`` envelope
    # as session-level events. Without this, the frontend's "sort by
    # seq" timeline skipped module errors entirely.
    socketio_bus = SocketIOEventBus(sio, session_bus=session_event_bus)
    log_bus = LogEventBus()
    event_bus = FanoutEventBus([log_bus, socketio_bus])

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        from digitorn.core.database import close_db, init_db
        from digitorn.core.loader import load_modules
        from digitorn.core.state_store import JsonStateStore
        from digitorn.core.watcher.service import SourceWatcherService
        from digitorn.modules.lifecycle import ModuleLifecycleManager
        from digitorn.modules.registry import ModuleRegistry
        from digitorn.modules.service_bus import ServiceBus

        # Schema management is handled directly by init_db() via
        # ``Base.metadata.create_all`` + the column-level migrations in
        # database.py::_migrate_missing_columns. The alembic env lives
        # under core/migrations/ for the day we add real versioned
        # migrations, but importing it at boot is a no-op (0 versions
        # checked in) and used to emit a spurious "db_migrations_skipped"
        # warning because alembic.context isn't initialised outside the
        # alembic CLI. Silent on purpose.
        engine = await init_db(settings)
        app.state.engine = engine

        registry = ModuleRegistry()
        results = load_modules(
            registry,
            enabled=settings.modules.enabled or None,
            disabled=settings.modules.disabled or None,
            load_all=settings.modules.load_all,
        )
        app.state.registry = registry
        app.state.module_load_results = results

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

        sidecar_pool = DaemonSidecarPool()
        await sidecar_pool.start()
        app.state.sidecar_pool = sidecar_pool

        # ── Node.js runtime — detect or auto-install ─────────────────
        # Used by MCP stdio servers (Node-based), PreviewManager (dev
        # servers), and package install hooks. Non-fatal: if the download
        # fails, the daemon keeps running but Node-dependent features
        # will surface the error at the point of use.
        from digitorn.core.runtime.node_runtime import get_node_runtime
        node_runtime = get_node_runtime()
        node_runtime.set_auto_install(settings.server.node_auto_install)
        try:
            await node_runtime.ensure_installed()
            logger.info(
                "node_runtime_ready version=%s source=%s",
                node_runtime.version,
                node_runtime.info.source if node_runtime.info else "?",
            )
        except Exception as exc:
            logger.warning(
                "node_runtime_unavailable: %s — Node-dependent features "
                "will be disabled until Node is installed", exc,
            )
        app.state.node_runtime = node_runtime

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

        await lifecycle.start_all()

        from digitorn.core.mcp_pool import DaemonMCPPool
        from digitorn.core.database import _session_factory as db_session_factory

        mcp_pool = DaemonMCPPool()
        try:
            await mcp_pool.start(db_session_factory)
        except Exception as exc:
            logger.warning("mcp_pool_start_failed: %s", exc, exc_info=True)
        app.state.mcp_pool = mcp_pool

        from digitorn.core.app.runtime import AppRuntimeStore

        runtime_store = AppRuntimeStore(registry)
        app.state.runtime_store = runtime_store

        from digitorn.core.app.manager import AppManager

        app_manager = AppManager(
            registry,
            service_bus,
            runtime_store,
            stop_on_error=settings.app.stop_on_error,
            session_backend_url=settings.server.kv_backend,
            event_bus=session_event_bus,
        )
        app_manager._settings = settings
        app_manager._daemon_mcp_pool = mcp_pool
        mcp_pool.set_on_event(app_manager._on_mcp_event)
        app.state.app_manager = app_manager

        # ── Credential store — foundation of the universal secrets system ──
        # The master key is auto-generated on the first boot at
        # ~/.digitorn/master.key. If DIGITORN_MASTER_KEY is set, that
        # takes precedence (Docker / k8s deployment model).
        try:
            from digitorn.core.credentials import (
                CredentialStore,
                Cipher,
                load_or_create_master_key,
            )
            from digitorn.core.credentials.bootstrap import (
                import_env_vars_into_store,
            )
            from digitorn.core.database import get_session_factory

            master_key = load_or_create_master_key()
            cipher = Cipher(master_key)
            credential_store = CredentialStore(get_session_factory(), cipher)
            app.state.credential_store = credential_store
            app_manager._credential_store = credential_store

            # Inbox store + producer — persistent cross-device notification
            # inbox. The producer subscribes to the bus's per-user fan-out
            # and materializes events into rows via the store. The API
            # routes in core/api/user.py read from the same store.
            try:
                from digitorn.core.usage import QuotaStore, UsageStore
                usage_store = UsageStore(get_session_factory())
                quota_store = QuotaStore(
                    get_session_factory(), usage_store=usage_store,
                )
                app.state.usage_store = usage_store
                app.state.quota_store = quota_store
                app_manager._usage_store = usage_store
                app_manager._quota_store = quota_store
                logger.info("usage_subsystem_started")
            except Exception as exc:
                logger.warning(
                    "usage init failed: %s", exc, exc_info=True,
                )
                app.state.usage_store = None
                app.state.quota_store = None

            try:
                from digitorn.core.inbox import (
                    InboxStore,
                    InboxProducer,
                    NotificationDispatcher,
                )
                inbox_store = InboxStore(get_session_factory())
                app.state.inbox_store = inbox_store

                # Notification dispatcher — gracefully degrades if
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
                await inbox_producer.start()
                app.state.inbox_producer = inbox_producer
                logger.info("inbox_subsystem_started")
            except Exception as exc:
                logger.warning("inbox init failed: %s", exc, exc_info=True)
                app.state.inbox_store = None
                app.state.inbox_producer = None
                app.state.notification_dispatcher = None

            import_summary = await import_env_vars_into_store(credential_store)
            logger.info(
                "credential bootstrap: imported=%d skipped=%d not_in_env=%d",
                len(import_summary["imported"]),
                len(import_summary["skipped_already_present"]),
                import_summary["not_in_env"],
            )

            # One-shot migration: set owner_type + create grants for
            # legacy 4-scope credential rows. Safe to run repeatedly.
            try:
                migrated = await credential_store.migrate_legacy_scopes()
                if any(migrated.values()):
                    logger.info(
                        "credential migration: user_rows=%d system_rows=%d grants_created=%d",
                        migrated["user_rows"],
                        migrated["system_rows"],
                        migrated["grants_created"],
                    )
            except Exception as exc:
                logger.warning("credential migration skipped: %s", exc)

            # Load the OAuth provider registry — writes a template at
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
                "credential store init FAILED: %s — the daemon will "
                "fall back to the legacy secret_store and credentials "
                "routes will be unavailable",
                exc,
                exc_info=True,
            )
            app.state.credential_store = None
        _manager_holder["manager"] = app_manager

        # ── App loading — MUST NEVER crash the daemon ─────────────────────
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
        # (``_deploy_from_bundle``) needs to resolve each package's
        # on-disk install_dir so features like PreviewManager can
        # locate relative paths (``preview.cwd=./web``). Without the
        # registry wired in time, ``_resolve_install_dir`` returns
        # None and bundle_dir falls back to ``Path.cwd()``, which
        # happens to be the daemon's working directory — usually
        # wrong and the source of the most confusing preview bugs.
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

        try:
            reloaded = await app_manager.reload_from_db()
        except BaseException as exc:
            logger.error("app_reload_from_db_failed: %s", exc, exc_info=True)

        # ── AppPackages bootstrap — install / upgrade built-in packages ──
        # Runs after reload so reload sees the existing registry rows,
        # and bootstrap can upgrade any stale builtins.
        #
        # Set DIGITORN_SKIP_BUILTINS=1 to skip entirely — useful for
        # isolated test daemons that want a bare registry.
        if os.environ.get("DIGITORN_SKIP_BUILTINS"):
            logger.info("bootstrap_builtins skipped (DIGITORN_SKIP_BUILTINS set)")
        elif package_registry is not None:
            async def _bootstrap_deploy(yaml_path, package_id):
                return await app_manager.deploy(yaml_path, force=True)

            async def _bootstrap_pre_upgrade(package_id):
                """Stop the package's preview dev server (Vite/Next) so its
                file handles in node_modules don't block the rename swap."""
                try:
                    deployed = app_manager.get(package_id)
                except Exception:
                    deployed = None
                if deployed is None:
                    return
                pm = getattr(deployed, "preview_manager", None)
                if pm is None or not getattr(pm, "enabled", False):
                    return
                try:
                    await pm.stop()
                    logger.info(
                        "bootstrap pre-upgrade: stopped preview manager for %s",
                        package_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "bootstrap pre-upgrade: pm.stop failed for %s: %s",
                        package_id, exc,
                    )

            async def _run_bootstrap_in_background():
                try:
                    await asyncio.wait_for(
                        bootstrap_builtins(
                            registry=package_registry,
                            on_deploy=_bootstrap_deploy,
                            on_pre_upgrade=_bootstrap_pre_upgrade,
                        ),
                        timeout=600.0,
                    )
                    logger.info("AppPackages bootstrap completed in background")
                except asyncio.TimeoutError:
                    logger.error(
                        "AppPackages bootstrap exceeded 10-min budget — "
                        "broken builtins are marked accordingly, daemon unaffected",
                    )
                except Exception as exc:
                    logger.error(
                        "AppPackages bootstrap (background) failed: %s",
                        exc, exc_info=True,
                    )

            asyncio.create_task(_run_bootstrap_in_background())
            logger.info(
                "AppPackages bootstrap dispatched to background task — "
                "daemon HTTP is starting NOW without waiting"
            )
        else:
            logger.warning(
                "package_registry not wired — skipping AppPackages bootstrap"
            )

        # ── Legacy built-in apps — kept for backwards compat ──
        # The old core/builtin_apps/ dir is now empty after v2
        # migration; this call is a no-op but stays for one release
        # as a safety net in case the new bootstrap had a partial
        # failure.
        await _deploy_builtin_apps(app_manager)

        if app.state.auth_service is not None:
            try:
                auth_config = getattr(settings.server, "auth_config", {})
                await app.state.auth_service.start(auth_config)
                logger.info("auth_service_started")
            except BaseException as exc:
                logger.warning("auth_service_start_failed error=%s", exc)

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

        # Rehydrate the message queue — reset rows left in `running` state
        # from a previous crash back to `queued` so they get picked up.
        try:
            from digitorn.core.app.message_queue import rehydrate_on_boot
            _recovered = await rehydrate_on_boot()
            if _recovered:
                logger.info(
                    "message_queue rehydrated %d stuck rows on boot",
                    _recovered,
                )
        except Exception as exc:
            logger.warning("message_queue rehydrate failed on boot: %s", exc)

        app.state._active_requests = 0
        app.state._active_agent_turns = 0
        app.state._shutting_down = False

        # Active event-loop watchdog — detects stalls > 2s and dumps
        # stacks at the moment of the stall. Also sets
        # ``loop.slow_callback_duration = 0.1`` so any callback > 100ms
        # is logged as a WARNING.
        from digitorn.core.runtime.loop_watchdog import install as _install_wd
        app.state.loop_watchdog = _install_wd()

        # Periodic session eviction — prevent memory leak from expired sessions
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
            piled up forever whenever agent_turn crashed — they broke
            the dashboard success_rate metric and the cron dashboard's
            running counter. Runs once a minute; the ``older_than``
            window exceeds any realistic turn timeout so a truly
            running activation is never stolen.

            BUG-107: also emit a ``background_app_rot`` event when any
            app's recent activations are 100% failures — silent rot on
            background-only apps (no user sitting in a session to
            notice) used to go unreported until someone opened the
            dashboard and saw ``success_rate: 0.0``.
            """
            from digitorn.core.app.activation_store import ActivationStore
            from digitorn.core.database import get_session_factory
            while not app.state._shutting_down:
                await asyncio.sleep(60)
                try:
                    store = ActivationStore(get_session_factory())
                    n = await store.sweep_stuck_running(older_than_seconds=600)
                    if n:
                        logger.info("activation_sweeper marked_failed=%d", n)
                except Exception as exc:
                    logger.debug("activation_sweeper_iteration_failed: %s", exc)
                # BUG-107: silent-rot detector. Scan each background
                # app's recent activation window and log a loud WARNING
                # the moment success_rate drops to 0 over a meaningful
                # sample — that's the signal ops should act on (bad
                # credentials, provider outage, misconfigured YAML).
                try:
                    mgr = getattr(app.state, "app_manager", None)
                    if mgr is None:
                        continue
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
                            stats = await store.stats(app_id)
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
                                "failed=%d success_rate=0.0 — every "
                                "recent activation failed, investigate "
                                "credentials / provider / YAML",
                                app_id, total, failed,
                            )
                except Exception as exc:
                    logger.debug("rot_detector_iteration_failed: %s", exc)
        _activation_sweeper_task = asyncio.create_task(_activation_sweeper())

        # ── Worker Pool — dedicated thread pools for agent turns + I/O ──
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

        yield

        try:
            await app_manager.stop_notification_poller()
        except Exception as exc:
            logger.warning("notification_poller_stop_failed: %s", exc)

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

        if app.state.auth_service is not None:
            await app.state.auth_service.stop()

        inbox_producer = getattr(app.state, "inbox_producer", None)
        if inbox_producer is not None:
            try:
                await inbox_producer.stop()
            except Exception as exc:
                logger.warning("inbox_producer stop failed: %s", exc)

        for app_id in list(app_manager._deployed.keys()):
            await app_manager.undeploy(app_id)
        await mcp_pool.stop()
        await sidecar_pool.stop()
        await lifecycle.stop_all()
        await watcher_service.shutdown()
        await close_db()

        # Worker pool shutdown LAST — modules may use it during cleanup
        await shutdown_worker_pool()

        logger.info("shutdown_complete")

    # Disable OpenAPI docs in production (auth enabled) — the full API schema
    # is an attacker's best friend.  In dev mode (auth disabled) docs are served.
    _auth_enabled = getattr(settings.server, "auth_enabled", True)
    _expose_docs = not _auth_enabled or getattr(settings.server, "expose_docs", False)

    app = FastAPI(
        title="Digitorn",
        description="Modular agent OS — bridging AI to operating systems, applications, and devices.",
        version=__version__,
        docs_url="/docs" if _expose_docs else None,
        redoc_url="/redoc" if _expose_docs else None,
        openapi_url="/openapi.json" if _expose_docs else None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        # Also allow any localhost port (Flutter web, dev servers, etc.)
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

    # ── Combined middleware (security + context + shutdown + metrics) ───
    # Merging 3 lightweight middlewares into one reduces ASGI middleware
    # stack traversals per request from 4 to 2 (this + rate_limit).

    from digitorn.core.metrics import metrics as _metrics
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
    # generous (16 MiB — enough room for a modest image upload payload)
    # but rejects the 50-MiB-text-message DoS that stalled the event
    # loop ~60s in production testing.
    _MAX_BODY_BYTES = 16 * 1024 * 1024
    # Upload endpoints legitimately move more bytes (images, archives,
    # avatar blobs). Keep the cap narrow for the message path where
    # the DoS was observed; the upload routes stay on their own limits.
    _MAX_BODY_BY_PATH_PREFIX = {
        "/messages": 2 * 1024 * 1024,  # JSON message body, 2 MiB cap
    }

    @app.middleware("http")
    async def combined_middleware(request: Request, call_next):
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

        # ── Phase 4: Security headers (on the way out) ────────────
        response.headers["x-request-id"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        # Preview iframes (Vite/HMR dev servers) need inline scripts,
        # eval, and to be embeddable inside the Flutter admin panel.
        # The strict CSP/X-Frame-Options stays on every other route.
        is_preview = "/preview-server/proxy" in path or "/preview/" in path
        if is_preview:
            allowed_ancestors = " ".join([
                "'self'",
                "http://localhost:*",
                "http://127.0.0.1:*",
                "https://localhost:*",
                "https://127.0.0.1:*",
            ])
            response.headers["Content-Security-Policy"] = (
                "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "connect-src 'self' ws://localhost:* ws://127.0.0.1:* "
                "wss://localhost:* wss://127.0.0.1:* "
                "http://localhost:* http://127.0.0.1:*; "
                "worker-src 'self' blob:; "
                f"frame-ancestors {allowed_ancestors}"
            )
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

        # 4. Catch-all: any OTHER authenticated /api/* GET/POST gets
        # a generic per-user bucket. Before this block the middleware
        # only rate-limited messages/run/auth/admin; an attacker (or a
        # misbehaving client) could hit `/api/apps` 1700+ RPM without
        # any pushback. BUG-047 confirmed 200×/6.9s all succeeding.
        if rl_key is None and path.startswith("/api/"):
            rl_key = "__api_generic__"

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

    app.state.settings = settings
    app.state.sio = sio
    app.state.session_bus = session_event_bus
    app.state.event_bus = event_bus
    app.state.rate_limiter = rate_limiter

    auth_enabled = getattr(settings.server, "auth_enabled", True)
    auth_service = None

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

    if auth_enabled:
        try:
            from digitorn.core.auth.jwt import JWTService
            from digitorn.core.auth.service import AuthService
            from digitorn.core.auth.middleware import AuthMiddleware

            jwt_secret = getattr(settings.server, "jwt_secret", None)
            jwt_service = JWTService(
                secret_key=jwt_secret,
                access_ttl=getattr(settings.auth, "access_token_ttl", 900),
                refresh_ttl=getattr(settings.auth, "refresh_token_ttl", 604800),
            )
            auth_service = AuthService(jwt_service)
            app.add_middleware(AuthMiddleware, auth_service=auth_service, enabled=True)
            logger.info("auth_enabled providers=%s", getattr(settings.server, "auth_providers", ["local"]))
        except ImportError as exc:
            logger.warning("auth_disabled reason=%s", exc)
    else:
        from digitorn.core.auth.middleware import AuthMiddleware
        app.add_middleware(AuthMiddleware, auth_service=None, enabled=False)

    app.state.auth_service = auth_service
    _auth_holder["service"] = auth_service

    from digitorn.core.api.apps import router as apps_router
    from digitorn.core.api.auth import router as auth_router
    from digitorn.core.api.builder import router as builder_router
    from digitorn.core.api.config import router as config_router
    from digitorn.core.api.credentials import (
        oauth_callback_router,
        router as credentials_router,
    )
    from digitorn.core.api.discovery import router as discovery_router
    from digitorn.core.api.mcp import router as mcp_router
    from digitorn.core.api.modules import router as modules_router
    from digitorn.core.api.packages import router as packages_router
    from digitorn.core.api.requires import router as requires_router
    from digitorn.core.api.security import router as security_router
    from digitorn.core.api.transcribe import router as transcribe_router
    from digitorn.core.api.ui import router as ui_router
    from digitorn.core.api.user import (
        admin_router as user_admin_router,
        router as user_router,
    )

    app.include_router(auth_router)
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
    app.include_router(packages_router)
    app.include_router(user_router)
    app.include_router(user_admin_router)
    app.include_router(transcribe_router)
    app.include_router(ui_router)

    # --- Global exception handler — structured JSON for unhandled errors ---
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
        # "Internal server error" — that's what a recent
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
        }

        # System metrics (best-effort — psutil may not be installed)
        try:
            import psutil
            proc = psutil.Process()
            result["system"] = {
                "cpu_percent": proc.cpu_percent(interval=0),
                "memory_mb": round(proc.memory_info().rss / 1024 / 1024, 1),
                "threads": proc.num_threads(),
                "open_files": len(proc.open_files()),
                "connections": len(proc.connections()),
            }
        except (ImportError, Exception):
            result["system"] = {"available": False}

        # Event loop lag — measures how responsive the loop is
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
    async def healthz() -> dict:
        return {"status": "alive"}

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

    # Conventional Prometheus endpoint location — many scrapers look for
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

    asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)

    return asgi_app


cli = typer.Typer(
    name="digitorn",
    help="Digitorn — Modular agent OS.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

# Server-side commands only — client commands are in the digitorn_cli package
from digitorn.core.cli.doctor import doctor as doctor_command  # noqa: E402
from digitorn.core.cli.init import init as init_command  # noqa: E402
from digitorn.core.cli.app import app_cli  # noqa: E402
from digitorn.core.cli.modules import modules_cli  # noqa: E402
from digitorn.core.cli.package import package_cli  # noqa: E402
from digitorn.core.cli.requires import requires_cli  # noqa: E402
from digitorn.core.cli.secret import secret_cli  # noqa: E402
from digitorn.core.cli.credentials import credentials_cli  # noqa: E402
from digitorn.core.cli.mcp_cli import mcp_cli  # noqa: E402
from digitorn.core.cli.middleware_cli import middleware_cli  # noqa: E402
from digitorn.core.cli.dev import dev_cli  # noqa: E402

cli.command(name="init")(init_command)
cli.command(name="doctor")(doctor_command)
cli.add_typer(modules_cli)
cli.add_typer(requires_cli)
cli.add_typer(app_cli)
cli.add_typer(secret_cli)
cli.add_typer(credentials_cli)
cli.add_typer(mcp_cli)
cli.add_typer(middleware_cli)
cli.add_typer(package_cli)
cli.add_typer(dev_cli)


_DEFAULT_DAEMON = "http://127.0.0.1:8000"


def _run_first_time_setup_if_needed() -> None:
    """Interactive first-time setup if no config file exists."""
    config_path = Path.home() / ".digitorn" / "config.yaml"
    if config_path.exists():
        return

    import yaml as _yaml

    console.print()
    console.print("[bold cyan]Digitorn[/bold cyan] — First run setup\n")

    console.print("  Database backend:")
    console.print("    [bold]1[/bold] SQLite (local, zero config) — recommended for development")
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
                    console.print(f"[bold red]INVALID[/bold red] — database name must be alphanumeric: {pg_db!r}")
                    raise typer.Exit(1)
                asyncio.get_event_loop().run_until_complete(
                    sys_conn.execute(f'CREATE DATABASE "{pg_db}"')
                )
                asyncio.get_event_loop().run_until_complete(sys_conn.close())
                console.print("[bold green]OK[/bold green]")
            except Exception as create_exc:
                console.print(f"[bold red]FAILED[/bold red] — {create_exc}")
                console.print(f"  Create the database manually: CREATE DATABASE {pg_db};")
                console.print("  Then run [cyan]digitorn start[/cyan] again.")
                raise typer.Exit(1)
        except Exception as exc:
            console.print(f"[bold red]FAILED[/bold red] — {exc}")
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
    before deployment — nothing is hardcoded.
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

            # Inject default model config into the YAML before deploying
            import yaml as _yaml
            raw = _yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
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
    import subprocess as _sp
    from digitorn.core.config import Settings, override_settings
    from digitorn.core.process_group import install as _install_process_group

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

    try:
        if reload:
            uvicorn.run(
                "digitorn.core.server:create_app",
                factory=True,
                host=host,
                port=port,
                log_level=log_level,
                reload=True,
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
                **_ssl_kwargs,
            )
        else:
            app = create_app(settings=settings)
            uvicorn.run(
                app,
                host=host,
                port=port,
                log_level=log_level,
                **_ssl_kwargs,
            )
    finally:
        if web_proc is not None:
            console.print("[dim]Stopping web client…[/dim]")
            web_proc.terminate()
            web_proc.wait(timeout=5)


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
