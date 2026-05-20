"""Compile-time secret resolver using the new CredentialStore."""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.credentials.store import CredentialStore, Scope

logger = logging.getLogger(__name__)


async def build_compile_secrets(
    store: CredentialStore | None,
    *,
    app_id: str,
    legacy_secrets: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a flat `{key: value}` dict of secrets visible at compile time."""
    merged: dict[str, str] = {}

    if store is not None:
        try:
            # system_wide
            system_rows = await store.list_credentials(
                user_id=None, app_id=None, scope=Scope.SYSTEM_WIDE,
            )
            for row in system_rows:
                await _flatten_credential_into(store, row, merged)

            # per_app_shared for this app
            shared_rows = await store.list_credentials(
                user_id=None, app_id=app_id, scope=Scope.PER_APP_SHARED,
            )
            for row in shared_rows:
                await _flatten_credential_into(store, row, merged)
        except Exception as exc:
            logger.warning(
                "build_compile_secrets: CredentialStore lookup failed for "
                "app=%s: %s - falling back to legacy secrets only",
                app_id, exc,
            )

    if legacy_secrets:
        merged.update(legacy_secrets)

    return merged


async def _flatten_credential_into(
    store: CredentialStore,
    row: dict[str, Any],
    target: dict[str, str],
) -> None:
    """Decrypt a credential row and inject its fields into `target`."""
    try:
        full = await store.get_credential(
            user_id=row.get("user_id"),
            app_id=row.get("app_id"),
            provider_name=row["provider_name"],
            decrypt=True,
        )
    except Exception as exc:
        logger.warning(
            "flatten: failed to decrypt %s: %s", row.get("provider_name"), exc,
        )
        return
    if full is None:
        return

    fields = full.get("fields") or {}
    provider = row["provider_name"]

    if len(fields) == 1:
        # Flat form: secret.PROVIDER → the single field value
        only_value = next(iter(fields.values()))
        target[provider] = str(only_value)
        # ALSO expose as PROVIDER.FIELD for apps that prefer the
        # dotted form even with a single field.
        only_name = next(iter(fields.keys()))
        target[f"{provider}.{only_name}"] = str(only_value)
    else:
        # Multi-field form: every field under PROVIDER.FIELD
        for fname, fval in fields.items():
            target[f"{provider}.{fname}"] = str(fval)


async def runtime_resolve_secret(
    store: CredentialStore | None,
    *,
    key: str,
    user_id: str,
    app_id: str,
) -> str | None:
    """Resolve a single secret key at runtime for a specific user."""
    if store is None:
        return None
    try:
        return await store.resolve_field(
            provider_or_field=key,
            user_id=user_id,
            app_id=app_id,
        )
    except Exception as exc:
        logger.warning(
            "runtime_resolve_secret failed for key=%s user=%s app=%s: %s",
            key, user_id, app_id, exc,
        )
        return None
