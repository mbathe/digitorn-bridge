"""Session-level credential resolver - binds user credentials to providers.

When a user sends a message to a deployed app, this module runs
**before** the agent turn starts. It walks every LLM provider, every
MCP server config, and every module config that contains a secret
template, and substitutes the per-user values from the credential
store.

The key behavior: if a template references a user-scoped credential
that does not yet have a grant for this app, ``CredentialAuthRequired``
is raised. The caller (``_run_turn`` in api/apps.py) catches it and
publishes a structured event on the session bus that drives the picker dialog on
the client.

Why session-start and not per-turn or per-call?
------------------------------------------------

- **Per-call** would require every LLM provider to be credential-
  aware and would duplicate the walk for every generate request.
- **Per-turn** works but forces a store read on every turn even
  when nothing changed.
- **Session-start** mutates the shared provider instances once per
  session with the caller's resolved credentials. For the initial
  multi-user story this is the pragmatic trade-off - single-daemon
  multi-user deployments where users don't step on each other's
  providers concurrently are the common case.

For the concurrent multi-user case, a later refinement will give
each user its own provider instance keyed under
``f"u:{user_id}:{provider_id}"`` on the llm_module.
"""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.credentials.runtime_resolver import (
    resolve_runtime_secrets_in_value,
)
from digitorn.core.credentials.store import (
    CredentialAuthRequired,
    CredentialStore,
)

logger = logging.getLogger(__name__)


async def ensure_user_credentials_for_app(
    *,
    deployed_app: Any,
    user_id: str,
    credential_store: CredentialStore | None,
) -> dict[str, Any]:
    """Resolve and bind the user's credentials for a deployed app.

    Walks the compiled app's module configs, substitutes every
    ``{{env.X}}`` / ``{{secret.X}}`` template with the effective
    per-user value (going through grants), and mutates the live
    provider instances on ``deployed_app.modules`` so the agent
    turn that follows uses the right credentials.

    Args:
        deployed_app: ``DeployedApp`` - has ``app_id``, ``compiled``,
                     ``modules``.
        user_id:     Caller's user id - required. Pass ``"local"`` or
                     ``"anonymous"`` for dev mode.
        credential_store: Live ``CredentialStore``. When ``None``,
                     the function is a no-op (dev paths without a
                     configured store keep working).

    Raises:
        CredentialAuthRequired: The user has candidate credentials
            for a provider but no grant for this app yet. The
            exception carries the picker metadata.

    Returns:
        A diagnostic dict::

            {
                "resolved_providers": [...],
                "resolved_modules":   [...],
                "unresolved":         [...],
            }

        Only used for logging / debugging - the mutation is the
        real output.
    """
    if credential_store is None:
        return {"resolved_providers": [], "resolved_modules": [], "unresolved": []}

    if not user_id:
        user_id = "local"

    app_id = deployed_app.app_id
    compiled = deployed_app.compiled
    modules = deployed_app.modules

    resolved_providers: list[str] = []
    resolved_modules: list[str] = []
    unresolved: list[str] = []

    # ── 1. LLM providers ────────────────────────────────────────
    llm_module = modules.get("llm_provider")
    if llm_module is not None:
        llm_cfg = dict(compiled.modules["llm_provider"].config or {})
        providers_cfg = llm_cfg.get("providers") or {}
        # The compile normalises a flat single-provider form into
        # ``providers: {pid: {...}}``; if somehow the flat form is
        # still here, fold it.
        if not providers_cfg and any(k in llm_cfg for k in ("provider", "backend", "model", "api_key")):
            providers_cfg = {llm_cfg.get("provider", "default"): llm_cfg}

        for provider_id, provider_conf in providers_cfg.items():
            # Derive the canonical provider name (``deepseek``,
            # ``openai``, ...) from the compiled config so the runtime
            # resolver can rewrite bare ``{{secret.DEEPSEEK_API_KEY}}``
            # into the qualified ``deepseek.DEEPSEEK_API_KEY`` lookup
            # that matches how the credential is stored.
            canonical_provider: str | None = None
            if isinstance(provider_conf, dict):
                canonical_provider = provider_conf.get("provider") or provider_conf.get("provider_hint")
            if not canonical_provider and isinstance(provider_id, str):
                if provider_id.endswith("_brain"):
                    # For inline brains the internal id is
                    # ``{agent}_brain`` - not a usable provider name;
                    # leave the hint unset and let the runtime
                    # resolver fall back to the bare key (env var).
                    canonical_provider = None
                else:
                    canonical_provider = provider_id

            logger.info(
                "session_resolver: app=%s user=%s provider_id=%s canonical=%s api_key=%r",
                app_id, user_id, provider_id, canonical_provider,
                str(provider_conf.get("api_key", ""))[:60] if isinstance(provider_conf, dict) else None,
            )
            try:
                resolved = await resolve_runtime_secrets_in_value(
                    provider_conf,
                    store=credential_store,
                    user_id=user_id,
                    app_id=app_id,
                    raise_on_miss=False,
                    provider_hint=canonical_provider,
                )
            except CredentialAuthRequired:
                raise

            # Mutate the live provider instance in place.
            live = getattr(llm_module, "_providers", {}).get(provider_id)
            if live is None:
                # Provider isn't spun up yet (bootstrap may have
                # skipped it because the api_key was a template).
                # Ask the module to configure it now with the
                # resolved config.
                try:
                    configure_fn = getattr(llm_module, "_configure_from_dict", None)
                    if configure_fn is not None:
                        await configure_fn(provider_id, resolved)
                        live = getattr(llm_module, "_providers", {}).get(provider_id)
                except CredentialAuthRequired:
                    raise
                except Exception as exc:
                    logger.warning(
                        "ensure_user_credentials: configure %s failed: %s",
                        provider_id, exc,
                    )

            resolved_api_key = resolved.get("api_key") if isinstance(resolved, dict) else None
            logger.info(
                "session_resolver: resolved app=%s provider_id=%s api_key=%r live=%s",
                app_id, provider_id,
                (resolved_api_key[:16] + "…") if isinstance(resolved_api_key, str) and len(resolved_api_key) > 16 else resolved_api_key,
                live is not None,
            )
            if live is not None:
                # The canonical provider name (deepseek, openai, ...)
                # is in ``provider_conf["provider"]``. The iteration
                # key ``provider_id`` is an INTERNAL id - for inline
                # agent brains the compiler generates it as
                # ``f"{agent_id}_brain"``, which must never leak to
                # the credential picker.
                canonical_provider = (
                    resolved.get("provider")
                    or provider_conf.get("provider")
                    or provider_id
                )
                # If provider_id looks like "{agent}_brain", strip
                # the suffix to expose the owning agent id for UI.
                agent_id: str | None = None
                if isinstance(provider_id, str) and provider_id.endswith("_brain"):
                    agent_id = provider_id[: -len("_brain")]
                _override_provider_fields(
                    live,
                    resolved,
                    canonical_provider=canonical_provider,
                    agent_id=agent_id,
                    provider_id=provider_id,
                )
                resolved_providers.append(provider_id)

    # ── 2. Other module configs with secret templates ──────────
    # Modules like mcp, http, database, etc. can carry secret
    # templates too. We don't mutate them here - MCP has its own
    # grant-aware resolver at connect time. Collect whatever is
    # still unresolved for the diagnostic dict.
    for mid, mod_cfg in compiled.modules.items():
        cfg = mod_cfg.config or {}
        for left in _collect_unresolved(cfg):
            unresolved.append(f"{mid}.{left}")

    return {
        "resolved_providers": resolved_providers,
        "resolved_modules": resolved_modules,
        "unresolved": unresolved,
    }


