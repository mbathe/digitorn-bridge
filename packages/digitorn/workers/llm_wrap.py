"""Helper to wrap a daemon-side LLM provider with an ``LLMProviderProxy``.

The wrap happens at the seam where the agent's ``ctx.provider`` is
assigned in ``bootstrap.py`` (line 639 per the audit). When workers
host the ``llm_provider`` module, the daemon-side provider object
is replaced with a proxy that streams every ``chat_stream`` call
to the worker process over HTTP/NDJSON.

This module is the ONLY new code on the daemon side for Phase 3.
The integration in ``bootstrap.py`` is a single call: ::

    from digitorn.workers.llm_wrap import maybe_wrap_provider

    provider = _resolve_provider(agent, modules)
    provider = maybe_wrap_provider(provider, agent.brain)  # NEW

If workers don't host ``llm_provider`` or anything goes wrong, the
helper returns the original provider unchanged -- guaranteed
legacy behaviour.
"""
from __future__ import annotations

import logging
from typing import Any

from .proxy import LLMProviderProxy
from .registry import get_default_registry

logger = logging.getLogger(__name__)


def maybe_wrap_provider(provider: Any, brain: Any) -> Any:
    """Return an ``LLMProviderProxy`` wrapping ``provider`` if the
    ``llm_provider`` module is hosted by a worker, otherwise return
    ``provider`` unchanged.

    Inputs:
      * ``provider``: a ``BaseLLMProvider`` instance (already created
        and initialised by the daemon-side llm_provider module).
      * ``brain``: the ``AgentBrain`` (or ``CompiledBrain``) that was
        used to build the provider. Used to derive the ``brain_config``
        dict the worker needs to lazy-init its own copy.

    Failure semantics: any unexpected error logs a WARNING and
    returns the original provider. The agent loop never sees a
    degraded provider -- worst case, the call stays on the main
    loop as before (no regression).
    """
    if provider is None:
        return provider

    # Idempotence: if the provider is already an ``LLMProviderProxy``
    # (e.g. ``resolve_session_provider`` returned the same proxy that
    # was already wrapping the deploy-time provider), don't double-
    # wrap. Returning the proxy as-is is safe and avoids stacking two
    # layers of HTTP forwarding.
    if isinstance(provider, LLMProviderProxy):
        return provider

    try:
        reg = get_default_registry()
        endpoint = reg.route("llm_provider")
        if endpoint is None:
            # llm_provider not workered -- legacy in-process path.
            return provider
    except Exception as exc:
        logger.debug("llm_wrap_route_check_failed: %s", exc)
        return provider

    # Build the brain_config dict in the shape the worker's
    # ``llm_provider._configure_from_dict`` expects. Mirrors the
    # logic of ``LLMProviderModule.create_provider_from_brain``
    # (module.py:600-649) so the worker rebuilds the SAME backend.
    try:
        brain_config = _brain_to_config_dict(brain, provider)
    except Exception as exc:
        logger.warning(
            "llm_wrap_brain_extract_failed provider=%s err=%s -- "
            "falling back to in-process provider",
            getattr(provider, "provider_id", "?"), exc,
        )
        return provider

    # Cache a ProviderInfo snapshot so the proxy's ``get_info()`` is
    # an in-process lookup (no HTTP round trip).
    try:
        info = provider.get_info()
        info_dict = _provider_info_to_dict(info)
    except Exception:
        info_dict = {
            "provider_id": getattr(provider, "provider_id", ""),
            "backend": _detect_backend(provider, brain),
            "model": getattr(provider, "model", ""),
        }

    try:
        proxy = LLMProviderProxy(
            endpoint,
            provider_id=getattr(provider, "provider_id", "") or "",
            model=getattr(provider, "model", "") or "",
            provider_name=(
                getattr(provider, "provider_name", "")
                or _detect_backend(provider, brain)
            ),
            brain_config=brain_config,
            api_key=getattr(provider, "api_key", "") or "",
            base_url=getattr(provider, "base_url", None),
            timeout=float(getattr(provider, "timeout", 120.0) or 120.0),
            max_retries=int(getattr(provider, "max_retries", 2) or 2),
            default_params=dict(
                getattr(provider, "default_params", None) or {}
            ),
            provider_info=info_dict,
            # CRITICAL: keep a reference to the LIVE provider so the
            # proxy can read FRESH credentials on every call. Without
            # this, ``inject_session_time`` hot-swaps (BYOK key,
            # gateway session token, gateway base_url) never reach
            # the worker -- it'd see the deploy-time ``placeholder``
            # api_key frozen at proxy construction and the LLM API
            # returns 401.
            live_provider=provider,
        )
    except Exception as exc:
        logger.warning(
            "llm_wrap_construct_failed provider=%s err=%s -- "
            "falling back to in-process provider",
            getattr(provider, "provider_id", "?"), exc,
        )
        return provider

    # Copy any dynamic markers the bootstrap (or other code) had set
    # on the original provider, so the proxy looks identical to
    # downstream code that reads them.
    _copy_dynamic_markers(provider, proxy)

    # IMPORTANT: do NOT close the daemon-side provider. The proxy
    # holds a ``live_provider`` reference to it specifically so it
    # can read FRESH credentials (api_key, base_url) on every call --
    # ``inject_session_time`` hot-swaps these per session, and we
    # need the live attribute lookup to pick that up. Closing would
    # destroy the SDK client (cheap) but more importantly invalidates
    # the assumption that the live provider stays mutable for
    # session-time injectors.

    logger.info(
        "llm_provider_wrapped_for_worker provider_id=%s model=%s "
        "endpoint=%s",
        proxy.provider_id, proxy.model, endpoint.worker_id,
    )
    return proxy


