"""Monkey-patch action handlers in a BaseModule instance to forward
calls over HTTP to a worker.

The instance stays a real BaseModule -- its manifest, ActionSpecs,
params_models, constraints, service_bus registration, and even its
class identity are untouched. We mutate exactly one thing per action:
the ``handler`` callable inside its ``_action_registry`` dict.

Why this approach
=================

Hard constraints from the architecture review:
  1. No module source code is modified.
  2. When ``workers.enabled`` is False (default), zero behavioural
     change: ``wrap_module_for_worker`` is never invoked.
  3. All call paths route through the worker transparently -- not
     just ``tool_exec``. Hooks, REST ``/api/modules``, middleware,
     and channels all go through the same instance, so wrapping the
     in-memory ``_action_registry`` covers them by construction.

What stays in the daemon
========================

  * Pydantic ``params_model`` validation runs **before** the proxy
    is called (BaseModule.execute() validates at line 1115). So
    malformed args are rejected at the daemon boundary, not after
    a wasted HTTP round-trip.
  * Security gates run before the proxy too. Permission denied is
    still surfaced as ``ActionResult(success=False)`` without
    leaving the daemon.
  * L2 cache check runs before the proxy -- a cached result skips
    the HTTP call entirely.

What moves to the worker
========================

  * Action implementation (the @action method body).
  * Module lifecycle: ``on_start`` / ``on_stop`` / ``on_config_update``
    -- the daemon-side instance has them skipped via the
    ``_skip_on_start`` / ``_skip_on_stop`` markers; the bootstrap
    lifecycle loop honours those.
  * Any local state the module holds (e.g. ``shell.BackgroundTask``
    dict, ``rag.collections`` map). The daemon-side instance is
    effectively hollow -- it serves as a routing handle only.
"""
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
    """Replace every action handler in ``module._action_registry``
    with an async HTTP forwarder targeting ``endpoint``. Mutates the
    module instance in place.

    Returns the number of actions actually wrapped (useful for
    sanity logs / smoke tests).

    Side-effects on the module:
      * ``module._workered_endpoint = endpoint``
      * ``module._workered_client = client``
      * ``module._skip_on_start = True``
      * ``module._skip_on_stop = True``

    Idempotent: a second call on an already-wrapped module is a
    no-op (the existing wrapping is kept). To re-wrap with a new
    endpoint, call ``unwrap_module(module)`` first.
    """
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
        # Use the per-endpoint shared client. Building a fresh
        # ``WorkerClient`` per workered module would trigger an
        # ``httpx.AsyncClient.__init__`` per module -- which loads
        # the Windows cert store synchronously and stalls the main
        # loop for 200ms-3s per call. See ``client.py`` docstring.
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
        # ActionEntry is a @dataclass (modules/decorators.py:60) so
        # direct field assignment works. We leave entry.spec and
        # entry.params_model untouched so Pydantic validation +
        # security gates still execute on the daemon side BEFORE
        # the HTTP call.
        entry.handler = proxy_handler

        # @action(...) also installs the wrapper as a class attribute
        # (decorators.py:309). Some callers reach the action directly
        # via ``module.bash(...)`` instead of going through
        # ``execute()``; mirror the swap there too. Falls back
        # gracefully when the attribute is a read-only descriptor.
        # The proxy is ``async def _proxy(module_self, params)`` -- an
        # unbound function. Installing it as an instance attribute
        # skips Python's descriptor protocol, so ``module.action(p)``
        # would call ``_proxy(p)`` with one positional arg and
        # blow up with "missing 1 required argument: 'params'". Bind
        # ``module`` here so direct callers see a normal one-arg API.
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
    """Restore the original action handlers a previous
    ``wrap_module_for_worker`` swapped out. Useful for tests and for
    hot-swap-back when a worker is taken offline by the supervisor.

    Returns the number of actions actually restored.
    """
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
        # Original handler is the class-level wrapper (decorators.py:285),
        # which is unbound. Drop the instance attribute so attribute
        # lookup falls through to the class and the descriptor protocol
        # rebinds it as a method again -- otherwise direct callers like
        # ``module.action(params)`` would lose ``self``.
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
) -> dict[str, bool]:
    """Push a per-app ``module.config`` block to every worker that
    hosts ``module_id``.

    The wire path: ``WorkerClient.push_config`` → worker's
    ``POST /admin/config/{module}`` → worker calls
    ``module.on_config_update(config)``. This is what makes workered
    modules behave like in-process ones: a YAML's
    ``modules.lsp.config.python: "ruff ..."`` actually reaches the
    LSP instance that handles the call, instead of being silently
    dropped by the bootstrap loop's ``_skip_on_start`` short-circuit.

    Multi-replica safe: pushes to ALL endpoints (registry's
    ``endpoints_for``), not just the round-robin pick from
    ``route()``. Without that, scaled-out modules would have
    stale-config replicas serving stale results.

    Returns ``{worker_id: success_bool}``. Caller logs the map but
    does NOT abort the deploy on partial failure -- the workered
    module falls back to its on_start defaults (auto-detect for LSP,
    catalog-only for MCP, etc.), same behaviour as today.
    """
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
            ok = await client.push_config(module_id, config)
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


# ---- internals -----------------------------------------------------


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
    """Convert the worker's JSON-decoded response back into an
    ``ActionResult``. Daemon-side callers (REST endpoints, hooks,
    behaviour engine) reach for ``.success`` / ``.error`` / ``.data``
    -- a bare ``dict`` would crash them with ``AttributeError``.

    Non-dict / non-ActionResult payloads pass through untouched so
    custom return types (stream chunks, raw bytes) still work.
    """
    from digitorn.modules.base import ActionResult

    if isinstance(payload, ActionResult):
        return payload
    if not isinstance(payload, dict):
        return payload
    # Accept both the canonical ActionResult shape and the looser
    # ``{success, data, error}`` envelope some workers emit.
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
    """Bind ``module`` as the first positional argument of an unbound
    proxy. Returns a one-arg coroutine that mirrors what callers expect
    from a class-defined method on ``module`` (post-descriptor binding).
    """
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
    """Build the unbound async handler installed in
    ``ActionEntry.handler``.

    BaseModule's ``_get_handler`` (base.py:1015-1029) calls
    ``await unbound(module_self, params)`` where ``params`` is either:
      * an already-validated Pydantic model instance (when the
        action declared a ``params_model``), or
      * the raw dict (when no ``params_model``).

    Both shapes are handled below.
    """
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
    """Best-effort cast of an unknown object to a dict. Falls back to
    an empty dict when there's nothing iterable. Never raises -- the
    proxy boundary must not produce surprise exceptions for the agent
    loop's error handler.
    """
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
    """Snapshot the active ExecutionContext fields the worker needs.

    Phase 2 keeps this envelope minimal. Richer fields (full
    AgentContext, workspace permissions, security profile body) are
    added in Phase 3 when the worker-side dispatch is wired in.
    """
    payload: dict[str, Any] = {}
    try:
        # Late import: keeps the workers package importable without
        # forcing the full ``digitorn.modules`` namespace at module
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
    return payload
