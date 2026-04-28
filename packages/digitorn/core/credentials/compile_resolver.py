"""Compile-time secret resolver using the new CredentialStore.

The existing compiler in ``core/app/compiler.py`` takes a pre-built
``secrets: dict[str, str]`` when it invokes ``variables._lookup``
for ``{{secret.X}}`` references. This module builds that dict from
the new ``CredentialStore`` by walking the scopes that are valid at
compile time:

- ``system_wide``  - daemon-level config, always shared
- ``per_app_shared`` - credentials scoped to this specific app

Per-user scopes (``per_user``, ``per_app_per_user``) are **not**
resolved at compile time because the compile has no user context.
Those are resolved at runtime via
``runtime_resolve_secret(store, user_id, app_id, key)``.

Both resolvers go through the same store, so the user never has to
think about "which resolver reads which scope" - the store's
``resolve_field`` walks the full 4-level hierarchy.

Legacy ``secret_store`` per-app shim
------------------------------------

The previous daemon had a separate per-app secret store
(``manager._secret_store``). During migration we keep writing to it
AND to the new store, and this module reads from both. Once every
app has been migrated, the old store can be removed without
touching any runtime code - this resolver is the single seam.
"""

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
    """Build a flat ``{key: value}`` dict of secrets visible at compile time.

    Walks ``per_app_shared`` and ``system_wide`` scopes on the new
    store, merges with the legacy per-app secret dict (for apps that
    haven't been migrated yet), and returns the result.

    The compiler then passes this dict to ``variables._resolve_string``
    and everything in YAML that uses ``{{secret.X}}`` resolves as it
    did before - except now ``X`` can come from the new store.

    Args:
        store: The live CredentialStore, or None to skip new-store lookup.
        app_id: The app being compiled.
        legacy_secrets: The existing per-app secret dict from
            ``manager._secret_store`` (shim during migration).

    Returns:
        A flat dict. Legacy values win on conflict (to avoid
        breaking apps that set their secret via the old route).
    """
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

    # Legacy secrets win because apps that had old secret_store entries
    # may reference them with their historical keys. Migration will
    # eventually drop this merge step.
    if legacy_secrets:
        merged.update(legacy_secrets)

    return merged


async def _flatten_credential_into(
    store: CredentialStore,
    row: dict[str, Any],
    target: dict[str, str],
) -> None:
    """Decrypt a credential row and inject its fields into ``target``.

    The naming convention for the flat dict keys:

    - If a credential has a **single field**, it lands under its
      provider_name (so ``provider: OPENAI_API_KEY`` with a single
      ``api_key`` field → ``target["OPENAI_API_KEY"] = <value>``).
      This preserves the legacy flat-secret-name convention.

    - If a credential has **multiple fields**, each lands under
      ``<provider>.<field>`` (so Slack's bot_token becomes
      ``target["slack.bot_token"]``). The YAML references the same
      way: ``{{secret.slack.bot_token}}``.
    """
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
    """Resolve a single secret key at runtime for a specific user.

    Used by runtime template renderers (channels pipeline, module
    params rendering) that have a user context and want to resolve
    ``{{secret.X}}`` expressions left as passthrough by the compile.

    Walks the full 4-scope hierarchy via ``store.resolve_field``:

    1. per_app_per_user
    2. per_user
    3. per_app_shared
    4. system_wide

    Returns ``None`` if nothing matched - the caller decides whether
    to raise or log.
    """
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
