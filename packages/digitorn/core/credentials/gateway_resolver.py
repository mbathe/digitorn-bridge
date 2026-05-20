"""Decide whether to route an outbound LLM call through the digitorn gateway or keep the app's deployed provider."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Providers that ALWAYS run on the user's own machine - never proxy.
LOCAL_PROVIDERS: frozenset[str] = frozenset({
    "ollama",
    "lm_studio",
    "vllm",
    "llama_cpp",
    "lmstudio",
})

# Pseudo-user IDs that mean "no real authenticated user". The runtime
# trusts the YAML for these (CLI, internal turns, healthcheck).
NON_USER_IDS: frozenset[str] = frozenset({
    "", "local", "anonymous", "system", "admin",
})


_PROVIDER_CACHE: dict[tuple[str, str, Any], Any] = {}
_PROVIDER_CACHE_LOCK = asyncio.Lock()


async def resolve_session_provider(
    *,
    deployed_provider: Any,
    agent: Any,
    user_id: Optional[str],
    app_id: str,
    modules: dict[str, Any],
    settings: Any,
    byok_enabled: bool = False,
) -> Any:
    """Return the LLM provider the runtime should use for this session."""
    brain = getattr(agent, "brain", None)
    if brain is None:
        return deployed_provider

    if byok_enabled:
        logger.info(
            "session_provider: KEEP (BYOK enabled, app=%s user=%s)",
            app_id, user_id,
        )
        return deployed_provider

    # Rule 2: gateway disabled at the daemon level (local-only deploy).
    if not getattr(settings.runtime, "gateway_enabled", True):
        logger.debug(
            "session_provider: KEEP (gateway disabled, app=%s)", app_id,
        )
        return deployed_provider

    # Rule 3: no authenticated user (CLI / healthcheck / internal).
    user_id_norm = (user_id or "").strip().lower()
    if user_id_norm in NON_USER_IDS:
        logger.debug(
            "session_provider: KEEP (anonymous / local user, app=%s)", app_id,
        )
        return deployed_provider

    # Rule 4: brain runs locally on the user's machine.
    real_provider = _resolve_brain_provider_name(brain, deployed_provider)
    if real_provider in LOCAL_PROVIDERS:
        logger.debug(
            "session_provider: KEEP (local provider %s, app=%s)",
            real_provider, app_id,
        )
        return deployed_provider

    llm_module = modules.get("llm_provider")
    if llm_module is None:
        logger.warning(
            "session_provider: KEEP (llm_provider module missing, app=%s)",
            app_id,
        )
        return deployed_provider

    return await _build_gateway_provider(
        brain=brain, deployed_provider=deployed_provider, settings=settings,
    )


def _resolve_brain_provider_name(brain: Any, deployed_provider: Any) -> str:
    """Return the canonical provider name (e.g. `deepseek`) for the"""
    # 1) AgentBrain (raw schema) carries `provider` directly.
    direct = getattr(brain, "provider", "") or ""
    if direct:
        return str(direct).lower()
    # 2) CompiledBrain inline form.
    inline = getattr(brain, "inline_config", None)
    if isinstance(inline, dict):
        pname = inline.get("provider") or inline.get("provider_hint") or ""
        if pname:
            return str(pname).lower()
    pid = (getattr(brain, "provider_id", "") or "").lower()
    if pid:
        # Deployed provider's hint is the most authoritative source
        # when the provider was instantiated from a named block.
        hint = (
            getattr(deployed_provider, "provider_hint", "")
            or getattr(deployed_provider, "provider_id", "")
            or ""
        ).lower()
        if hint and hint in {
            "anthropic", "openai", "openai_compat", "deepseek",
            "gemini", "google", "mistral", "groq", "cohere",
            "azure", "xai", "perplexity", "github_copilot",
            "ollama", "lm_studio", "vllm",
        }:
            return hint
        return pid
    # 4) Last-resort: deployed provider hint.
    return (
        getattr(deployed_provider, "provider_hint", "")
        or getattr(deployed_provider, "provider_id", "")
        or ""
    ).lower()


async def _build_gateway_provider(
    *, brain: Any, deployed_provider: Any, settings: Any,
) -> Any:
    """Return a gateway-routed `OpenAICompatProvider`, cached per"""
    from digitorn.modules.llm_provider.providers.openai_compat import (
        OpenAICompatProvider,
        USER_JWT_PLACEHOLDER,
    )

    real_provider = _resolve_brain_provider_name(brain, deployed_provider)
    # `model` lives at different paths depending on the brain shape.
    real_model = (
        getattr(brain, "model", "")
        or (
            (getattr(brain, "inline_config", {}) or {}).get("model")
            if hasattr(brain, "inline_config") else ""
        )
        or getattr(deployed_provider, "model", "")
        or ""
    )
    # LiteLLM convention: "provider/model" routes the call. If the
    # YAML already namespaces the model, keep it as-is.
    gateway_model = real_model if "/" in real_model else f"{real_provider}/{real_model}"
    timeout = getattr(brain, "timeout", None)
    base_url = settings.runtime.gateway_base_url
    cache_key = (base_url, gateway_model, timeout)

    cached = _PROVIDER_CACHE.get(cache_key)
    if cached is not None:
        return cached

    async with _PROVIDER_CACHE_LOCK:
        # Re-check under the lock: another coroutine may have built
        # while we were waiting.
        cached = _PROVIDER_CACHE.get(cache_key)
        if cached is not None:
            return cached
        provider = OpenAICompatProvider(
            provider_id="digitorn_gateway",
            model=gateway_model,
            api_key=USER_JWT_PLACEHOLDER,
            base_url=base_url,
            provider_hint="digitorn_gateway",
            timeout=timeout,
        )
        await provider.initialize()
        _PROVIDER_CACHE[cache_key] = provider
        logger.info(
            "session_provider: ROUTE-VIA-GATEWAY (cold) base_url=%s "
            "model=%s (real_provider=%s real_model=%s) cache_size=%d",
            base_url, gateway_model, real_provider, real_model,
            len(_PROVIDER_CACHE),
        )
        return provider


async def route_derived_brain_through_gateway(
    *,
    brain: Any,
    deployed_provider: Any,
    settings: Any,
) -> Any:
    """Apply the default "everything via the gateway" rule to a derived"""
    if brain is None or deployed_provider is None:
        return deployed_provider
    try:
        if not getattr(settings.runtime, "gateway_enabled", True):
            return deployed_provider
        real_provider = _resolve_brain_provider_name(brain, deployed_provider)
        if real_provider in LOCAL_PROVIDERS:
            return deployed_provider
        return await _build_gateway_provider(
            brain=brain, deployed_provider=deployed_provider, settings=settings,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "route_derived_brain_through_gateway failed (keeping deployed): %s",
            exc,
        )
        return deployed_provider


def reset_gateway_provider_cache() -> None:
    """Drop every cached gateway provider. Used by tests and by any"""
    _PROVIDER_CACHE.clear()
