"""FastAPI routes mounted by the worker app.

Three endpoints (plus the standard ``/health``):

  * ``POST /tool/{module}/{action}`` -- unary action call. Body:
    ``{"args": {...}, "ctx": {...}}``. Response: serialised
    ``ActionResult`` payload (success / error / data / metadata).

  * ``POST /stream/{module}/{action}`` -- streaming action (LLM
    chat_stream, file_extract on big PDFs, ...). Response: NDJSON
    chunked stream; the client parses one JSON object per line.

  * ``GET /modules`` -- introspection: which modules and actions
    this worker hosts. Used by the supervisor and the routing
    table healthchecks.

Phase 1 status: routes return placeholder responses so the FastAPI
app boots cleanly. The actual module dispatch happens in Phase 2
once we agree on how AgentContext is serialised across the boundary.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from .serializers import dumps

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_auth(authorization: str | None, expected_secret: str) -> None:
    """Constant-time check of the shared secret. The worker binds to
    127.0.0.1 by default so the loopback restriction is the primary
    defense; the bearer check is defense-in-depth against another
    local process on the same machine.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer")
    token = authorization.removeprefix("Bearer ").strip()
    # NB: this is a string equality, not constant-time. The worker is
    # loopback-only, the secret is 32 random bytes -- a timing attack
    # would need millions of trips. Keep it simple for now; switch to
    # ``hmac.compare_digest`` if we ever expose workers over network.
    if token != expected_secret:
        raise HTTPException(status_code=401, detail="bad bearer")


