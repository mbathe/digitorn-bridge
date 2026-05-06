"""Decide whether the runtime should route an outbound LLM call
through the digitorn gateway or use the app's deployed provider as-is.

Core principle (the YAML is developer-agnostic; it MAY declare
``credential:``, ``api_key``, ``provider_id``, etc. - all those are
runtime hints that we honour ONLY when the user explicitly opts out
of the Digitorn account via the BYOK toggle):

  * Authenticated Digitorn user, default state → ROUTE via gateway.
    The brain's ``credential:`` / ``api_key`` blocks are IGNORED -
    only the ``(provider, model)`` tuple matters. The gateway has the
    catalogue of provider keys and bills the user's quota.

  * Authenticated Digitorn user, BYOK ON → KEEP deployed provider.
    The user explicitly chose to bring their own credentials.
    ``inject_session_time_credentials`` has already either injected
    the user's saved credential into the live provider OR raised
    ``CredentialAuthRequired`` so the picker opens upstream.

  * Local pseudo-user (CLI / healthcheck / internal) → KEEP. The
    daemon trusts the YAML when no real user is present.

  * Local-only provider (ollama, lm_studio, vllm) → KEEP. The gateway
    can't proxy a model running on the user's laptop.

  * Provider not in the gateway catalogue → KEEP, as a fallback path.
    The deployed provider has whatever auth the YAML or env put on
    it; if that fails, the user gets a clean error and can configure.

Decision tree (applied at session-start, AFTER deploy-time provider
instantiation, BEFORE the first ``agent_turn``):

    1. ``byok_enabled = True`` (toggle row in user_app_byok).
       → KEEP the deployed provider (user-managed credential).

    2. ``settings.runtime.gateway_enabled`` is False.
       → KEEP the deployed provider. Operator opted out of routing.

    3. The user is not authenticated (anonymous, local, system).
       → KEEP the deployed provider. CLI / healthcheck / internal.

    4. The brain's provider is local-only (ollama, lm_studio, vllm).
       → KEEP the deployed provider.

    5. The brain's provider is not in the gateway catalogue.
       → KEEP the deployed provider as a fallback.

    6. Otherwise: ROUTE via gateway.
       A fresh ``OpenAICompatProvider`` is instantiated, pointing at
       ``settings.runtime.gateway_base_url`` with the placeholder
       api_key. At call time the provider's HTTP layer pulls the
       user's JWT from the ``RequestContext`` and forwards it as the
       gateway bearer. The model alias is prefixed with the original
       provider name (anthropic, openai, …) so LiteLLM inside the
       gateway routes correctly.

The resolver NEVER mutates the YAML. It returns a provider instance
that the caller assigns to ``ctx.provider`` for the duration of the
session.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Providers that the gateway proxies. Anything outside this set is
# considered local-only or out of scope; the runtime keeps the
# deployed provider unchanged.
#
# ``openai_compat`` and ``github_copilot`` are intentionally absent:
# the first targets an arbitrary user-supplied base_url (no canonical
# Digitorn-side key), the second uses a per-user OAuth device-code
# flow with a token cached on disk (``GITHUB_COPILOT_API_KEY_FILE``).
# Neither can be served from the gateway's shared key catalogue, so
# they always run as BYOK against the deployed provider.
GATEWAY_SUPPORTED_PROVIDERS: frozenset[str] = frozenset({
    "anthropic",
    "openai",
    "deepseek",
    "gemini",
    "google",
    "mistral",
    "groq",
    "cohere",
    "azure",
    "xai",
    "perplexity",
})

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
    """Return the LLM provider the runtime should use for this session.

    Returns ``deployed_provider`` (no swap) when the user opted out via
    BYOK, the gateway is disabled, the user is anonymous, or the brain
    is local-only / unsupported. Otherwise returns a freshly built
    ``OpenAICompatProvider`` aimed at the gateway. NEVER returns
    ``None`` - the caller can always proceed with the value.

    ``byok_enabled`` is the per-(user, app) toggle from
    ``user_app_byok``. When True, the user explicitly chose to bring
    their own credentials and the deployed provider stays in the
    path. The credential injection done upstream by
    ``inject_session_time_credentials`` has either mutated the
    deployed provider with the user's saved key OR raised
    ``CredentialAuthRequired`` so the picker opens.
    """
    brain = getattr(agent, "brain", None)
    if brain is None:
        return deployed_provider

    # Rule 1: BYOK on - user opted out of the Digitorn account.
    # The deployed provider has been pre-configured by
    # ``inject_session_time_credentials`` (or the picker fired).
    # The YAML's ``credential:`` block is honoured ONLY in this branch.
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

    # Rule 5: provider unsupported by the gateway catalogue.
    if real_provider not in GATEWAY_SUPPORTED_PROVIDERS:
        # Unknown provider - we don't know how to alias the model for
        # LiteLLM. Keep the deployed provider; if it works it works,
        # otherwise the user will need to configure a credential.
        logger.info(
            "session_provider: KEEP (provider %s not in gateway catalogue)",
            real_provider,
        )
        return deployed_provider

    llm_module = modules.get("llm_provider")
    if llm_module is None:
        logger.warning(
            "session_provider: KEEP (llm_provider module missing, app=%s)",
            app_id,
        )
        return deployed_provider

    # Rule 6: route via gateway.
    return await _build_gateway_provider(
        brain=brain, deployed_provider=deployed_provider, settings=settings,
    )


def _resolve_brain_provider_name(brain: Any, deployed_provider: Any) -> str:
    """Return the canonical provider name (e.g. ``deepseek``) for the
    brain. Walks several possible storage locations because the
    compile pipeline normalises brains in two shapes:

      * Inline brain (``CompiledBrain.is_inline = True``) - the
        canonical name lives in ``inline_config["provider"]``.
      * Named brain - resolves through ``provider_id`` which by
        convention matches the canonical name (the legacy
        ``{{secret.X}}`` resolver relies on the same equivalence).

    Falls back to the live ``deployed_provider``'s ``provider_hint``,
    populated by the openai_compat / anthropic provider classes when
    the daemon spun up the deploy-time instance.
    """
    # 1) AgentBrain (raw schema) carries ``provider`` directly.
    direct = getattr(brain, "provider", "") or ""
    if direct:
        return str(direct).lower()
    # 2) CompiledBrain inline form.
    inline = getattr(brain, "inline_config", None)
    if isinstance(inline, dict):
        pname = inline.get("provider") or inline.get("provider_hint") or ""
        if pname:
            return str(pname).lower()
    # 3) CompiledBrain named form - provider_id often matches the
    #    canonical name (deepseek_main → deepseek? not always; only
    #    keep the part before an underscore as a heuristic).
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
    """Instantiate a fresh ``OpenAICompatProvider`` aimed at the gateway.

    The api_key carries the placeholder string that the provider's
    HTTP layer recognises and replaces with the user's JWT (read from
    the ``RequestContext`` ContextVar at call time).
    """
    from digitorn.modules.llm_provider.providers.openai_compat import (
        OpenAICompatProvider,
        USER_JWT_PLACEHOLDER,
    )

    real_provider = _resolve_brain_provider_name(brain, deployed_provider)
    # ``model`` lives at different paths depending on the brain shape.
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

    provider = OpenAICompatProvider(
        provider_id="digitorn_gateway",
        model=gateway_model,
        api_key=USER_JWT_PLACEHOLDER,
        base_url=settings.runtime.gateway_base_url,
        provider_hint="digitorn_gateway",
        timeout=getattr(brain, "timeout", None),
    )
    await provider.initialize()
    logger.info(
        "session_provider: ROUTE-VIA-GATEWAY base_url=%s model=%s "
        "(real_provider=%s real_model=%s)",
        settings.runtime.gateway_base_url, gateway_model,
        real_provider, real_model,
    )
    return provider