# ── Internals ────────────────────────────────────────────────────


def _brain_to_config_dict(brain: Any, provider: Any) -> dict[str, Any]:
    """Reduce an ``AgentBrain`` / ``CompiledBrain`` to the flat dict
    shape ``llm_provider._configure_from_dict`` accepts.

    Mirrors ``LLMProviderModule.create_provider_from_brain``
    (module.py:600-649) -- if the brain has ``inline_config`` (a
    ``CompiledBrain``), use it directly; otherwise flatten the
    schema fields.
    """
    # CompiledBrain: flat dict ready.
    inline_config = getattr(brain, "inline_config", None)
    if isinstance(inline_config, dict) and inline_config:
        conf = dict(inline_config)
        default_params = dict(conf.get("default_params") or {})
        for k in ("temperature", "max_tokens", "top_p"):
            v = getattr(brain, k, None)
            if v is not None and k not in default_params:
                default_params[k] = v
        if default_params:
            conf["default_params"] = default_params
        return conf

    # AgentBrain (schema) path: flatten ``config`` + top-level fields.
    #
    # **Priority: live provider attributes WIN over brain.config**.
    # Reason: when the session goes through ``resolve_session_provider``
    # (gateway-routing for shared accounts, BYOK swap), the provider
    # gets a different ``api_key`` / ``base_url`` than what's in
    # ``brain.config``. The worker must rebuild the SAME backend the
    # daemon resolved -- not the deploy-time defaults.
    cfg = dict(getattr(brain, "config", None) or {})
    conf: dict[str, Any] = {
        "backend": _detect_backend(provider, brain) or getattr(
            brain, "backend", None,
        ),
        "model": getattr(provider, "model", "") or getattr(
            brain, "model", "",
        ),
        "api_key": getattr(provider, "api_key", "") or cfg.get(
            "api_key", "",
        ),
        "base_url": getattr(provider, "base_url", None) or cfg.get(
            "base_url",
        ),
        "provider_hint": (
            getattr(provider, "provider_name", None)
            or cfg.get("provider_hint")
            or getattr(brain, "provider", None)
        ),
        "timeout": getattr(provider, "timeout", None) or cfg.get("timeout"),
        "max_retries": getattr(
            provider, "max_retries",
            cfg.get("max_retries", 2),
        ),
    }
    default_params: dict[str, Any] = {}
    for k in ("temperature", "max_tokens", "top_p"):
        v = getattr(brain, k, None)
        if v is not None:
            default_params[k] = v
    # Merge any default_params the provider already had so the worker
    # rebuilds an equivalent SDK client.
    existing_dp = dict(getattr(provider, "default_params", None) or {})
    for k, v in existing_dp.items():
        if k not in default_params:
            default_params[k] = v
    if default_params:
        conf["default_params"] = default_params
    return conf


