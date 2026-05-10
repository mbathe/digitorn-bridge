"""FastAPI application for the digitorn LLM gateway.

Routes:

* `POST /v1/chat/completions`  - OpenAI-compatible. `stream=true` for SSE.
* `GET  /v1/models`            - list aliases visible to the caller.
* `GET  /v1/quota/me`          - caller's current usage + limits.
* `GET  /admin/quota/plans`    - admin: list plans (+ CRUD).
* `GET  /admin/quota/users/{}` - admin: user usage + assign/reset.
* `GET  /healthz`              - liveness probe.

Auth: `Authorization: Bearer <digitorn-jwt>` on every `/v1/*` and
`/admin/*` route. Verification is offline against a JWKS cached at boot.

Quota: pre-call `is_blocked` (O(1) memory dict), post-call `record`
(in-memory increment, lazy Postgres flush). The user is never made to
wait on the quota machinery.

LLM dispatch: `llm_call.dispatch` and `llm_call.dispatch_stream`.
For models flagged `provider: custom` in the catalogue, dispatch
delegates to `custom_router.get_router()` instead of LiteLLM.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

# orjson is 3-10x faster than stdlib for the JSON-heavy hot path
# (request body parse + streaming chunk encode). Required dep, but
# guard the import so a partial install still boots with a slow path.
try:
    import orjson as _orjson

    def _loads(b: bytes) -> Any:
        return _orjson.loads(b)

    def _dumps(o: Any) -> bytes:
        return _orjson.dumps(o)
except ImportError:  # pragma: no cover
    import json as _json

    def _loads(b: bytes) -> Any:
        return _json.loads(b.decode("utf-8"))

    def _dumps(o: Any) -> bytes:
        return _json.dumps(o).encode("utf-8")

from digitorn_gateway.auth import GatewayPrincipal, init_jwks, require_principal
from digitorn_gateway.config import get_settings
from digitorn_gateway.custom_router import CustomProviderNotImplemented
from digitorn_gateway.db import dispose_engine, get_session_factory, init_engine as init_db
from digitorn_gateway.llm_call import dispatch, dispatch_stream
from digitorn_gateway.models import get_catalog, load_catalog, set_catalog
from digitorn_gateway.plans import (
    get_registry as get_plan_registry,
    init_registry as init_plan_registry,
    seed_plans_from_yaml,
)
from digitorn_gateway.quota import UsageRecord, get_engine, init_engine
from digitorn_gateway.admin_config_routes import router as admin_config_router
from digitorn_gateway.admin_writable_routes import router as admin_writable_router
from digitorn_gateway.oauth_login_routes import router as oauth_login_router
from digitorn_gateway.bootstrap_seed import seed_if_empty
from digitorn_gateway.config_cache import (
    get_cache as get_config_cache,
    start_refresh_loop as start_config_cache_refresh,
    stop_refresh_loop as stop_config_cache_refresh,
)
from digitorn_gateway.oauth_refresher import (
    start_refresh_loop as start_oauth_refresh,
    stop_refresh_loop as stop_oauth_refresh,
)
from digitorn_gateway.route_prober import (
    start_probe_loop as start_route_probe,
    stop_probe_loop as stop_route_probe,
)
from digitorn_gateway.dashboard_routes import router as dashboard_router
from digitorn_gateway.quota_routes import router as quota_router
from digitorn_gateway.usage_routes import router as usage_router

logger = logging.getLogger(__name__)


# ── Lifespan ───────────────────────────────────────────────────────


async def _periodic_jwks_refresh(jwks: Any, interval_s: int) -> None:
    """Refresh the JWKS cache in the background."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            await jwks.fetch()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("jwks_periodic_refresh_failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # Resilience hardening: install a global asyncio exception handler
    # so any orphan ``Task exception was never retrieved`` ends up in
    # OUR logger instead of stderr. The default Python behaviour is
    # to print a traceback at GC time, which (a) leaks noise into
    # logs and (b) skips our structured log format. Catching them
    # here lets us route them to the same monitoring stack as the
    # rest of the gateway, AND gives us a single place to alert from.
    try:
        loop = asyncio.get_running_loop()

        def _global_exc_handler(_loop, context):
            exc = context.get("exception")
            msg = context.get("message") or "asyncio task error"
            task = context.get("task")
            if isinstance(exc, asyncio.CancelledError):
                return
            logger.warning(
                "asyncio_orphan_task_error msg=%s task=%s exc=%r",
                msg, getattr(task, "get_name", lambda: "?")(), exc,
            )
        loop.set_exception_handler(_global_exc_handler)
    except Exception as _exc:
        logger.debug("asyncio_global_handler_install_failed: %s", _exc)

    # Eager-import LiteLLM at boot so the FIRST /v1/chat/completions
    # request doesn't pay the ~1-2s cold-start penalty (LiteLLM loads
    # ~100 provider modules + a Pydantic model registry on first use).
    # Also silence its default success/failure callbacks - they make
    # synchronous logging+stats calls on every chunk we don't need.
    try:
        import litellm

        litellm.success_callback = []
        litellm.failure_callback = []
        litellm.callbacks = []
        litellm.suppress_debug_info = True
        litellm.set_verbose = False
        litellm.telemetry = False
        # Silently drop params the upstream model doesn't support
        # rather than raising ``UnsupportedParamsError``. The canonical
        # case: ``gpt-5*`` only accepts ``temperature=1`` and rejects
        # any other value. App YAMLs commonly carry ``temperature: 0.7``
        # as a baseline -- without ``drop_params=True``, every gpt-5
        # call from a YAML that didn't special-case the model would
        # fail with a 400. LiteLLM only drops the specific params the
        # provider rejects; the rest pass through unchanged.
        litellm.drop_params = True
    except Exception as exc:
        logger.warning("litellm_eager_import_failed: %s", exc)

    # Catalogue (model aliases) - legacy YAML load, kept for the
    # fallback path when a model alias isn't yet in the gateway DB.
    catalog = load_catalog(settings.models_config_path)
    set_catalog(catalog)

    # Database. Build engine + session factory before any quota-aware
    # subsystem is created. Schema migrations are owned by Alembic
    # (see `alembic/versions/`), not by `Base.metadata.create_all` -
    # so the gateway can safely run against a Postgres that already
    # has the auth tables in place.
    init_db()
    factory = get_session_factory()

    # ── Dashboard-writable config: master key + seed + cache ─────
    # The cipher module needs DIGITORN_GATEWAY_MASTER_KEY in env. If
    # missing, the gateway boots WITHOUT the writable subsystem so
    # operators can still hit /v1/chat/completions on the legacy
    # YAML+env path. A clear log line lets ops see why the dashboard
    # CRUD looks broken.
    config_cache_loaded = False
    try:
        from digitorn_gateway.cipher import get_master as _get_master
        _get_master()
    except Exception as exc:
        logger.warning(
            "writable_config_disabled (DIGITORN_GATEWAY_MASTER_KEY): %s", exc,
        )
    else:
        try:
            await asyncio.wait_for(
                seed_if_empty(factory, catalog=catalog), timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "bootstrap_seed_timeout (DB unreachable, continuing without seed)"
            )
        except Exception as exc:
            logger.warning("bootstrap_seed_failed (continuing): %s", exc)
        try:
            cache = get_config_cache()
            # Hard timeout on the boot-time DB reload so a broken DB
            # doesn't hang ``Waiting for application startup`` forever.
            # The periodic refresh loop will retry once the DB is back.
            try:
                stats = await asyncio.wait_for(
                    cache.reload_from_db(factory), timeout=15.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "config_cache_reload_timeout (continuing with empty cache; "
                    "periodic refresh will retry)",
                )
                stats = {"providers": 0, "models": 0, "credentials": 0, "routes": 0}
            await start_config_cache_refresh(factory)
            try:
                await asyncio.wait_for(start_oauth_refresh(factory), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("oauth_refresh_start_timeout (continuing)")
            await start_route_probe()
            config_cache_loaded = True
            logger.info("config_cache_loaded %s", stats)

            # Cross-worker invalidation via Redis Pub/Sub. Reuses the
            # same ``quota_redis_url`` env var operators already wire
            # up for production multi-worker deployments. With this
            # active, ``set_route`` / ``upsert_credential`` / ... on
            # one worker propagate to every other worker in < 100ms
            # via a ``reload_from_db()`` triggered by a Pub/Sub
            # message. Without it, caches converge only via the 30 s
            # periodic refresh -- workable in single-worker dev but
            # unsuitable for multi-worker prod.
            if settings.quota_redis_url:
                from digitorn_gateway.config_redis import (
                    ConfigCoordinator, set_coordinator,
                )
                coord = ConfigCoordinator(settings.quota_redis_url)

                async def _on_invalidate(payload: dict[str, Any]) -> None:
                    """Re-read the entire DB into the cache. Cheap (one
                    SELECT per table, < 200ms on prod) and bulletproof
                    against partial-update bugs. The kind / key in
                    ``payload`` are kept for future granular reloads
                    + audit visibility but we don't act on them yet."""
                    try:
                        await cache.reload_from_db(factory)
                    except Exception as exc:
                        logger.warning(
                            "config_cache_reload_on_invalidate_failed "
                            "kind=%s key=%s: %s",
                            payload.get("kind"), payload.get("key"), exc,
                        )

                ok = await coord.start(on_invalidate=_on_invalidate)
                if ok:
                    set_coordinator(coord)
                    logger.info(
                        "config_redis_started url=%s "
                        "(cross-worker cache invalidation live)",
                        settings.quota_redis_url,
                    )
                else:
                    logger.warning(
                        "config_redis_unavailable - cross-worker cache "
                        "convergence will rely on the 30s periodic "
                        "refresh only. Check redis_url=%s",
                        settings.quota_redis_url,
                    )
        except Exception as exc:
            logger.warning(
                "config_cache_load_failed (legacy fallback active): %s", exc,
            )

    # Plans: seed defaults from YAML, then load into in-memory registry.
    if settings.quota_enabled:
        try:
            await asyncio.wait_for(
                seed_plans_from_yaml(
                    session_factory=factory,
                    yaml_path=settings.quota_plans_seed_path,
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.warning("plans_seed_timeout (continuing)")
        except Exception as exc:
            logger.warning("plans_seed_failed (continuing): %s", exc)

        plan_registry = init_plan_registry(
            session_factory=factory,
            default_plan_name=settings.quota_default_plan_name,
            user_cache_ttl_seconds=settings.quota_plan_cache_ttl_seconds,
        )
        try:
            await asyncio.wait_for(plan_registry.reload_plans(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("plan_registry_reload_timeout (empty plan set)")
        except Exception as exc:
            logger.warning("plan_registry_reload_failed: %s", exc)

        # Optional cross-worker coordinator. Hot path stays in-memory;
        # this only kicks in on the post-call BackgroundTask.
        redis_coord = None
        if settings.quota_redis_url:
            from digitorn_gateway.quota_redis import RedisCoordinator
            redis_coord = RedisCoordinator(settings.quota_redis_url)

        engine = init_engine(
            session_factory=factory,
            plan_registry=plan_registry,
            flush_interval_seconds=settings.quota_flush_interval_seconds,
            redis_coordinator=redis_coord,
        )
        if redis_coord is not None:
            ok = await redis_coord.start(on_remote_block=engine._on_remote_block)
            if not ok:
                logger.warning(
                    "quota_redis_unavailable - falling back to in-memory only "
                    "(multi-worker leak possible). Check redis_url=%s",
                    settings.quota_redis_url,
                )
        # Boot-time DB calls need a hard timeout so a broken or
        # unreachable Postgres at startup degrades gracefully (gateway
        # boots in degraded mode, /healthz serves, hot path still
        # works, periodic loops will retry once the DB is back).
        # Without the timeout, asyncpg's connect retry can keep us in
        # ``Waiting for application startup`` indefinitely.
        try:
            await asyncio.wait_for(engine.recover_from_db(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning(
                "quota_recover_timeout (continuing fresh, DB may be down)"
            )
        except Exception as exc:
            logger.warning("quota_recover_failed (continuing fresh): %s", exc)
        # Leader-election: only ONE worker per cluster runs the
        # supervisor (decides blocks). Per-process flush still runs
        # everywhere because each worker has its own counters slice.
        try:
            from digitorn_gateway.cluster_sync import (
                try_acquire_leader_lock as _lead,
            )
            _is_leader = await asyncio.wait_for(
                _lead("quota_supervisor"), timeout=10.0,
            )
        except (asyncio.TimeoutError, Exception):
            _is_leader = True  # solo mode: no DB lock available, behave as before
        engine.start(start_supervisor=_is_leader)
        if not _is_leader:
            logger.info(
                "quota_supervisor: another worker is leader; standing by "
                "as standby (will take over if leader dies)"
            )
        # Hand the takeover loop the start/stop hooks. If the current
        # leader dies, this worker's next acquire attempt fires
        # ``start_supervisor`` automatically; no manual restart needed.
        async def _on_supervisor_become_leader() -> None:
            engine.start_supervisor()
            logger.info("quota_supervisor: this worker is now the leader")

        async def _on_supervisor_lose_leader() -> None:
            await engine.stop_supervisor()
            logger.info("quota_supervisor: this worker stepped down")

        try:
            from digitorn_gateway import cluster_sync as _cs
            _cs.watch_leader(
                "quota_supervisor",
                on_become=_on_supervisor_become_leader,
                on_lose=_on_supervisor_lose_leader,
            )
        except Exception as exc:
            logger.debug("quota_supervisor_takeover_register_failed: %s", exc)
    else:
        logger.info("quota_disabled by config")

    # Response cache. Master switch is ``settings.cache_enabled``;
    # when off, ``init_cache`` is still called but the resulting
    # backend is never queried (the chat handler short-circuits).
    # We DO call init even when off so a future flip-on can pick up
    # a redis client without restart.
    if settings.cache_enabled:
        try:
            from digitorn_gateway.response_cache import init_cache as _init_cache
            _init_cache(settings.cache_redis_url)
        except Exception as exc:
            # Defensive: a broken cache module must NEVER prevent the
            # gateway from booting. Worst case, requests dispatch
            # without caching.
            logger.warning("response_cache_init_failed: %s", exc)

    # Failover load-aware spill: pass the per-credential inflight cap
    # to the resolver so saturated credentials get filtered out and the
    # dispatch falls through to the next route. 0 = no cap (legacy).
    try:
        from digitorn_gateway.config_cache import get_cache as _get_cache_for_cap
        _get_cache_for_cap().set_inflight_cap(
            settings.failover_max_inflight_per_credential,
        )
    except Exception as exc:
        logger.warning("failover_cap_init_failed: %s", exc)

    # Live runtime overrides: read every row from gateway_runtime_settings
    # and reapply BEFORE traffic starts, so the first request already
    # sees the operator's intent. Best-effort - missing table or DB
    # blip falls back to env defaults.
    try:
        from digitorn_gateway.runtime_settings_routes import (
            load_runtime_overrides as _load_overrides,
            apply_one_override as _apply_one,
        )
        # Same boot-time hard timeout: don't hang if DB is down.
        try:
            await asyncio.wait_for(_load_overrides(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning(
                "runtime_settings_load_timeout (using env defaults)"
            )
    except Exception as exc:
        logger.warning("runtime_settings_load_skipped: %s", exc)
        _apply_one = None  # type: ignore

    # Cluster-wide propagation: register listener handlers BEFORE
    # starting the LISTEN connection. Each handler is best-effort -
    # a slow handler doesn't block its peers (cluster_sync schedules
    # them as tasks).
    try:
        from digitorn_gateway import cluster_sync as _cs
        if _apply_one is not None:
            _cs.register_handler(
                _cs.CHANNEL_RUNTIME_SETTINGS,
                lambda key: _apply_one(key),
            )
        # Config cache: peer signalled providers/credentials/models/
        # routes have changed. Just reload from DB - cheap enough.
        async def _reload_config_cache(_payload: str) -> None:
            try:
                from digitorn_gateway.config_cache import get_cache as _gc
                from digitorn_gateway.db import get_session_factory as _gsf
                await _gc().reload_from_db(_gsf())
            except Exception as exc:
                logger.warning("config_cache_peer_reload_failed: %s", exc)
        _cs.register_handler(_cs.CHANNEL_CONFIG_CACHE, _reload_config_cache)
        # Quota blocks (filled in by hygiene 3 / quota engine when
        # Redis pub/sub isn't enabled).
        async def _quota_blocks_handler(_payload: str) -> None:
            try:
                from digitorn_gateway.quota import get_engine as _ge
                eng = _ge()
                if hasattr(eng, "refresh_blocks_from_db"):
                    await eng.refresh_blocks_from_db()
            except Exception as exc:
                logger.debug("quota_blocks_peer_apply_failed: %s", exc)
        _cs.register_handler(_cs.CHANNEL_QUOTA_BLOCKS, _quota_blocks_handler)

        await _cs.start_listener()
    except Exception as exc:
        logger.warning("cluster_sync_listener_skipped: %s", exc)

    # Auth JWKS - fetch at boot, then refresh periodically.
    # Hard timeout so a slow/down auth service doesn't block boot.
    jwks = init_jwks(settings.auth_jwks_url)
    try:
        await asyncio.wait_for(jwks.fetch(), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning(
            "boot_jwks_fetch_timeout url=%s - first /v1 request will retry",
            settings.auth_jwks_url,
        )
    except Exception as exc:
        logger.warning(
            "boot_jwks_fetch_failed url=%s err=%s - first /v1 request "
            "will retry. Until then, all /v1 requests fail with 503.",
            settings.auth_jwks_url, exc,
        )

    refresh_task = asyncio.create_task(
        _periodic_jwks_refresh(jwks, settings.auth_jwks_refresh_seconds),
        name="gateway-jwks-refresh",
    )
    app.state.jwks_refresh_task = refresh_task

    # v2: keep gateway_usage_events partitions a quarter ahead.
    # Leader-only: partition creation is idempotent but spamming it
    # from N workers is wasteful + fills the audit log with noise.
    try:
        from digitorn_gateway.cluster_sync import (
            try_acquire_leader_lock as _lead_pk,
        )
        _pk_leader = await asyncio.wait_for(
            _lead_pk("partition_keeper"), timeout=10.0,
        )
    except (asyncio.TimeoutError, Exception):
        _pk_leader = True
    if _pk_leader:
        from digitorn_gateway import partition_keeper as _pk
        partition_task = asyncio.create_task(
            _pk.run(), name="gateway-partition-keeper",
        )
        app.state.partition_task = partition_task
    else:
        app.state.partition_task = None
        logger.info("partition_keeper: peer holds leader lock, skipping")

    async def _on_pk_become() -> None:
        if app.state.partition_task is None or app.state.partition_task.done():
            from digitorn_gateway import partition_keeper as _pk
            app.state.partition_task = asyncio.create_task(
                _pk.run(), name="gateway-partition-keeper",
            )
            logger.info("partition_keeper: this worker is now the leader")

    async def _on_pk_lose() -> None:
        t = app.state.partition_task
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
            app.state.partition_task = None
            logger.info("partition_keeper: stepped down")

    try:
        from digitorn_gateway import cluster_sync as _cs
        _cs.watch_leader(
            "partition_keeper", on_become=_on_pk_become, on_lose=_on_pk_lose,
        )
    except Exception as exc:
        logger.debug("partition_keeper_takeover_register_failed: %s", exc)

    # v2: enforce audit_actions_catalog.retention_days nightly.
    # Same leader-only pattern - retention is destructive and
    # idempotent, but only one worker should run it.
    try:
        from digitorn_gateway.cluster_sync import (
            try_acquire_leader_lock as _lead_rk,
        )
        _rk_leader = await asyncio.wait_for(
            _lead_rk("retention_keeper"), timeout=10.0,
        )
    except (asyncio.TimeoutError, Exception):
        _rk_leader = True
    if _rk_leader:
        from digitorn_gateway import retention_keeper as _rk
        retention_task = asyncio.create_task(
            _rk.run(), name="gateway-retention-keeper",
        )
        app.state.retention_task = retention_task
    else:
        app.state.retention_task = None
        logger.info("retention_keeper: peer holds leader lock, skipping")

    async def _on_rk_become() -> None:
        if app.state.retention_task is None or app.state.retention_task.done():
            from digitorn_gateway import retention_keeper as _rk
            app.state.retention_task = asyncio.create_task(
                _rk.run(), name="gateway-retention-keeper",
            )
            logger.info("retention_keeper: this worker is now the leader")

    async def _on_rk_lose() -> None:
        t = app.state.retention_task
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
            app.state.retention_task = None
            logger.info("retention_keeper: stepped down")

    try:
        from digitorn_gateway import cluster_sync as _cs
        _cs.watch_leader(
            "retention_keeper", on_become=_on_rk_become, on_lose=_on_rk_lose,
        )
        # Start the takeover loop AFTER all watch_leader registrations
        # so the very first poll sees every scope.
        await _cs.start_takeover_loop()
    except Exception as exc:
        logger.debug("retention_keeper_takeover_register_failed: %s", exc)

    logger.info(
        "gateway_started host=%s port=%s models=%d quota=%s",
        settings.host, settings.port, len(catalog.all()),
        "enabled" if settings.quota_enabled else "disabled",
    )

    # Connection pool: warm httpx clients per credential (live_pool=True).
    # Idempotent: starting twice is a no-op. Started even when
    # ``config_cache_loaded`` is False so the pool's evictor runs in the
    # legacy YAML-fallback case too (no harm: the dispatch path won't
    # populate it without a CachedCredential anyway).
    try:
        from digitorn_gateway.connection_pool import get_pool as _get_pool
        _get_pool().start_evictor(interval_s=60.0)
        logger.info("connection_pool_evictor_started")
    except Exception as exc:
        logger.warning("connection_pool_evictor_start_failed: %s", exc)

    try:
        yield
    finally:
        # Cluster-wide listener + takeover loop: stop FIRST so they
        # stop dispatching to handlers whose modules we're about to
        # tear down.
        try:
            from digitorn_gateway import cluster_sync as _cs
            await _cs.stop_takeover_loop()
            await _cs.stop_listener()
            # Release any leader locks held by this worker so peers
            # can take over without waiting for connection timeout.
            for _scope in list(_cs._held_locks):
                try:
                    await _cs.release_leader_lock(_scope)
                except Exception:
                    pass
        except Exception:
            logger.debug("cluster_sync_stop_failed", exc_info=True)
        if config_cache_loaded:
            try:
                await stop_config_cache_refresh()
            except Exception:
                logger.warning("config_cache_stop_failed", exc_info=True)
            try:
                await stop_oauth_refresh()
            except Exception:
                logger.warning("oauth_refresh_stop_failed", exc_info=True)
            try:
                await stop_route_probe()
            except Exception:
                logger.warning("route_probe_stop_failed", exc_info=True)
            try:
                from digitorn_gateway.config_redis import (
                    get_coordinator, set_coordinator,
                )
                _c = get_coordinator()
                if _c is not None:
                    await _c.stop()
                    set_coordinator(None)
            except Exception:
                logger.warning("config_redis_stop_failed", exc_info=True)
        try:
            from digitorn_gateway.connection_pool import get_pool as _get_pool
            await _get_pool().shutdown()
        except Exception:
            logger.warning("connection_pool_shutdown_failed", exc_info=True)
        # Some background tasks may be None when this worker isn't the
        # leader for that scope; the takeover loop didn't promote it
        # before shutdown. Skip Nones cleanly.
        _shutdown_tasks = [
            refresh_task,
            getattr(app.state, "partition_task", None),
            getattr(app.state, "retention_task", None),
        ]
        for t in _shutdown_tasks:
            if t is None:
                continue
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        if settings.quota_enabled:
            try:
                eng = get_engine()
                if eng._redis is not None:
                    try:
                        await eng._redis.stop()
                    except Exception:
                        logger.warning("quota_redis_stop_failed", exc_info=True)
                await eng.stop()
            except Exception:
                logger.warning("quota_engine_stop_failed", exc_info=True)
        await dispose_engine()


app = FastAPI(
    title="Digitorn LLM Gateway",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS so the admin dashboard (different origin) can call us.
_cors_origins = [
    o.strip() for o in get_settings().cors_allow_origins.split(",") if o.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )

# Mount the quota subsystem routes (user + admin) + usage analytics.
# Order matters: admin_writable_router REGISTERS FIRST so its CRUD
# handlers for /admin/providers and /admin/models win over the
# legacy read-only handlers in admin_config_router.
from digitorn_gateway.diag_routes import router as diag_router
from digitorn_gateway.runtime_settings_routes import router as runtime_settings_router


# Cluster-wide cache invalidation: any successful mutation on an
# admin resource (providers / credentials / models / routes / plans)
# fires a NOTIFY so every peer worker reloads its local ConfigCache.
# Single middleware = 100% coverage for free; no per-endpoint diff.
# When cluster_sync is unavailable (no DB, network blip) the notify
# is a silent no-op, never blocking the response.
_INVALIDATING_PREFIXES = (
    "/admin/providers", "/admin/credentials", "/admin/models",
    "/admin/routes", "/admin/quota/plans", "/admin/diag/refresh-cache",
)


@app.middleware("http")
async def _config_invalidation_notify(request, call_next):
    response = await call_next(request)
    try:
        if (
            request.method in ("POST", "PUT", "PATCH", "DELETE")
            and 200 <= response.status_code < 300
            and any(request.url.path.startswith(p) for p in _INVALIDATING_PREFIXES)
        ):
            from digitorn_gateway.cluster_sync import (
                notify as _notify, CHANNEL_CONFIG_CACHE,
            )
            # Fire-and-forget so the response isn't held back.
            asyncio.create_task(_notify(CHANNEL_CONFIG_CACHE, request.url.path))
    except Exception:
        pass
    return response


app.include_router(diag_router)
app.include_router(runtime_settings_router)
app.include_router(admin_writable_router)
app.include_router(oauth_login_router)
app.include_router(quota_router)
app.include_router(usage_router)
app.include_router(dashboard_router)
app.include_router(admin_config_router)
from digitorn_gateway.metrics import router as metrics_router
app.include_router(metrics_router)


# ── Health ─────────────────────────────────────────────────────────


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# ── Models ─────────────────────────────────────────────────────────


@app.get("/v1/models")
async def list_models(
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """List every alias the caller can dispatch.

    Sources, in this order (later sources can override earlier ones
    if an alias was defined in both):

      1. Legacy YAML catalog (``catalog.all()``) - kept for back-compat
         with operators who still maintain ``models.yaml``.
      2. DB-backed catalog (``ConfigCache.all_models()``) - the
         dashboard-managed source of truth.

    De-duplication is by ``id`` (alias name); DB wins."""
    items: dict[str, dict[str, Any]] = {}
    from digitorn_gateway.config_cache import get_cache as _get_cache
    cache = _get_cache()

    # Only advertise aliases that actually dispatch end-to-end. The catalogue
    # is the source of truth for the *human-readable* model list, but a YAML
    # entry is meaningless if the alias isn't backed by a row in the
    # gateway_models table (so the cache can resolve a route + inject a
    # credential). Listing un-dispatchable aliases is the bug that produced
    # 502s for digitorn-* in stabilization tests.
    for m in cache.all_models():
        if cache.resolve_dispatch(m.alias) is None:
            continue
        items[m.alias] = {
            "id": m.alias,
            "object": "model",
            "created": 0,
            "owned_by": m.provider_slug,
            "max_context_tokens": m.max_context,
        }
    return {"object": "list", "data": list(items.values())}


# ── Chat completions ───────────────────────────────────────────────


def _quota_check_or_raise(user_id: str) -> None:
    """O(1) sticky-block lookup. Raises 429 with structured payload
    when the user is blocked. Called BEFORE we touch the LLM provider.
    """
    settings = get_settings()
    if not settings.quota_enabled:
        return
    blocked, info = get_engine().is_blocked(user_id)
    if not blocked or info is None:
        return
    raise HTTPException(
        status_code=429,
        detail={
            "code": "quota_exceeded",
            "reason": info.reason,
            "metric": info.metric,
            "window": info.window,
            "limit": info.limit_value,
            "actual": info.actual_value,
            "retry_after": info.blocked_until_dt.isoformat(),
        },
    )


async def _quota_record(
    *,
    user_id: str,
    model_alias: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: float,
    success: bool,
    error_class: str | None = None,
    run_id: str | None = None,
    agent_id: str | None = None,
    app_id: str | None = None,
    external_sid: str | None = None,
    fallback_messages: list | None = None,
    fallback_content: str | None = None,
    cache_key: str | None = None,
    cache_payload: dict | None = None,
    served_by: str | None = None,
    attempts: int = 1,
    failover_trail: list[str] | None = None,
    truncated_dropped: int = 0,
    cache_hit: bool = False,
) -> None:
    """Post-call accounting. Updates the in-memory quota counter (DB
    flush is amortised by the background loop) AND writes one row to
    ``gateway_usage_events`` so the dashboard / admin endpoints have a
    full per-call audit trail. Never raises.

    Designed to run via FastAPI ``BackgroundTask`` so it never blocks
    the response back to the daemon - the LLM result reaches the
    caller and accounting happens in parallel. ALL CPU work lives
    here (tokenizer fallback, DB writes, counter increments).
    """
    # Local tokenizer fallback. Runs ONLY when the upstream omitted a
    # usage block (input=0 AND output=0 on a successful call). Uses
    # the model's PUBLISHED tokenizer (litellm routes per-model to
    # tiktoken / anthropic-tokenizer / HF tokenizers); the result is
    # bit-identical to what the provider would have reported.
    # CPU-bound but sub-ms; runs HERE in the BackgroundTask so it
    # never delays the response on the hot path. Cache hits skip
    # the fallback - they legitimately have 0/0 tokens (no upstream
    # call happened).
    if (
        success and input_tokens == 0 and output_tokens == 0
        and error_class != "cache_hit"
    ):
        try:
            import litellm as _litellm
            if fallback_messages:
                input_tokens = int(_litellm.token_counter(
                    model=model_alias, messages=fallback_messages,
                ) or 0)
            if fallback_content:
                output_tokens = int(_litellm.token_counter(
                    model=model_alias, text=fallback_content,
                ) or 0)
            # Recompute cost now that we have token counts.
            if input_tokens > 0 or output_tokens > 0:
                try:
                    from digitorn_gateway.config_cache import get_cache as _gc
                    resolved = _gc().resolve_dispatch(model_alias)
                    if resolved is not None:
                        cost_usd = round(
                            (input_tokens / 1000.0) * resolved.cost_per_1k_input
                            + (output_tokens / 1000.0) * resolved.cost_per_1k_output,
                            6,
                        )
                except Exception:
                    pass
        except Exception as exc:
            logger.debug(
                "token_counter_fallback_failed model=%s err=%s",
                model_alias, exc,
            )

    # If we STILL have 0/0 after the fallback (tokenizer not available
    # for this model, or both messages and content empty), flag the
    # row so dashboards can surface "% of events with missing usage".
    if success and input_tokens == 0 and output_tokens == 0 and error_class is None:
        error_class = "usage_missing"
        logger.warning(
            "usage_missing user=%s provider=%s model=%s "
            "(provider returned no usage block AND tokenizer fallback "
            "could not recover; recording 0 tokens)",
            user_id, provider, model_alias,
        )

    # In-memory quota counter (gating future requests).
    if get_settings().quota_enabled:
        try:
            await get_engine().record(UsageRecord(
                user_id=user_id,
                model_alias=model_alias,
                provider=provider,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                success=success,
            ))
        except Exception as exc:
            logger.warning(
                "quota_record_failed user=%s model=%s err=%s",
                user_id, model_alias, exc,
            )

    # Per-call usage event (observability). Best-effort; failures
    # already logged inside record_event.
    from digitorn_gateway.usage_events import record_event
    await record_event(
        user_id=user_id,
        provider=provider,
        model=model_alias,
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        error_class=error_class,
        run_id=run_id,
        agent_id=agent_id,
        app_id=app_id,
        external_sid=external_sid,
        served_by=served_by or provider,
        attempts=attempts,
        failover_trail=failover_trail,
        truncated_dropped=truncated_dropped,
        cache_hit=cache_hit,
    )

    # Cache write (background). When the chat handler computed a
    # ``cache_key`` and dispatch succeeded, store the response so
    # future identical requests short-circuit. NEVER raises - any
    # failure is logged inside ``response_cache.store`` and silently
    # ignored.
    if cache_key is not None and cache_payload is not None and success:
        try:
            from digitorn_gateway.response_cache import store as _cache_store
            from digitorn_gateway.config import get_settings as _gs
            await _cache_store(
                cache_key, cache_payload,
                ttl_seconds=_gs().cache_default_ttl_seconds,
            )
        except Exception as exc:
            logger.debug("cache_store_skipped (%s)", exc)


def _build_trace_headers(trace: Any) -> dict[str, str]:
    """Translate a ``DispatchTrace`` into the X-Digitorn-* response
    headers operators (and clients) read to know which provider served
    the request and how many fallbacks were burned.

    Empty / zero values are not emitted - the headers stay clean for
    the common single-attempt success case (only ``Served-By`` +
    ``Attempts: 1`` show up).
    """
    out: dict[str, str] = {}
    if not trace:
        return out
    if getattr(trace, "served_by", ""):
        out["X-Digitorn-Served-By"] = trace.served_by
    if getattr(trace, "attempts", 0):
        out["X-Digitorn-Attempts"] = str(trace.attempts)
    if getattr(trace, "trail", None) and trace.attempts and trace.attempts > 1:
        # Only emit the trail when we actually burned a fallback - the
        # single-attempt case is already implied by served_by + attempts=1.
        out["X-Digitorn-Failover-Trail"] = ",".join(trace.trail)
    if getattr(trace, "route_id", ""):
        out["X-Digitorn-Route-Id"] = trace.route_id
    if getattr(trace, "truncated_dropped", 0):
        out["X-Digitorn-Truncated"] = f"dropped={trace.truncated_dropped}"
    return out


def _read_attribution_headers(request: Request) -> dict[str, str | None]:
    """Pull the ``X-Digitorn-*`` attribution headers off an inbound
    request. The daemon sets these to identify which run/agent issued
    the LLM call so the gateway can attribute cost in
    ``gateway_usage_events``. Missing headers are recorded as NULL.

    ``user_id`` is intentionally NOT read here - it's authoritatively
    extracted from the JWT (``principal.user_id``) so a malicious or
    buggy client can't impersonate another user.
    """
    h = request.headers
    return {
        "run_id": h.get("x-digitorn-run-id") or None,
        "agent_id": h.get("x-digitorn-agent-id") or None,
        "app_id": h.get("x-digitorn-app-id") or None,
        "external_sid": h.get("x-digitorn-session-id") or None,
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    principal: GatewayPrincipal = Depends(require_principal),
):
    settings = get_settings()

    # Body size guard.
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > settings.max_request_bytes:
                raise HTTPException(413, detail="request_too_large")
        except ValueError:
            pass

    raw = await request.body()
    if len(raw) > settings.max_request_bytes:
        raise HTTPException(413, detail="request_too_large")

    try:
        body = _loads(raw)
    except Exception as exc:
        raise HTTPException(400, detail=f"invalid_json: {exc}") from exc

    if not isinstance(body, dict):
        raise HTTPException(400, detail="body_must_be_object")
    if "model" not in body:
        raise HTTPException(400, detail="missing_field: model")
    if "messages" not in body:
        raise HTTPException(400, detail="missing_field: messages")

    # Pre-call quota gate. O(1) memory check, raises 429 if blocked.
    _quota_check_or_raise(principal.user_id)

    # Pre-call provider gate. Surface a clear ``model_not_provided``
    # error BEFORE the LLM round trip when the gateway has no key
    # for the requested provider. Saves the user from waiting 30 s
    # for an opaque 401 from LiteLLM.
    from digitorn_gateway.llm_call import check_provider_supported
    _supported, _provider, _missing_key = check_provider_supported(body["model"])
    if not _supported:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "model_not_provided_by_digitorn",
                "category": "configuration",
                "provider": _provider,
                "model": body["model"],
                "missing_env_key": _missing_key,
                "message": (
                    f"The model '{body['model']}' (provider: {_provider}) "
                    "is not provided by Digitorn. To use this app, "
                    "configure your own credentials for this provider, "
                    "or pick a model from the Digitorn-supported list."
                ),
            },
        )

    # Read the daemon's attribution headers BEFORE dispatch so a
    # malformed request still has the IDs available for error events.
    attribution = _read_attribution_headers(request)

    # Mode 1 truncation: pre-flight context window check. Count tokens
    # once and reject with a clean 413 if the request would overflow
    # the resolved model's max_context. Saves the user a 200ms-2s RTT
    # to the provider for an opaque 400 response. Branch is fully
    # bypassed when ``truncate_enabled=False`` (zero overhead).
    if settings.truncate_enabled:
        try:
            from digitorn_gateway.config_cache import get_cache as _get_cache_for_trim
            from digitorn_gateway.truncation import (
                count_tokens as _count_tokens,
                check_overflow as _check_overflow,
                can_skip_tokenization as _can_skip_tok,
            )
            _cached_model = _get_cache_for_trim().model(body.get("model", ""))
            if (_cached_model is not None
                    and _cached_model.max_context
                    and not _can_skip_tok(
                        body.get("messages") or [],
                        _cached_model.max_context,
                    )):
                _max_out = int(
                    body.get("max_tokens")
                    or settings.truncate_default_max_output_tokens,
                )
                _request_tokens = _count_tokens(
                    _cached_model.real_model_id, body.get("messages") or [],
                )
                _overflows, _budget = _check_overflow(
                    _request_tokens, _cached_model.max_context, _max_out,
                )
                if _overflows:
                    raise HTTPException(
                        status_code=413,
                        detail={
                            "code": "context_window_exceeded",
                            "category": "request",
                            "model": body.get("model"),
                            "tokens_in_request": _request_tokens,
                            "max_context": _cached_model.max_context,
                            "max_tokens_reserved": _max_out,
                            "input_budget": _budget,
                            "trim_tokens": _request_tokens - _budget,
                            "message": (
                                f"Request has {_request_tokens} input tokens "
                                f"but model '{body.get('model')}' supports "
                                f"{_cached_model.max_context} total. Reserved "
                                f"{_max_out} for output. Trim "
                                f"{_request_tokens - _budget} tokens from "
                                f"messages or lower max_tokens."
                            ),
                        },
                    )
        except HTTPException:
            raise
        except Exception as exc:
            # Truncation must NEVER take the gateway down. On any
            # internal error in the guard, fall through to dispatch.
            logger.debug("truncation_preflight_skipped (%s)", exc)

    stream = bool(body.get("stream"))

    if stream:
        # Pre-open: drive ``_stream_response`` to its first SSE byte so
        # the failover loop in ``dispatch_stream`` finishes its open
        # phase BEFORE we hand the iterator to ``StreamingResponse``.
        # That gives us the populated ``DispatchTrace`` in time to set
        # X-Digitorn-* response headers; without the pre-open the
        # headers would already be flushed before the trace was known.
        from digitorn_gateway.llm_call import DispatchTrace
        stream_trace = DispatchTrace()
        inner_iter = _stream_response(
            body, principal, attribution, trace=stream_trace,
        )
        try:
            first_bytes = await inner_iter.__anext__()
        except StopAsyncIteration:
            first_bytes = None

        async def _resumed_iter() -> AsyncIterator[bytes]:
            if first_bytes is not None:
                yield first_bytes
            async for chunk in inner_iter:
                yield chunk

        stream_headers = _build_trace_headers(stream_trace)
        stream_headers["Cache-Control"] = "no-cache"
        stream_headers["X-Accel-Buffering"] = "no"
        return StreamingResponse(
            _resumed_iter(),
            media_type="text/event-stream",
            headers=stream_headers,
        )

    # ── Response cache lookup (opt-in, hot-path-safe) ──
    # Three guards before we touch the cache module at all:
    #   1. settings.cache_enabled (master switch)
    #   2. ``X-Digitorn-Cache: enabled`` request header (per-request
    #      opt-in, so traffic that didn't ask for caching pays nothing)
    #   3. is_cacheable() inside compute_key (skips streaming, temp>0,
    #      tools, etc. - returns None which short-circuits the lookup)
    # The lookup itself has a hard timeout (settings.cache_lookup_timeout_ms,
    # default 5ms) and swallows ALL exceptions inside response_cache.lookup -
    # a slow or dead Redis can never block the user.
    cache_key: str | None = None
    if (
        settings.cache_enabled
        and request.headers.get("x-digitorn-cache", "").lower() == "enabled"
    ):
        try:
            from digitorn_gateway.response_cache import (
                compute_key as _cache_key,
                lookup as _cache_lookup,
            )
            cache_key = _cache_key(principal.user_id, body)
            if cache_key is not None:
                cached = await _cache_lookup(
                    cache_key, timeout_ms=settings.cache_lookup_timeout_ms,
                )
                if cached is not None:
                    # Cache hit: return immediately. We still record
                    # the event (so analytics show the hit) but pass
                    # 0 tokens / 0 cost to ``_quota_record`` and
                    # tag with ``error_class=cache_hit`` - that
                    # bypasses the tokenizer fallback and surfaces
                    # the hit in the dashboard.
                    return JSONResponse(
                        cached,
                        headers={
                            "X-Digitorn-Cache": "hit",
                            "X-Digitorn-Served-By": "cache",
                            "X-Digitorn-Attempts": "0",
                        },
                        background=BackgroundTask(
                            _quota_record,
                            user_id=principal.user_id,
                            model_alias=body.get("model", ""),
                            provider="cache",
                            input_tokens=0,
                            output_tokens=0,
                            cost_usd=0.0,
                            latency_ms=0.0,
                            success=True,
                            error_class="cache_hit",
                            served_by="cache",
                            attempts=0,
                            cache_hit=True,
                            **attribution,
                        ),
                    )
        except Exception as exc:
            # Cache code MUST NEVER take the gateway down. On ANY
            # failure here we silently fall through to a normal
            # dispatch - the user gets a real response, just no cache
            # benefit on this call.
            logger.debug("cache_lookup_skipped (%s)", exc)
            cache_key = None

    # Per-request failover trace. Filled by ``dispatch`` with the
    # winning provider, attempt count and the trail. Surfaced to the
    # client via X-Digitorn-* response headers below.
    from digitorn_gateway.llm_call import DispatchTrace
    trace = DispatchTrace()

    try:
        resp, usage_record = await dispatch(body=body, trace=trace)
    except CustomProviderNotImplemented as exc:
        raise HTTPException(501, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "dispatch_failed user=%s model=%s",
            principal.user_id, body.get("model"),
        )
        raise HTTPException(
            502,
            detail=f"upstream_error: {type(exc).__name__}: {exc}",
        ) from exc

    # Post-call accounting: NEVER block the response. The daemon gets
    # the LLM result immediately; quota counter + gateway_usage_events
    # write happen in a FastAPI BackgroundTask that runs AFTER the
    # response goes out. Total overhead visible to the daemon = 0.
    # Pull the assistant content out of the response so the
    # BackgroundTask can run the local-tokenizer fallback if the
    # provider didn't include a usage block. Cheap dict navigation,
    # no new compute on the hot path.
    _content_for_fallback = ""
    try:
        _choices = resp.get("choices") or []
        if _choices:
            _content_for_fallback = (_choices[0].get("message") or {}).get("content") or ""
    except Exception:
        pass
    # Cache write also happens in the BackgroundTask. We pass the
    # ``cache_key`` (already computed pre-dispatch) and the response
    # payload so the storage step is just a dict pickle + Redis SET.
    _trace_headers = _build_trace_headers(trace)
    if cache_key:
        _trace_headers["X-Digitorn-Cache"] = "miss"
    return JSONResponse(
        resp,
        headers=_trace_headers,
        background=BackgroundTask(
            _quota_record,
            user_id=principal.user_id,
            model_alias=usage_record.model_alias,
            provider=usage_record.provider,
            input_tokens=usage_record.input_tokens,
            output_tokens=usage_record.output_tokens,
            cost_usd=usage_record.cost_usd,
            latency_ms=usage_record.latency_ms,
            success=usage_record.success,
            fallback_messages=body.get("messages") or [],
            fallback_content=_content_for_fallback,
            cache_key=cache_key,
            cache_payload=resp if cache_key else None,
            served_by=trace.served_by or usage_record.provider,
            attempts=trace.attempts or 1,
            failover_trail=(
                trace.trail if trace.attempts and trace.attempts > 1 else None
            ),
            truncated_dropped=trace.truncated_dropped,
            cache_hit=False,
            **attribution,
        ),
    )


# ── Streaming helpers ──────────────────────────────────────────────


async def _stream_response(
    body: dict[str, Any],
    principal: GatewayPrincipal,
    attribution: dict[str, str | None] | None = None,
    trace: Any = None,
) -> AsyncIterator[bytes]:
    # The trace is filled by ``dispatch_stream`` between the OPEN and
    # the first chunk yield. We read it in the finally block to feed
    # the trace fields into ``_quota_record`` for the usage_events row.
    from digitorn_gateway.llm_call import _compute_cost, resolve_alias

    alias = body.get("model", "")
    entry = resolve_alias(alias)

    attribution = attribution or {}
    in_tokens = 0
    out_tokens = 0
    sent_done = False
    # Accumulate the streamed assistant text so the local tokenizer
    # fallback (when provider doesn't send a usage chunk) has data to
    # count. This is just string append; cheap.
    streamed_content: list[str] = []

    try:
        async for chunk in dispatch_stream(body=body, trace=trace):
            usage = chunk.get("usage") if isinstance(chunk, dict) else None
            if usage:
                in_tokens = int(usage.get("prompt_tokens") or in_tokens)
                out_tokens = int(usage.get("completion_tokens") or out_tokens)
            # Capture the delta text for fallback tokenization. We only
            # collect it - the chunk goes straight to the client below.
            if isinstance(chunk, dict):
                for c in (chunk.get("choices") or []):
                    delta = c.get("delta") or c.get("message") or {}
                    if isinstance(delta, dict):
                        text = delta.get("content")
                        if isinstance(text, str) and text:
                            streamed_content.append(text)
            yield b"data: " + _dumps(chunk) + b"\n\n"
        yield b"data: [DONE]\n\n"
        sent_done = True
    except CustomProviderNotImplemented as exc:
        err = _dumps({
            "error": {"message": str(exc), "type": "not_implemented", "code": 501},
        })
        yield b"data: " + err + b"\n\n"
        if not sent_done:
            yield b"data: [DONE]\n\n"
    except Exception as exc:
        logger.exception(
            "stream_dispatch_failed user=%s model=%s",
            principal.user_id, alias,
        )
        err = _dumps({
            "error": {
                "message": f"upstream_error: {type(exc).__name__}: {exc}",
                "type": "upstream_error",
                "code": 502,
            },
        })
        yield b"data: " + err + b"\n\n"
        if not sent_done:
            yield b"data: [DONE]\n\n"
    finally:
        # Fire-and-forget the post-call accounting so the streaming
        # response can close from the client's perspective immediately
        # after [DONE]. The created task lives on the event loop,
        # un-awaited - it never holds the HTTP connection open. ALL
        # CPU work (tokenizer fallback, DB writes) happens inside
        # ``_quota_record`` which is async.
        joined_content = "".join(streamed_content)
        from digitorn_gateway.config_cache import get_cache as _get_cache
        _resolved = _get_cache().resolve_dispatch(alias)
        if _resolved is not None:
            _stream_provider = _resolved.provider_slug
            _stream_cost = round(
                (in_tokens / 1000.0) * _resolved.cost_per_1k_input
                + (out_tokens / 1000.0) * _resolved.cost_per_1k_output,
                6,
            ) if (in_tokens or out_tokens) else 0.0
        else:
            _stream_provider = entry.provider if entry else "unknown"
            _stream_cost = _compute_cost(entry, in_tokens, out_tokens)
        try:
            _stream_served_by = (
                getattr(trace, "served_by", "") or _stream_provider
            )
            _stream_attempts = getattr(trace, "attempts", 0) or 1
            _stream_trail_full = getattr(trace, "trail", None)
            _stream_trail = (
                list(_stream_trail_full)
                if _stream_trail_full and _stream_attempts > 1 else None
            )
            _stream_truncated = getattr(trace, "truncated_dropped", 0) or 0

            # Wrap the fire-and-forget in a try/except so a crash inside
            # _quota_record (e.g. Postgres just went away) doesn't surface
            # as ``Task exception was never retrieved`` in the logs. The
            # global asyncio exception handler also catches it as a
            # safety net; this gives a cleaner per-call error log.
            async def _safe_record(**kwargs):
                try:
                    await _quota_record(**kwargs)
                except Exception as _exc:
                    logger.warning(
                        "stream_quota_record_failed user=%s err=%s",
                        kwargs.get("user_id"), _exc,
                    )

            asyncio.create_task(_safe_record(
                user_id=principal.user_id,
                model_alias=alias,
                provider=_stream_provider,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cost_usd=_stream_cost,
                latency_ms=0.0,
                success=True,
                fallback_messages=body.get("messages") or [],
                fallback_content=joined_content,
                served_by=_stream_served_by,
                attempts=_stream_attempts,
                failover_trail=_stream_trail,
                truncated_dropped=_stream_truncated,
                cache_hit=False,
                **attribution,
            ))
        except Exception:
            pass
