"""FastAPI routes mounted by the worker app."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from .serializers import dumps

logger = logging.getLogger(__name__)

router = APIRouter()

def _require_auth(authorization: str | None, expected_secret: str) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer")
    token = authorization.removeprefix("Bearer ").strip()
    # String equality is fine: workers are loopback-only with a
    # 32-byte secret. Switch to `hmac.compare_digest` for network exposure.
    if token != expected_secret:
        raise HTTPException(status_code=401, detail="bad bearer")

@router.post("/tool/{module}/{action}")
async def call_tool(
    module: str,
    action: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    """Forward a unary action call to the locally-hosted module."""
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
            app_id=ctx.get("app_id"),
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

    payload = _result_to_payload(result)
    return Response(
        content=dumps(payload),
        media_type="application/json",
    )

def _result_to_payload(result: Any) -> dict[str, Any]:
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
        except Exception as exc:
            logger.debug("routes best-effort block failed: %s", exc)
    # Pydantic v2 model
    if hasattr(result, "model_dump") and callable(
        getattr(result, "model_dump", None),
    ):
        try:
            converted = result.model_dump(mode="python")
            if isinstance(converted, dict):
                return converted
        except Exception as exc:
            logger.debug("routes best-effort block failed: %s", exc)
    # Dataclass fallback
    try:
        import dataclasses
        if dataclasses.is_dataclass(result) and not isinstance(
            result, type,
        ):
            return dataclasses.asdict(result)
    except Exception as exc:
        logger.debug("routes best-effort block failed: %s", exc)
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
        "worker_stream_call module=%s action=%s args_keys=%s",
        module, action, list(args.keys()),
    )

    if module == "llm_provider" and action == "chat_stream":
        return StreamingResponse(
            _llm_chat_stream(state, args, ctx),
            media_type="application/x-ndjson",
        )

    # Unknown streaming action -- 404 with a clear error so the
    # daemon-side proxy doesn't silently hang on an empty stream.
    raise HTTPException(
        status_code=404,
        detail=f"no streaming handler for {module}/{action}",
    )

async def _llm_chat_stream(
    state: Any,
    args: dict[str, Any],
    ctx: dict[str, Any],
):
    try:
        module_instance = state.modules.get("llm_provider")
        if module_instance is None:
            yield dumps({
                "__error__": True,
                "error_type": "ConfigError",
                "error": "llm_provider module not loaded by this worker",
            }) + "\n"
            return

        provider_id = str(args.get("provider_id") or "")
        if not provider_id:
            yield dumps({
                "__error__": True,
                "error_type": "ConfigError",
                "error": "provider_id is required",
            }) + "\n"
            return

        # Lazy provider creation + credential-freshness check: the
        # daemon hot-swaps api_key / base_url per session, so the
        # worker rebuilds when the incoming creds differ from cache.
        brain_config = args.get("brain_config") or {}
        provider = module_instance._providers.get(provider_id)

        def _creds_changed(existing: Any, cfg: dict[str, Any]) -> bool:
            for attr, key in (
                ("api_key", "api_key"),
                ("base_url", "base_url"),
                ("model", "model"),
            ):
                incoming = cfg.get(key)
                current = getattr(existing, attr, None)
                # Only compare when both sides have a value -- empty
                # incoming means "no override; reuse cached".
                if incoming and current != incoming:
                    return True
            return False

        if provider is not None and brain_config:
            if _creds_changed(provider, brain_config):
                logger.info(
                    "llm_worker_creds_changed provider=%s -- rebuilding",
                    provider_id,
                )
                provider = None  # force reconfigure below

        if provider is None:
            if not brain_config:
                yield dumps({
                    "__error__": True,
                    "error_type": "ConfigError",
                    "error": (
                        f"provider {provider_id!r} not configured and "
                        f"no brain_config provided to lazy-init"
                    ),
                }) + "\n"
                return
            try:
                await module_instance._configure_from_dict(
                    provider_id, brain_config,
                )
                provider = module_instance._providers.get(provider_id)
            except Exception as exc:
                logger.exception(
                    "llm_worker_lazy_configure_failed provider=%s",
                    provider_id,
                )
                yield dumps({
                    "__error__": True,
                    "error_type": type(exc).__name__,
                    "error": f"failed to configure provider: {exc}",
                }) + "\n"
                return

        if provider is None:
            yield dumps({
                "__error__": True,
                "error_type": "ConfigError",
                "error": f"provider {provider_id!r} unavailable after configure",
            }) + "\n"
            return

        from digitorn.modules.llm_provider.providers.base import (
            ChatMessage,
        )
        messages_in = args.get("messages") or []
        messages: list[ChatMessage] = []
        for m in messages_in:
            if not isinstance(m, dict):
                continue
            messages.append(ChatMessage(
                role=str(m.get("role") or "user"),
                content=m.get("content") or "",
                name=m.get("name"),
                tool_call_id=m.get("tool_call_id"),
                tool_calls=m.get("tool_calls"),
                reasoning_content=m.get("reasoning_content"),
            ))

        tools = args.get("tools") or None
        gen_params = args.get("gen_params") or {}

        # Restore the daemon RequestContext on this worker's contextvar
        # so the provider can substitute the real bearer + identity headers.
        rc_token = _restore_request_context(args.get("request_ctx"))

        try:
            async for chunk in provider.chat_stream(
                messages, tools=tools, **gen_params,
            ):
                yield dumps(_chunk_to_dict(chunk)) + "\n"
        except asyncio.CancelledError:
            # Daemon side closed the stream early (e.g. user aborted
            # the agent turn). Don't emit an error sentinel -- the
            # daemon already moved on; surfacing it would be noise.
            raise
        except Exception as exc:
            logger.exception(
                "llm_worker_chat_stream_failed provider=%s",
                provider_id,
            )
            yield dumps({
                "__error__": True,
                "error_type": type(exc).__name__,
                "error": str(exc) or repr(exc),
            }) + "\n"
            return
        finally:
            _reset_request_context(rc_token)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Top-level guard -- something exotic blew up (auth, missing
        # state). Always send a clean error sentinel.
        logger.exception("llm_worker_chat_stream_top_level_error")
        yield dumps({
            "__error__": True,
            "error_type": type(exc).__name__,
            "error": str(exc) or repr(exc),
        }) + "\n"

def _chunk_to_dict(chunk: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "delta": getattr(chunk, "delta", "") or "",
    }
    finish = getattr(chunk, "finish_reason", None)
    if finish:
        out["finish_reason"] = finish
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        out["usage"] = {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(
                getattr(usage, "completion_tokens", 0) or 0,
            ),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            "cache_read_tokens": int(
                getattr(usage, "cache_read_tokens", 0) or 0,
            ),
            "cache_creation_tokens": int(
                getattr(usage, "cache_creation_tokens", 0) or 0,
            ),
        }
    tool_calls = getattr(chunk, "tool_calls", None)
    if tool_calls:
        out["tool_calls"] = tool_calls
    thinking = getattr(chunk, "thinking", None)
    if thinking:
        out["thinking"] = thinking
    # OpenAI-compat per-chunk `tool_call` singular -- some chunks
    # carry partial tool-call deltas this way (see
    # `openai_compat.py` line 1391-1411 cited in the audit).
    tool_call = getattr(chunk, "tool_call", None)
    if tool_call:
        out["tool_call"] = tool_call
    return out

def _restore_request_context(rc_dict: Any) -> Any:
    if not isinstance(rc_dict, dict):
        return None
    try:
        from digitorn.core.runtime.request_context import (
            RequestContext, set_request_context,
        )
        rc = RequestContext(
            user_id=rc_dict.get("user_id"),
            app_id=rc_dict.get("app_id"),
            session_id=rc_dict.get("session_id"),
            run_id=rc_dict.get("run_id"),
            agent_id=rc_dict.get("agent_id"),
            user_jwt=rc_dict.get("user_jwt"),
        )
        return set_request_context(rc)
    except Exception as exc:
        logger.debug("worker_request_ctx_restore_failed: %s", exc)
        return None

def _reset_request_context(token: Any) -> None:
    if token is None:
        return
    try:
        from digitorn.core.runtime.request_context import (
            reset_request_context,
        )
        reset_request_context(token)
    except Exception as exc:
        logger.debug("worker_request_ctx_reset_failed: %s", exc)

@router.get("/modules")
async def list_modules(request: Request) -> dict[str, Any]:
    """Public (no-auth) introspection. The supervisor uses it."""
    state = request.app.state
    return {
        "worker_id": getattr(state, "worker_id", "unknown"),
        "modules": sorted(state.hosted_modules),
        "phase": getattr(state, "phase", "1-skeleton"),
    }

@router.post("/admin/config/{module}")
async def push_config(
    module: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    """Apply a per-app `module.config` block on a workered module."""
    state = request.app.state
    _require_auth(authorization, state.shared_secret)
    body = await request.json()
    config: dict[str, Any] = body.get("config") or {}
    app_id: str | None = body.get("app_id")

    if module not in state.hosted_modules:
        raise HTTPException(
            status_code=404,
            detail=f"module {module!r} not hosted by this worker",
        )
    module_instance = state.modules.get(module)
    if module_instance is None:
        return Response(
            content=dumps({
                "success": False,
                "error": (
                    f"module {module!r} listed but not loaded "
                    f"-- check worker startup logs"
                ),
            }),
            media_type="application/json",
        )

    logger.info(
        "worker_config_push module=%s app=%s keys=%s",
        module, app_id, sorted(config.keys()),
    )
    try:
        # Forward `app_id` only when the module accepts it.
        import inspect as _inspect
        sig = _inspect.signature(module_instance.on_config_update)
        if app_id is not None and "app_id" in sig.parameters:
            await module_instance.on_config_update(config, app_id=app_id)
        else:
            await module_instance.on_config_update(config)
    except Exception as exc:
        logger.exception(
            "worker_on_config_update_failed module=%s",
            module,
        )
        return Response(
            content=dumps({
                "success": False,
                "error": (
                    f"{type(exc).__name__}: {exc}"
                ),
            }),
            media_type="application/json",
        )

    return Response(
        content=dumps({"success": True}),
        media_type="application/json",
    )

@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Always returns 200 with a small JSON status. The supervisor."""
    state = request.app.state
    import time as _time
    return {
        "status": "ok",
        "worker_id": getattr(state, "worker_id", "unknown"),
        "modules": sorted(state.hosted_modules),
        "uptime_s": round(_time.monotonic() - state.started_at, 1),
        "phase": getattr(state, "phase", "1-skeleton"),
    }
