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
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from digitorn_gateway.auth import GatewayPrincipal, init_jwks, require_principal
from digitorn_gateway.config import get_settings
from digitorn_gateway.custom_router import CustomProviderNotImplemented
from digitorn_gateway.db import dispose_engine, get_session_factory, init_engine as init_db
from digitorn_gateway.llm_call import dispatch, dispatch_stream
from digitorn_gateway.models import get_catalog, load_catalog, set_catalog
from digitorn_gateway.models_db import Base
from digitorn_gateway.plans import (
    get_registry as get_plan_registry,
    init_registry as init_plan_registry,
    seed_plans_from_yaml,
)
from digitorn_gateway.quota import UsageRecord, get_engine, init_engine
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

    # Catalogue (model aliases) - in-memory, no DB.
    catalog = load_catalog(settings.models_config_path)
    set_catalog(catalog)

    # Database. Build engine + session factory before any quota-aware
    # subsystem is created.
    init_db()
    factory = get_session_factory()

    # Auto-create tables when running on SQLite (dev). On Postgres the
    # operator runs Alembic migrations - we do NOT create_all there to
    # avoid drift between code and migration files.
    if settings.database_url.startswith("sqlite"):
        async with factory() as db:
            engine_obj = db.bind
            async with engine_obj.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        logger.info("sqlite_schema_created (dev mode auto-bootstrap)")

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

        engine = init_engine(
            session_factory=factory,
            plan_registry=plan_registry,
            flush_interval_seconds=settings.quota_flush_interval_seconds,
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

    logger.info(
        "gateway_started host=%s port=%s models=%d quota=%s",
        settings.host, settings.port, len(catalog.all()),
        "enabled" if settings.quota_enabled else "disabled",
    )

    try:
        yield
    finally:
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass
        if settings.quota_enabled:
            try:
                await get_engine().stop()
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
app.include_router(quota_router)
app.include_router(usage_router)


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
) -> None:
    """Post-call quota record. Updates in-memory counters; DB write
    is amortised by the background flush. Never raises - failures here
    must NOT take the response away from the user."""
    if not get_settings().quota_enabled:
        return
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
        body = json.loads(raw.decode("utf-8"))
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

    stream = bool(body.get("stream"))

    if stream:
        return StreamingResponse(
            _stream_response(body, principal),
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

    # Post-call usage record. Engine handles the async flush.
    await _quota_record(
        user_id=principal.user_id,
        model_alias=usage_record.model_alias,
        provider=usage_record.provider,
        input_tokens=usage_record.input_tokens,
        output_tokens=usage_record.output_tokens,
        cost_usd=usage_record.cost_usd,
        latency_ms=usage_record.latency_ms,
        success=usage_record.success,
    )

    return JSONResponse(resp)


# ── Streaming helpers ──────────────────────────────────────────────


async def _stream_response(
    body: dict[str, Any],
    principal: GatewayPrincipal,
) -> AsyncIterator[bytes]:
    from digitorn_gateway.llm_call import _compute_cost, resolve_alias

    alias = body.get("model", "")
    entry = resolve_alias(alias)

    in_tokens = 0
    out_tokens = 0
    sent_done = False

    try:
        async for chunk in dispatch_stream(body=body):
            usage = chunk.get("usage") if isinstance(chunk, dict) else None
            if usage:
                in_tokens = int(usage.get("prompt_tokens") or in_tokens)
                out_tokens = int(usage.get("completion_tokens") or out_tokens)
            payload = json.dumps(chunk).encode("utf-8")
            yield b"data: " + payload + b"\n\n"
        yield b"data: [DONE]\n\n"
        sent_done = True
    except CustomProviderNotImplemented as exc:
        err = json.dumps({
            "error": {"message": str(exc), "type": "not_implemented", "code": 501},
        }).encode("utf-8")
        yield b"data: " + err + b"\n\n"
        if not sent_done:
            yield b"data: [DONE]\n\n"
    except Exception as exc:
        logger.exception(
            "stream_dispatch_failed user=%s model=%s",
            principal.user_id, alias,
        )
        err = json.dumps({
            "error": {
                "message": f"upstream_error: {type(exc).__name__}: {exc}",
                "type": "upstream_error",
                "code": 502,
            },
        }).encode("utf-8")
        yield b"data: " + err + b"\n\n"
        if not sent_done:
            yield b"data: [DONE]\n\n"
    finally:
        # Always record what reached the client, even on partial failures.
        await _quota_record(
            user_id=principal.user_id,
            model_alias=alias,
            provider=(entry.provider if entry else "unknown"),
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_usd=_compute_cost(entry, in_tokens, out_tokens),
            latency_ms=0.0,
            success=True,
        )
