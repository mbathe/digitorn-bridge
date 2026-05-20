"""Session-level credential resolver - binds user credentials to providers."""

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
    byok_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve and bind the user's credentials for a deployed app."""
    if credential_store is None:
        return {"resolved_providers": [], "resolved_modules": [], "unresolved": []}

    try:
        from digitorn.core.config import get_settings as _get_settings
        if _get_settings().mode == "cloud":
            return {"resolved_providers": [], "resolved_modules": [], "unresolved": []}
    except Exception as exc:
        logger.debug("session_resolver best-effort block failed: %s", exc)

    if not user_id:
        user_id = "local"

    app_id = deployed_app.app_id
    compiled = deployed_app.compiled
    modules = deployed_app.modules

    resolved_providers: list[str] = []
    resolved_modules: list[str] = []
    unresolved: list[str] = []

    llm_module = modules.get("llm_provider")
    if llm_module is not None:
        llm_cfg = dict(compiled.modules["llm_provider"].config or {})
        providers_cfg = llm_cfg.get("providers") or {}
        if not providers_cfg and any(k in llm_cfg for k in ("provider", "backend", "model", "api_key")):
            providers_cfg = {llm_cfg.get("provider", "default"): llm_cfg}

        for provider_id, provider_conf in providers_cfg.items():
            canonical_provider: str | None = None
            if isinstance(provider_conf, dict):
                canonical_provider = provider_conf.get("provider") or provider_conf.get("provider_hint")
            if not canonical_provider and isinstance(provider_id, str):
                if provider_id.endswith("_brain"):
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

    for mid, mod_cfg in compiled.modules.items():
        cfg = mod_cfg.config or {}
        for left in _collect_unresolved(cfg):
            unresolved.append(f"{mid}.{left}")

    try:
        from digitorn.core.credentials.inject_session_time import (
            inject_session_time_credentials,
        )
        audit = None
        try:
            mgr = getattr(deployed_app, "_manager", None)
            if mgr is not None:
                audit = getattr(mgr, "_credential_audit", None)
        except Exception:
            audit = None
        session_resolved = await inject_session_time_credentials(
            compiled=compiled,
            modules=modules,
            credential_store=credential_store,
            user_id=user_id,
            audit=audit,
            byok_overrides=byok_overrides,
        )
        resolved_providers.extend(session_resolved.get("providers", []))
        resolved_modules.extend(session_resolved.get("modules", []))
    except CredentialAuthRequired:
        # Surface the picker flow up to the chat route, exactly like
        # the legacy `{{secret.X}}` path.
        raise
    except Exception as exc:
        logger.warning(
            "session_time_credential_inject_failed app=%s user=%s: %s",
            app_id, user_id, exc, exc_info=True,
        )

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
    """Mutate a live LLM provider instance with resolved credentials."""
    for key in ("api_key", "base_url"):
        if key in resolved_config and resolved_config[key] is not None:
            new_val = resolved_config[key]
            if isinstance(new_val, str) and new_val.startswith("{{"):
                continue
            setattr(provider, key, new_val)

    current_key = getattr(provider, "api_key", None)
    if isinstance(current_key, str) and current_key.startswith("{{"):
        # Extract the secret name for the error message
        import re as _re
        m = _re.search(r"\{\{(?:secret|env)\.([A-Za-z0-9_]+)\}\}", current_key)
        secret_name = m.group(1) if m else current_key
        from digitorn.core.credentials.store import CredentialMissing
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
            except Exception as exc:
                logger.debug("session_resolver best-effort block failed: %s", exc)


def _collect_unresolved(value: Any) -> list[str]:
    """Small wrapper over `collect_unresolved_secrets` to keep the"""
    from digitorn.core.credentials.runtime_resolver import collect_unresolved_secrets
    return collect_unresolved_secrets(value)