def _provider_info_to_dict(info: Any) -> dict[str, Any]:
    """Convert a ``ProviderInfo`` dataclass to a JSON-safe dict."""
    if info is None:
        return {}
    if isinstance(info, dict):
        return info
    out: dict[str, Any] = {
        "provider_id": getattr(info, "provider_id", "") or "",
        "backend": getattr(info, "backend", "") or "",
        "model": getattr(info, "model", "") or "",
    }
    caps = getattr(info, "capabilities", None)
    if caps is not None:
        out["capabilities"] = {
            "streaming": bool(getattr(caps, "streaming", True)),
            "tool_use": bool(getattr(caps, "tool_use", False)),
            "vision": bool(getattr(caps, "vision", False)),
            "json_mode": bool(getattr(caps, "json_mode", False)),
            "system_message": bool(getattr(caps, "system_message", True)),
            "max_context_window": int(
                getattr(caps, "max_context_window", 0) or 0,
            ),
            "max_output_tokens": int(
                getattr(caps, "max_output_tokens", 0) or 0,
            ),
        }
    extra = getattr(info, "extra", None)
    if extra:
        out["extra"] = dict(extra)
    return out


def _detect_backend(provider: Any, brain: Any) -> str:
    """Best-effort backend name. The provider classes don't share a
    canonical ``backend`` attribute, so we sniff from common
    locations and fall back to the brain's declared backend.
    """
    for attr in ("backend", "provider_name", "_backend"):
        v = getattr(provider, attr, None)
        if isinstance(v, str) and v:
            return v
    # Class name fallback: AnthropicProvider -> "anthropic",
    # OpenAICompatProvider -> "openai_compat".
    cls_name = type(provider).__name__.lower()
    if "anthropic" in cls_name:
        return "anthropic"
    if "openai" in cls_name:
        return "openai_compat"
    # Brain fallback.
    return str(getattr(brain, "backend", None) or "openai_compat")


def _copy_dynamic_markers(src: Any, dst: Any) -> None:
    """Mirror dynamic attributes the daemon set on the original
    provider onto the proxy. Currently:

      * ``_known_tool_names`` -- set by bootstrap.py line 787 for
        agents that whitelist a subset of tools; the agent loop
        reads it back later to filter.
      * ``_is_clone`` -- clone tagging used by ``agent_spawn``.
    """
    for marker in ("_known_tool_names", "_is_clone"):
        if hasattr(src, marker):
            try:
                setattr(dst, marker, getattr(src, marker))
            except Exception:
                pass


def _close_in_background(provider: Any) -> None:
    """Schedule ``provider.close()`` as a fire-and-forget task so we
    free the daemon-side httpx pool we won't use any more. Never
    raises -- close failures are debug-level noise.
    """
    try:
        import asyncio
        close_coro = provider.close()
        if close_coro is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No loop -- caller is non-async; nothing to do.
        task = loop.create_task(close_coro)
        # Hold a strong reference until done so GC doesn't kill it.
        task.add_done_callback(lambda _t: None)
    except Exception as exc:
        logger.debug("llm_wrap_provider_close_skipped: %s", exc)
