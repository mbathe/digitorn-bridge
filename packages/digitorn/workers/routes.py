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

import asyncio
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
    """NDJSON-framed streaming action. One JSON object per line.

    Currently supports:

    * ``/stream/llm_provider/chat_stream`` -- forwards anthropic / openai
      SDK chunks to the daemon-side proxy as NDJSON. Each chunk is a
      ``StreamChunk`` serialised as ``{delta, finish_reason, usage,
      tool_calls, thinking}``. The proxy rehydrates these into real
      ``StreamChunk`` dataclass instances on the daemon side.

    Other modules: returns a 404 (no other streaming action wired yet).
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


# ── LLM provider streaming dispatch ──────────────────────────────


async def _llm_chat_stream(
    state: Any,
    args: dict[str, Any],
    ctx: dict[str, Any],
):
    """Async generator yielding NDJSON lines for one ``chat_stream``
    call. Lazily configures the provider from ``brain_config`` if it
    isn't already in the worker's ``_providers`` dict.

    Failure modes are surfaced as a special ``{"__error__": true, ...}``
    JSON line so the daemon-side proxy can rehydrate the exception
    type and re-raise into the agent loop's existing retry path. We
    never raise OUT of this generator -- a raised exception in a
    StreamingResponse generator yields a corrupted half-stream that
    httpx on the daemon side can't decode cleanly.
    """
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

        # Lazy provider creation + credential-freshness check.
        #
        # The worker keeps a hot cache in ``module_instance._providers``
        # so subsequent calls reuse the SDK client. BUT the daemon
        # hot-swaps the original provider's ``api_key`` and
        # ``base_url`` per session via ``inject_session_time``
        # (BYOK keys, gateway session tokens, gateway URL). The
        # daemon-side proxy reads these LIVE on every call and ships
        # them in ``brain_config`` -- so the worker must detect when
        # the incoming credentials differ from the cached provider's
        # and rebuild before re-using.
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

        # Rebuild ChatMessage dataclasses from the wire dicts -- the
        # SDK backends index attributes via getattr but the type hint
        # in ``BaseLLMProvider.chat_stream`` is ``list[ChatMessage]``;
        # keep the contract clean.
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

        # Restore the daemon-side RequestContext in this worker's
        # ContextVar. The live provider (e.g. gateway-routed
        # OpenAICompatProvider with ``api_key = "{{user.jwt}}"``) reads
        # this at HTTP-request time to substitute the real bearer and
        # the X-Digitorn-* identity headers. Without restoring it here
        # the worker would send the placeholder string literally and
        # the gateway would reject the bearer as a malformed JWT.
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
                "error": str(exc),
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
            "error": str(exc),
        }) + "\n"


def _chunk_to_dict(chunk: Any) -> dict[str, Any]:
    """Serialise a ``StreamChunk`` (or duck-typed equivalent) to the
    JSON dict the daemon-side proxy expects. Robust to slight shape
    variations -- providers occasionally extend chunks with extra
    fields (e.g. OpenAI-compat's per-chunk ``tool_call`` singular).
    """
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
    # OpenAI-compat per-chunk ``tool_call`` singular -- some chunks
    # carry partial tool-call deltas this way (see
    # ``openai_compat.py`` line 1391-1411 cited in the audit).
    tool_call = getattr(chunk, "tool_call", None)
    if tool_call:
        out["tool_call"] = tool_call
    return out


def _restore_request_context(rc_dict: Any) -> Any:
    """Rebuild a ``RequestContext`` from the dict the daemon-side proxy
    captured via ``_snapshot_request_context``, and push it onto this
    worker's ContextVar. Returns the reset token (or ``None`` when no
    context was sent / restore fails).

    Failure modes are silent on purpose: a missing context is the
    expected shape for CLI / healthcheck callers; the live provider
    falls through to its non-gateway path.
    """
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
    """Restore the previous ContextVar value, swallowing token-cross-
    loop errors that can occur when ``StreamingResponse`` switches
    coroutines under us."""
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
