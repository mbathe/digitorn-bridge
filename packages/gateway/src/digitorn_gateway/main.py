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
            await seed_if_empty(factory, catalog=catalog)
        except Exception as exc:
            logger.warning("bootstrap_seed_failed (continuing): %s", exc)
        try:
            cache = get_config_cache()
            stats = await cache.reload_from_db(factory)
            await start_config_cache_refresh(factory)
            await start_oauth_refresh(factory)
            await start_route_probe()
            config_cache_loaded = True
            logger.info("config_cache_loaded %s", stats)
        except Exception as exc:
            logger.warning(
                "config_cache_load_failed (legacy fallback active): %s", exc,
            )

    # Plans: seed defaults from YAML, then load into in-memory registry.
    if settings.quota_enabled:
        try:
            await seed_plans_from_yaml(
                session_factory=factory,
                yaml_path=settings.quota_plans_seed_path,
            )
        except Exception as exc:
            logger.warning("plans_seed_failed (continuing): %s", exc)

        plan_registry = init_plan_registry(
            session_factory=factory,
            default_plan_name=settings.quota_default_plan_name,
            user_cache_ttl_seconds=settings.quota_plan_cache_ttl_seconds,
        )
        await plan_registry.reload_plans()

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
        try:
            await engine.recover_from_db()
        except Exception as exc:
            logger.warning("quota_recover_failed (continuing fresh): %s", exc)
        engine.start()
    else:
        logger.info("quota_disabled by config")

    # Auth JWKS - fetch at boot, then refresh periodically.
    jwks = init_jwks(settings.auth_jwks_url)
    try:
        await jwks.fetch()
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
    from digitorn_gateway import partition_keeper as _pk
    partition_task = asyncio.create_task(
        _pk.run(), name="gateway-partition-keeper",
    )
    app.state.partition_task = partition_task

    # v2: enforce audit_actions_catalog.retention_days nightly.
    from digitorn_gateway import retention_keeper as _rk
    retention_task = asyncio.create_task(
        _rk.run(), name="gateway-retention-keeper",
    )
    app.state.retention_task = retention_task

    logger.info(
        "gateway_started host=%s port=%s models=%d quota=%s",
        settings.host, settings.port, len(catalog.all()),
        "enabled" if settings.quota_enabled else "disabled",
    )

    try:
        yield
    finally:
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
        for t in (refresh_task, partition_task, retention_task):
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
app.include_router(admin_writable_router)
app.include_router(oauth_login_router)
app.include_router(quota_router)
app.include_router(usage_router)
app.include_router(dashboard_router)
app.include_router(admin_config_router)


# ── Health ─────────────────────────────────────────────────────────


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# ── Models ─────────────────────────────────────────────────────────


@app.get("/v1/models")
async def list_models(
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    catalog = get_catalog()
    items = [
        {
            "id": alias,
            "object": "model",
            "created": 0,
            "owned_by": entry.provider,
            "max_context_tokens": entry.max_context_tokens,
        }
        for alias, entry in catalog.all().items()
    ]
    return {"object": "list", "data": items}


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
) -> None:
    """Post-call accounting. Updates the in-memory quota counter (DB
    flush is amortised by the background loop) AND writes one row to
    ``gateway_usage_events`` so the dashboard / admin endpoints have a
    full per-call audit trail. Never raises.

    Designed to run via FastAPI ``BackgroundTask`` so it never blocks
    the response back to the daemon - the LLM result reaches the
    caller and accounting happens in parallel.
    """
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
    )


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
                "category": "billing",
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

    stream = bool(body.get("stream"))

    if stream:
        return StreamingResponse(
            _stream_response(body, principal, attribution),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        resp, usage_record = await dispatch(body=body)
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
    return JSONResponse(
        resp,
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
            **attribution,
        ),
    )


# ── Streaming helpers ──────────────────────────────────────────────


async def _stream_response(
    body: dict[str, Any],
    principal: GatewayPrincipal,
    attribution: dict[str, str | None] | None = None,
) -> AsyncIterator[bytes]:
    from digitorn_gateway.llm_call import _compute_cost, resolve_alias

    alias = body.get("model", "")
    entry = resolve_alias(alias)

    attribution = attribution or {}
    in_tokens = 0
    out_tokens = 0
    sent_done = False

    try:
        async for chunk in dispatch_stream(body=body):
            usage = chunk.get("usage") if isinstance(chunk, dict) else None
            if usage:
                in_tokens = int(usage.get("prompt_tokens") or in_tokens)
                out_tokens = int(usage.get("completion_tokens") or out_tokens)
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
        # Always record what reached the client, even on partial failures.
        # The streaming generator already drained, so the daemon has its
        # data; we can afford to await this here. The single ``await``
        # is on the order of a few ms (in-memory + best-effort INSERT).
        await _quota_record(
            user_id=principal.user_id,
            model_alias=alias,
            provider=(entry.provider if entry else "unknown"),
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_usd=_compute_cost(entry, in_tokens, out_tokens),
            latency_ms=0.0,
            success=True,
            **attribution,
        )
