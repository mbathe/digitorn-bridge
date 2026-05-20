"""Monkey-patch action handlers in a BaseModule instance to forward."""
from __future__ import annotations

import logging
from typing import Any

from .client import WorkerClient
from .registry import WorkerEndpoint

logger = logging.getLogger(__name__)

def wrap_module_for_worker(
    module: Any,
    endpoint: WorkerEndpoint,
    *,
    client: WorkerClient | None = None,
) -> int:
    """Replace every action handler in `module._action_registry`."""
    if getattr(module, "_workered_endpoint", None) is not None:
        return 0  # already wrapped

    module_id = getattr(module, "MODULE_ID", "") or ""
    if not module_id:
        raise ValueError(
            f"cannot wrap module with empty MODULE_ID: "
            f"{type(module).__name__}",
        )

    registry = getattr(module, "_action_registry", None)
    if not registry:
        logger.warning(
            "module_has_no_actions module=%s -- nothing to wrap",
            module_id,
        )
        # Still mark it wrapped so the lifecycle loop skips start/stop.
        _stamp_wrapped_state(module, endpoint, client)
        return 0

    if client is None:
        # Reuse the per-endpoint shared client; one `httpx.AsyncClient`
        # per module would stall the loop loading the Windows cert store.
        from .client import get_or_create_client
        client = get_or_create_client(endpoint)

    wrapped_count = 0
    original_handlers: dict[str, Any] = {}
    for action_name in list(registry.keys()):
        entry = registry[action_name]
        original_handler = getattr(entry, "handler", None)
        if original_handler is None:
            continue
        original_handlers[action_name] = original_handler

        proxy_handler = _make_proxy_handler(
            module_id=module_id,
            action_name=action_name,
            client=client,
        )
        entry.handler = proxy_handler

        bound = _bind_proxy_to_module(proxy_handler, module)
        try:
            setattr(module, action_name, bound)
        except (AttributeError, TypeError):
            pass

        wrapped_count += 1

    _stamp_wrapped_state(
        module, endpoint, client, original_handlers=original_handlers,
    )

    logger.info(
        "module_wrapped_for_worker module=%s endpoint=%s actions=%d",
        module_id, endpoint.worker_id, wrapped_count,
    )
    return wrapped_count

def unwrap_module(module: Any) -> int:
    """Restore the original action handlers a previous."""
    originals = getattr(module, "_workered_original_handlers", None)
    if not originals:
        return 0

    registry = getattr(module, "_action_registry", None)
    if not registry:
        return 0

    restored = 0
    for action_name, original_handler in originals.items():
        entry = registry.get(action_name)
        if entry is None:
            continue
        entry.handler = original_handler
        # The class-level handler is unbound; drop the instance
        # attribute so the descriptor protocol rebinds `self`.
        try:
            delattr(module, action_name)
        except AttributeError:
            pass
        restored += 1

    # Clear all the wrap markers so the lifecycle loop can run
    # on_start/on_stop again if the module is reactivated locally.
    for attr in (
        "_workered_endpoint",
        "_workered_client",
        "_workered_original_handlers",
        "_skip_on_start",
        "_skip_on_stop",
    ):
        try:
            delattr(module, attr)
        except AttributeError:
            pass

    logger.info(
        "module_unwrapped module=%s actions_restored=%d",
        getattr(module, "MODULE_ID", "?"), restored,
    )
    return restored

async def push_module_config(
    module_id: str,
    config: dict[str, Any],
    *,
    registry: Any | None = None,
    app_id: str | None = None,
) -> dict[str, bool]:
    """Push a per-app `module.config` block to every worker."""
    if not config:
        return {}

    if registry is None:
        from .registry import get_default_registry
        registry = get_default_registry()

    endpoints = registry.endpoints_for(module_id)
    if not endpoints:
        return {}

    from .client import get_or_create_client

    results: dict[str, bool] = {}
    for ep in endpoints:
        try:
            client = get_or_create_client(ep)
            ok = await client.push_config(module_id, config, app_id=app_id)
            results[ep.worker_id] = ok
        except Exception as exc:
            logger.warning(
                "push_module_config_failed module=%s worker=%s err=%s",
                module_id, ep.worker_id, exc,
            )
            results[ep.worker_id] = False

    logger.info(
        "push_module_config module=%s endpoints=%d ok=%d",
        module_id, len(endpoints), sum(1 for v in results.values() if v),
    )
    return results