@router.post("/tool/{module}/{action}")
async def call_tool(
    module: str,
    action: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    """Forward a unary action call to the locally-hosted module.

    Dispatch path (mirrors the daemon's tool_exec):
      1. Auth check (Bearer shared secret).
      2. Lookup the module in ``app.state.modules`` (populated by the
         lifespan loader).
      3. Reconstruct an ``ExecutionContext`` from the ``ctx`` envelope
         the daemon sent (plan_id, action_id, security_profile,
         session_id, user_id, workspace, ...).
      4. ``await module.execute(action, args, context=ec)`` -- the
         same entry point the daemon uses. Pydantic validation,
         security gates, and cache logic run inside ``execute``.
      5. Serialise the result (``ActionResult`` dataclass, dict, or
         arbitrary value) into JSON for the wire.

    Any exception is caught and returned as a JSON-payload error
    with HTTP 200 -- the proxy on the daemon side expects
    ActionResult-shaped responses, not HTTP errors.
    """
    state = request.app.state
    _require_auth(authorization, state.shared_secret)

    body = await request.json()
    args: dict[str, Any] = body.get("args") or {}
    ctx: dict[str, Any] = body.get("ctx") or {}

    if module not in state.hosted_modules:
        raise HTTPException(
            status_code=404,
            detail=f"module {module!r} not hosted by this worker",
        )

    module_instance = state.modules.get(module)
    if module_instance is None:
        # The module was listed but failed to load / instantiate at
        # boot. Surface a clear error rather than a generic 500.
        return Response(
            content=dumps({
                "success": False,
                "error": (
                    f"module {module!r} listed in worker config but "
                    f"not loaded -- check worker startup logs for "
                    f"on_start failures."
                ),
            }),
            media_type="application/json",
        )

    logger.debug(
        "worker_tool_call module=%s action=%s args_keys=%s",
        module, action, list(args.keys()),
    )

    # Reconstruct an ExecutionContext from the daemon-side envelope.
    # The frozen-dataclass shape matches what BaseModule.execute()
    # expects for security gates + per-action policy.
    try:
        from digitorn.modules.base import ExecutionContext
        exec_ctx = ExecutionContext(
            plan_id=str(ctx.get("plan_id") or f"worker:{module}"),
            action_id=str(ctx.get("action_id") or f"{module}.{action}"),
            service_bus=state.service_bus,
            session_id=ctx.get("session_id"),
            user_id=str(ctx.get("user_id") or "admin"),
            workspace=ctx.get("workspace"),
            security_profile=ctx.get("security_profile"),
        )
    except Exception as exc:
        logger.warning(
            "worker_ctx_build_failed module=%s action=%s err=%s",
            module, action, exc,
        )
        exec_ctx = None

    try:
        result = await module_instance.execute(action, args, context=exec_ctx)
    except Exception as exc:
        logger.exception(
            "worker_dispatch_unhandled module=%s action=%s",
            module, action,
        )
        return Response(
            content=dumps({
                "success": False,
                "error": (
                    f"worker dispatch raised {type(exc).__name__}: {exc}"
                ),
            }),
            media_type="application/json",
        )

    # Normalise the response to a JSON-safe dict. ActionResult
    # dataclass -> .to_dict; Pydantic model -> .model_dump; bare
    # value -> wrap in success envelope so the daemon side always
    # sees a consistent shape.
    payload = _result_to_payload(result)
    return Response(
        content=dumps(payload),
        media_type="application/json",
    )


def _result_to_payload(result: Any) -> dict[str, Any]:
    """Convert any ``module.execute(...)`` return value to a JSON-
    serialisable dict the daemon-side proxy can consume.
    """
    if result is None:
        return {"success": True, "data": None}
    if isinstance(result, dict):
        return result
    # ActionResult dataclass
    if hasattr(result, "to_dict") and callable(
        getattr(result, "to_dict", None),
    ):
        try:
            converted = result.to_dict()
            if isinstance(converted, dict):
                return converted
        except Exception:
            pass
    # Pydantic v2 model
    if hasattr(result, "model_dump") and callable(
        getattr(result, "model_dump", None),
    ):
        try:
            converted = result.model_dump(mode="python")
            if isinstance(converted, dict):
                return converted
        except Exception:
            pass
    # Dataclass fallback
    try:
        import dataclasses
        if dataclasses.is_dataclass(result) and not isinstance(
            result, type,
        ):
            return dataclasses.asdict(result)
    except Exception:
        pass
    return {"success": True, "data": result}


@router.post("/stream/{module}/{action}")
async def stream_tool(
    module: str,
    action: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    """NDJSON-framed streaming action. One JSON object per line."""
    state = request.app.state
    _require_auth(authorization, state.shared_secret)

    body = await request.json()
    args: dict[str, Any] = body.get("args") or {}
    ctx: dict[str, Any] = body.get("ctx") or {}

    if module not in state.hosted_modules:
        raise HTTPException(
            status_code=404,
            detail=f"module {module!r} not hosted by this worker",
        )

    logger.debug(
        "worker_stream_call module=%s action=%s args_keys=%s ctx_keys=%s",
        module, action, list(args.keys()), list(ctx.keys()),
    )

    async def _chunks():
        # PHASE 3 INSERTION POINT: forward provider SSE chunks here.
        # The skeleton emits a single placeholder chunk so the
        # transport plumbing can be smoke-tested end-to-end.
        yield dumps({
            "_phase": "1-skeleton",
            "module": module,
            "action": action,
            "message": "streaming wiring placeholder",
        }) + "\n"

    return StreamingResponse(
        _chunks(),
        media_type="application/x-ndjson",
    )


@router.get("/modules")
async def list_modules(request: Request) -> dict[str, Any]:
    """Public (no-auth) introspection. The supervisor uses it to
    confirm a worker hosts what its config says it should.
    """
    state = request.app.state
    return {
        "worker_id": getattr(state, "worker_id", "unknown"),
        "modules": sorted(state.hosted_modules),
        "phase": getattr(state, "phase", "1-skeleton"),
    }


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Always returns 200 with a small JSON status. The supervisor
    polls this; clients (proxies) call it on startup to discover
    capabilities.
    """
    state = request.app.state
    import time as _time
    return {
        "status": "ok",
        "worker_id": getattr(state, "worker_id", "unknown"),
        "modules": sorted(state.hosted_modules),
        "uptime_s": round(_time.monotonic() - state.started_at, 1),
        "phase": getattr(state, "phase", "1-skeleton"),
    }
