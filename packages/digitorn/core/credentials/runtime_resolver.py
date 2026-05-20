"""Runtime secret resolution - per-user credentials at activation time."""

from __future__ import annotations

import logging
import re
from typing import Any

from digitorn.core.credentials.store import CredentialStore

logger = logging.getLogger(__name__)


def _field_spec_from_catalogue(provider: str) -> dict[str, Any]:
    """Build a `field_spec` dict from the provider catalogue so the"""
    try:
        from dataclasses import asdict, is_dataclass
        from digitorn.core.credentials.catalog import default_catalog
        from digitorn.core.credentials.handler import default_registry
    except Exception:
        return {}
    try:
        tpl = default_catalog.get(provider)
    except Exception:
        tpl = None
    if tpl is None:
        return {}
    try:
        handler = default_registry.get(tpl.handler_type)
        handler_defaults = handler.schema_fields()
        fields = tpl.effective_fields(handler_defaults)
    except Exception:
        fields = []
    return {
        "name": tpl.name,
        "label": tpl.display_name or tpl.name,
        "type": tpl.handler_type,
        "fields": [
            f.to_dict() if hasattr(f, "to_dict")
            else (asdict(f) if is_dataclass(f) else dict(f))
            for f in fields
        ],
    }


_SECRET_PATTERN = re.compile(
    r"\{\{\s*(?:secret|env)\.([a-zA-Z0-9_.-]+)\s*\}\}"
)


async def resolve_runtime_secrets_in_value(
    value: Any,
    *,
    store: CredentialStore | None,
    user_id: str,
    app_id: str,
    raise_on_miss: bool = False,
    provider_hint: str | None = None,
) -> Any:
    """Walk an arbitrary JSON-compatible value and substitute user secrets."""
    if store is None:
        return value
    if isinstance(value, str):
        return await _resolve_string(
            value, store=store, user_id=user_id, app_id=app_id,
            raise_on_miss=raise_on_miss,
            provider_hint=provider_hint,
        )
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out[k] = await resolve_runtime_secrets_in_value(
                v, store=store, user_id=user_id, app_id=app_id,
                raise_on_miss=raise_on_miss,
                provider_hint=provider_hint,
            )
        return out
    if isinstance(value, list):
        return [
            await resolve_runtime_secrets_in_value(
                item, store=store, user_id=user_id, app_id=app_id,
                raise_on_miss=raise_on_miss,
                provider_hint=provider_hint,
            )
            for item in value
        ]
    return value


async def _resolve_string(
    text: str,
    *,
    store: CredentialStore,
    user_id: str,
    app_id: str,
    raise_on_miss: bool,
    provider_hint: str | None = None,
) -> str:
    """Substitute every `{{secret.X}}` / `{{env.X}}` in a string."""
    if "{{secret." not in text and "{{env." not in text:
        return text

    matches = list(_SECRET_PATTERN.finditer(text))
    if not matches:
        return text

    result_parts: list[str] = []
    last_end = 0

    from digitorn.core.credentials.store import (
        CredentialAuthRequired,
        CredentialMissing,
    )

    for match in matches:
        key = match.group(1)
        lookup_keys: list[str] = []
        if provider_hint and "." not in key:
            lookup_keys.append(f"{provider_hint}.{key}")
        lookup_keys.append(key)

        value: str | None = None
        lookup_used: str = key
        auth_exc: Exception | None = None
        for lookup_key in lookup_keys:
            try:
                provider_for_catalogue = (
                    lookup_key.split(".", 1)[0] if "." in lookup_key
                    else (provider_hint or lookup_key)
                )
                v = await store.resolve_field_for_app(
                    provider_or_field=lookup_key,
                    user_id=user_id,
                    app_id=app_id,
                    raise_on_auth_required=True,
                    field_spec=_field_spec_from_catalogue(
                        provider_for_catalogue,
                    ),
                )
            except CredentialAuthRequired as exc:
                auth_exc = exc
                continue
            except Exception as exc:
                logger.warning(
                    "runtime secret resolve failed for key=%s lookup=%s user=%s app=%s: %s",
                    key, lookup_key, user_id, app_id, exc,
                )
                v = None
            if v is not None:
                value = v
                lookup_used = lookup_key
                auth_exc = None  # a real hit supersedes any auth flow
                break

        # No hit at all → if any lookup surfaced an auth flow, bubble
        # it up now so the client gets the picker.
        if value is None and auth_exc is not None:
            raise auth_exc

        logger.debug(
            "runtime_secret_lookup key=%s hint=%s lookup=%s user=%s app=%s hit=%s",
            key, provider_hint, lookup_used, user_id, app_id, value is not None,
        )

        if value is None:
            import os as _os
            # `key` looks like `DEEPSEEK_API_KEY` (bare) or
            # `deepseek.DEEPSEEK_API_KEY` (qualified). Try both.
            env_key = key.split(".")[-1] if "." in key else key
            env_val = _os.environ.get(env_key)
            if env_val:
                logger.debug(
                    "runtime secret resolved from env: key=%s",
                    env_key,
                )
                value = env_val

        result_parts.append(text[last_end:match.start()])
        if value is not None:
            result_parts.append(str(value))
        else:
            if raise_on_miss:
                raise CredentialMissing(
                    provider=key.split(".")[0] if "." in key else key,
                    field=key.split(".", 1)[1] if "." in key else key,
                    app_id=app_id,
                    user_id=user_id,
                )
            result_parts.append(match.group(0))
        last_end = match.end()

    result_parts.append(text[last_end:])
    return "".join(result_parts)


# Small synchronous wrapper for code paths that are still sync


def collect_unresolved_secrets(value: Any) -> list[str]:
    """Return the list of `{{secret.X}}` / `{{env.X}}` templates still"""
    found: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            for m in _SECRET_PATTERN.finditer(node):
                found.append(m.group(1))
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(value)
    return found