def _override_provider_fields(
    provider: Any,
    resolved_config: dict[str, Any],
    *,
    canonical_provider: str | None = None,
    agent_id: str | None = None,
    provider_id: str | None = None,
) -> None:
    """Mutate a live LLM provider instance with resolved credentials.

    Only touches the fields that are safe to hot-swap: ``api_key``,
    ``base_url``, and anything in ``default_params``. Everything
    else (model id, backend family, retry policy) stays as-is
    because those are set at construction time.

    ``canonical_provider`` is the true provider name (deepseek,
    openai, ...) - used when raising ``CredentialMissing`` so the
    credential picker on the client creates the entry under the
    same name the daemon uses at lookup time.
    """
    for key in ("api_key", "base_url"):
        if key in resolved_config and resolved_config[key] is not None:
            new_val = resolved_config[key]
            # Skip literal passthroughs - they indicate "no credential
            # found, runtime resolver left the template". Don't wipe
            # a real api_key with a template.
            if isinstance(new_val, str) and new_val.startswith("{{"):
                continue
            setattr(provider, key, new_val)

    # If the provider's api_key is STILL a template literal at this
    # point - meaning neither the credential store nor the env var
    # fallback could resolve it - raise a clear error so the chat
    # route surfaces a structured event to the client instead of
    # silently 401-ing on the LLM call.
    current_key = getattr(provider, "api_key", None)
    if isinstance(current_key, str) and current_key.startswith("{{"):
        # Extract the secret name for the error message
        import re as _re
        m = _re.search(r"\{\{(?:secret|env)\.([A-Za-z0-9_]+)\}\}", current_key)
        secret_name = m.group(1) if m else current_key
        from digitorn.core.credentials.store import CredentialMissing
        # Fall back to the live provider's provider_id only when no
        # canonical name was supplied by the caller - never the
        # internal ``{agent}_brain`` key.
        raise CredentialMissing(
            provider=canonical_provider or getattr(provider, "provider_id", "llm"),
            field=secret_name,
            app_id=None,
            user_id=None,
            agent_id=agent_id,
            provider_id=provider_id,
        )

    # Anthropic provider caches an OAuth token - force a reload on
    # next call so the new api_key takes effect.
    for cache_attr in ("_cached_token", "_cached_token_expiry", "_client"):
        if hasattr(provider, cache_attr):
            try:
                setattr(provider, cache_attr, None)
            except Exception:
                pass


def _collect_unresolved(value: Any) -> list[str]:
    """Small wrapper over ``collect_unresolved_secrets`` to keep the
    diagnostic return value limited to this module's scope."""
    from digitorn.core.credentials.runtime_resolver import collect_unresolved_secrets
    return collect_unresolved_secrets(value)