def _stamp_wrapped_state(
    module: Any,
    endpoint: WorkerEndpoint,
    client: WorkerClient | None,
    *,
    original_handlers: dict[str, Any] | None = None,
) -> None:
    module._workered_endpoint = endpoint
    module._workered_client = client
    module._workered_original_handlers = dict(original_handlers or {})
    module._skip_on_start = True
    module._skip_on_stop = True

def _rehydrate_action_result(payload: Any) -> Any:
    from digitorn.modules.base import ActionResult

    if isinstance(payload, ActionResult):
        return payload
    if not isinstance(payload, dict):
        return payload
    # Accept both the canonical ActionResult shape and the looser
    # `{success, data, error}` envelope some workers emit.
    if "success" not in payload:
        return payload
    return ActionResult(
        success=bool(payload.get("success")),
        data=payload.get("data"),
        output=payload.get("output"),
        error=payload.get("error"),
        metadata=dict(payload.get("metadata") or {}),
    )

def _bind_proxy_to_module(proxy_handler: Any, module: Any) -> Any:
    import functools as _ft

    async def _bound(*args: Any, **kwargs: Any) -> Any:
        return await proxy_handler(module, *args, **kwargs)

    _bound.__name__ = getattr(proxy_handler, "__name__", "proxy")
    _bound.__qualname__ = getattr(
        proxy_handler, "__qualname__", _bound.__name__,
    )
    _bound._unbound_proxy = proxy_handler  # type: ignore[attr-defined]
    _ft.update_wrapper(_bound, proxy_handler, updated=())
    return _bound

def _make_proxy_handler(
    *,
    module_id: str,
    action_name: str,
    client: WorkerClient,
):
    async def _proxy(module_self: Any, params: Any) -> Any:
        # Convert back to a JSON-serialisable dict for the wire.
        if hasattr(params, "model_dump") and callable(
            getattr(params, "model_dump", None),
        ):
            try:
                args = params.model_dump(mode="python")
            except Exception:
                args = _safe_dict(params)
        elif isinstance(params, dict):
            args = params
        elif params is None:
            args = {}
        else:
            args = _safe_dict(params)

        ctx_payload = _build_ctx_payload(module_self)
        result = await client.call_action(
            module_id, action_name, args, ctx=ctx_payload,
        )
        return _rehydrate_action_result(result)

    _proxy.__name__ = f"proxy_{module_id}_{action_name}"
    _proxy.__qualname__ = _proxy.__name__
    return _proxy

def _safe_dict(obj: Any) -> dict[str, Any]:
    try:
        if hasattr(obj, "__dict__"):
            return {
                k: v for k, v in obj.__dict__.items()
                if not k.startswith("_")
            }
        return dict(obj)
    except Exception:
        return {}

def _build_ctx_payload(module_self: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    try:
        # Late import: keeps the workers package importable without
        # forcing the full `digitorn.modules` namespace at module
        # load. Cheap on the hot path (cached after first call).
        from digitorn.modules.base import BaseModule
        ec = BaseModule._context_var.get()
    except Exception:
        ec = None
    if ec is not None:
        for field in (
            "plan_id", "action_id", "security_profile",
            "agent_id", "session_id", "app_id", "user_id",
            "workspace",
        ):
            val = getattr(ec, field, None)
            if val is not None:
                payload[field] = val
    # Module-level workspace (set from YAML config) is a fallback when
    # the active ExecutionContext doesn't carry one. The session ctx
    # always wins so per-session workspace switching keeps working.
    if "workspace" not in payload:
        workspace = getattr(module_self, "_workspace", None)
        if workspace:
            payload["workspace"] = workspace
    if "app_id" not in payload:
        # `_app_id_override` is stamped on per-app modules by
        # `_inject_app_id_overrides`; fall back to it for tenant routing.
        app_id_fallback = (
            getattr(module_self, "_app_id_override", None)
            or getattr(module_self, "_app_id", None)
        )
        if app_id_fallback and app_id_fallback != "default":
            payload["app_id"] = app_id_fallback
    return payload
