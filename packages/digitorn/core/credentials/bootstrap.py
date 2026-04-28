"""One-shot env var importer - bootstrap credentials on first daemon boot.

Policy (confirmed with the user):

- **Env vars are NOT a persistent source.** They exist only to help
  the first boot: if the daemon starts with ``OPENAI_API_KEY=sk-...``
  in its environment, we import that value once into the encrypted
  store under scope ``system_wide`` and then **never read it again**.
  Subsequent daemon restarts don't need the env var.

- **Only well-known keys** are imported. Random env vars are ignored
  so we don't leak arbitrary shell variables into the store.

- **Never overwrite**: if a system_wide credential already exists
  for a provider, the env value is skipped. Once the daemon is
  operating from the persistent store, changing an env var has no
  effect - this is by design.

- **Audit log**: every import is logged (but the value itself is
  never logged - only the key name). A counter is returned so the
  caller can print "imported N secrets from env on first boot".
"""

from __future__ import annotations

import logging
import os
from typing import Any

from digitorn.core.credentials.store import CredentialStore, Scope, Status

logger = logging.getLogger(__name__)


# The keys we recognise. Each entry maps an env var name to a
# (provider_name, field_name) pair in the store. If you add a new
# key here, think hard: env var imports are a migration helper, not
# a config mechanism.
WELL_KNOWN_ENV_KEYS: dict[str, tuple[str, str]] = {
    # LLM providers - the big ones
    "ANTHROPIC_API_KEY": ("anthropic", "api_key"),
    "OPENAI_API_KEY": ("openai", "api_key"),
    "OPENAI_ORG_ID": ("openai", "organization"),
    "DEEPSEEK_API_KEY": ("deepseek", "api_key"),
    "GROQ_API_KEY": ("groq", "api_key"),
    "MISTRAL_API_KEY": ("mistral", "api_key"),
    "GEMINI_API_KEY": ("gemini", "api_key"),
    "COHERE_API_KEY": ("cohere", "api_key"),
    # Embedding / search
    "VOYAGE_API_KEY": ("voyage", "api_key"),
    "JINA_API_KEY": ("jina", "api_key"),
    # Channels
    "TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
    "SLACK_BOT_TOKEN": ("slack", "bot_token"),
    "SLACK_SIGNING_SECRET": ("slack", "signing_secret"),
    "SLACK_APP_TOKEN": ("slack", "app_token"),
    "DISCORD_BOT_TOKEN": ("discord", "bot_token"),
    # Email
    "SMTP_HOST": ("smtp", "host"),
    "SMTP_USER": ("smtp", "user"),
    "SMTP_PASSWORD": ("smtp", "password"),
    "IMAP_HOST": ("imap", "host"),
    "IMAP_USER": ("imap", "user"),
    "IMAP_PASSWORD": ("imap", "password"),
    # Database
    "DATABASE_URL": ("database", "url"),
    "POSTGRES_URL": ("postgres", "url"),
    "REDIS_URL": ("redis", "url"),
    "MONGODB_URL": ("mongodb", "url"),
    # Webhooks
    "WEBHOOK_SECRET": ("webhook", "secret"),
    "GITHUB_WEBHOOK_SECRET": ("github_webhook", "secret"),
    # Twilio / voice
    "TWILIO_ACCOUNT_SID": ("twilio", "account_sid"),
    "TWILIO_AUTH_TOKEN": ("twilio", "auth_token"),
}


async def import_env_vars_into_store(
    store: CredentialStore,
) -> dict[str, Any]:
    """Scan the environment for well-known keys and import any matches.

    Returns a dict summary::

        {
            "imported": ["anthropic.api_key", "telegram.bot_token"],
            "skipped_already_present": ["openai.api_key"],
            "not_in_env": 15,
        }

    Called once in the daemon lifespan after the store is constructed
    and before the compiler is used. Safe to call multiple times -
    it never overwrites existing rows.
    """
    imported: list[str] = []
    skipped: list[str] = []
    not_in_env = 0

    # Group env vars by provider so multi-field credentials land in
    # one row. For instance OPENAI_API_KEY + OPENAI_ORG_ID → one
    # credential row with two fields.
    by_provider: dict[str, dict[str, str]] = {}
    for env_name, (provider, field) in WELL_KNOWN_ENV_KEYS.items():
        value = os.environ.get(env_name)
        if value is None or value == "":
            not_in_env += 1
            continue
        by_provider.setdefault(provider, {})[field] = value

    for provider, fields in by_provider.items():
        # Check for an existing system_wide row - never overwrite.
        existing = await store.get_credential(
            user_id=None, app_id=None, provider_name=provider,
        )
        if existing is not None:
            skipped.extend(f"{provider}.{f}" for f in fields)
            continue

        # Guess the provider_type from the fields we have. Most are
        # api_key. multi_field if >1 field. connection_string for
        # the *_URL entries we grouped under the same provider.
        if any(f == "url" for f in fields):
            provider_type = "connection_string"
        elif len(fields) > 1:
            provider_type = "multi_field"
        else:
            provider_type = "api_key"

        try:
            await store.upsert_credential(
                user_id=None,
                app_id=None,
                provider_name=provider,
                provider_type=provider_type,
                scope=Scope.SYSTEM_WIDE,
                fields=fields,
                status=Status.FILLED,
                display_metadata={
                    "imported_from_env": True,
                    "label": provider,
                },
            )
            imported.extend(f"{provider}.{f}" for f in fields)
            logger.info(
                "credential imported from env: provider=%s fields=%s",
                provider, list(fields.keys()),
            )
        except Exception as exc:
            logger.warning(
                "failed to import env credential for %s: %s", provider, exc,
            )

    return {
        "imported": imported,
        "skipped_already_present": skipped,
        "not_in_env": not_in_env,
    }
