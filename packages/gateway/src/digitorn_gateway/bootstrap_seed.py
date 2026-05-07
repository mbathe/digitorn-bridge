"""Boot-time seeder.

Migrates the legacy YAML+env config into the new gateway DB tables on
first boot. Idempotent: subsequent boots are a no-op when rows exist.

Order:
  1. Providers from ``llm_call._PROVIDER_ENV_KEYS`` (the canonical list
     of providers LiteLLM understands).
  2. Credentials from process env vars when ``${env_var}`` is non-empty
     (one credential per provider, label=``Default``).
  3. Models from the legacy ``models.yaml`` catalogue.
  4. Routes left empty - lookup falls back to provider-default
     credential automatically.

Why seed at boot rather than in a migration:
  * The cipher needs the master key, which lives in env at runtime.
  * Provider keys live in env vars that the migration container won't
    have access to.
  * Re-runnable: an operator who deploys with new keys gets the new
    Default credential created automatically.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from digitorn_gateway.auth_dispatchers import schema_for as auth_schema_for
from digitorn_gateway.cipher import GatewayCipherError, encrypt_dict
from digitorn_gateway.llm_call import _PROVIDER_ENV_KEYS
from digitorn_gateway.models_db import (
    GatewayCredential,
    GatewayModel,
    GatewayProvider,
)

logger = logging.getLogger(__name__)


_PROVIDER_LABELS: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "deepseek": "DeepSeek",
    "gemini": "Google Gemini",
    "google": "Google Gemini",
    "mistral": "Mistral",
    "groq": "Groq",
    "cohere": "Cohere",
    "xai": "xAI",
    "perplexity": "Perplexity",
    "azure": "Azure OpenAI",
}


async def seed_if_empty(
    factory: async_sessionmaker[AsyncSession],
    *,
    catalog: Any,
) -> dict[str, int]:
    """Seed providers / credentials / models when the tables are empty.
    Returns a dict counting rows created per table (0 means already
    seeded, no work done)."""

    created = {"providers": 0, "credentials": 0, "models": 0}

    async with factory() as db:
        await _seed_providers(db, created)
        await _seed_credentials(db, created)
        await _seed_models(db, catalog, created)
        await _seed_default_aliases(db, created)
        await db.commit()

    if any(created.values()):
        logger.info("bootstrap_seed_inserted %s", created)
    else:
        logger.debug("bootstrap_seed already populated, skipping")
    return created


async def _seed_providers(
    db: AsyncSession, created: dict[str, int],
) -> None:
    """Two passes:
    1. If the table is empty, seed the legacy ``_PROVIDER_ENV_KEYS``
       map - one provider per LiteLLM-supported vendor.
    2. Always check that the multi-auth templates (``claude_code``,
       ``github_copilot``) exist; insert them when missing. This way an
       operator who upgrades from 0006 without re-creating the DB
       still sees the new templates appear after the next boot."""
    have_any = (
        await db.execute(select(GatewayProvider).limit(1))
    ).first() is not None
    if not have_any:
        for slug, env_vars in _PROVIDER_ENV_KEYS.items():
            env_var = env_vars[0]
            db.add(GatewayProvider(
                slug=slug,
                name=_PROVIDER_LABELS.get(slug, slug.title()),
                base_url=None,
                compat=slug if slug in ("openai", "anthropic", "azure") else "openai",
                env_var=env_var,
                auth_type="api_key",
                auth_schema=auth_schema_for("api_key"),
                extra_metadata={"alt_env_vars": list(env_vars[1:])} if len(env_vars) > 1 else {},
            ))
            created["providers"] += 1

    # Additive: ensure the multi-auth templates are present. Idempotent
    # by slug; a re-run does nothing. Each tuple = (slug, name, compat,
    # base_url, auth_type, description).
    extras: list[tuple[str, str, str, str | None, str, str]] = [
        # ── OAuth / file-paste flows ───────────────────────────────
        ("claude_code", "Claude Code (OAuth)", "anthropic", None,
         "claude_code",
         "Anthropic API via the Claude Code OAuth flow. Paste your "
         "access_token + refresh_token from ~/.claude/.credentials.json."),
        ("github_copilot", "GitHub Copilot", "openai_compat",
         "https://api.githubcopilot.com",
         "github_copilot",
         "Routes chat models GitHub Copilot exposes (claude-3.5-sonnet, "
         "gpt-4, etc.) using your GitHub OAuth token (gho_...). "
         "compat=openai_compat avoids LiteLLM's native github_copilot "
         "connector which would trigger its own device-flow auth."),
        # ── AWS-signed (multi-field) ───────────────────────────────
        ("aws_bedrock", "AWS Bedrock", "bedrock", None, "aws_bedrock",
         "Anthropic Claude + Meta Llama via AWS Bedrock. Uses SigV4 "
         "auth (no Bearer); paste an IAM access key + secret + region."),
        # ── OpenAI-compatible providers (just paste an API key) ────
        ("together_ai", "Together AI", "openai_compat",
         "https://api.together.xyz/v1", "api_key",
         "100+ open-source models hosted (Llama 3.3, Mixtral, "
         "DeepSeek-V3) at ~$0.5-2/M tokens."),
        ("fireworks_ai", "Fireworks AI", "openai_compat",
         "https://api.fireworks.ai/inference/v1", "api_key",
         "Llama / Mixtral / DeepSeek hosted with low-latency inference."),
        ("cerebras", "Cerebras", "openai_compat",
         "https://api.cerebras.ai/v1", "api_key",
         "Llama 3.3 70B at ~2000 tok/s - the fastest inference on the "
         "market for the same model class."),
        ("sambanova", "SambaNova", "openai_compat",
         "https://api.sambanova.ai/v1", "api_key",
         "Llama 3.3 / DeepSeek-V3 on SambaNova's RDU silicon, "
         "ultra-fast on long context."),
        ("hyperbolic", "Hyperbolic", "openai_compat",
         "https://api.hyperbolic.xyz/v1", "api_key",
         "Llama 3.3 / DeepSeek-V3 / DeepSeek-R1 hosted on a market for "
         "GPU compute."),
        ("nvidia_nim", "NVIDIA NIM", "openai_compat",
         "https://integrate.api.nvidia.com/v1", "api_key",
         "Nvidia's hosted catalogue (Llama, Mistral, Phi, "
         "DeepSeek-R1) on Nvidia infrastructure."),
        # ── Replicate (special compat - LiteLLM has a native handler) ─
        ("replicate", "Replicate", "openai_compat",
         "https://api.replicate.com/v1", "api_key",
         "Open-source models on Replicate's serverless GPU pool. "
         "Each model id is ``owner/model-name:version``."),
        # ── Google Vertex AI (service-account JSON) ────────────────
        ("vertex_ai", "Google Vertex AI", "vertex_ai", None, "vertex_ai",
         "Anthropic Claude + Gemini hosted on GCP. Paste your service-"
         "account JSON; LiteLLM signs each request with the embedded key."),
        # ── Azure OpenAI (multi-field) ─────────────────────────────
        ("azure_openai", "Azure OpenAI", "azure", None, "azure_openai",
         "GPT-4 / GPT-4o / o1 hosted on Azure. Needs api_key + endpoint URL"
         " + api_version + a default deployment name."),
        # ── Hugging Face Router (OpenAI-compat) ────────────────────
        ("huggingface", "Hugging Face", "openai_compat",
         "https://router.huggingface.co/v1", "api_key",
         "Llama / Mixtral / DeepSeek / Qwen via Hugging Face's Inference "
         "Router. Free tier: 1k req/day, paste a token from "
         "huggingface.co/settings/tokens."),
        # ── Codeium / Windsurf (OpenAI-compat) ─────────────────────
        ("codeium", "Codeium / Windsurf", "openai_compat",
         "https://server.codeium.com/v1", "api_key",
         "Codeium hosts Llama / Mixtral / GPT proxies (Pro tier). Paste a "
         "session token from codeium.com/account."),
    ]
    # GitHub Copilot's chat endpoint requires VS Code-flavoured headers
    # the operator can edit from the dashboard (e.g. when GitHub bumps
    # the accepted Editor-Plugin-Version). Stored under the well-known
    # ``dispatch_headers`` key that the cache layer merges into every
    # request. Defaults mirror a recent VS Code Copilot Chat install.
    _copilot_dispatch_headers = {
        "Editor-Version": "vscode/1.96.0",
        "Editor-Plugin-Version": "copilot-chat/0.27.0",
        "Copilot-Integration-Id": "vscode-chat",
        "User-Agent": "GithubCopilot/1.270.0",
        "Openai-Intent": "conversation-panel",
    }
    extra_meta_extras: dict[str, dict[str, Any]] = {
        "github_copilot": {"dispatch_headers": _copilot_dispatch_headers},
        "gemini": {"api_key_url": "https://aistudio.google.com/app/apikey"},
        "google": {"api_key_url": "https://aistudio.google.com/app/apikey"},
        "huggingface": {"api_key_url": "https://huggingface.co/settings/tokens"},
        "cohere": {"api_key_url": "https://dashboard.cohere.com/api-keys"},
        "groq": {"api_key_url": "https://console.groq.com/keys"},
        "mistral": {"api_key_url": "https://console.mistral.ai/api-keys/"},
        "perplexity": {"api_key_url": "https://www.perplexity.ai/settings/api"},
        "xai": {"api_key_url": "https://console.x.ai/"},
        "deepseek": {"api_key_url": "https://platform.deepseek.com/api_keys"},
        "openrouter": {"api_key_url": "https://openrouter.ai/keys"},
        "together_ai": {"api_key_url": "https://api.together.xyz/settings/api-keys"},
        "fireworks_ai": {"api_key_url": "https://app.fireworks.ai/users?tab=apiKeys"},
        "cerebras": {"api_key_url": "https://cloud.cerebras.ai/?action=apikey"},
        "sambanova": {"api_key_url": "https://cloud.sambanova.ai/apis"},
        "hyperbolic": {"api_key_url": "https://app.hyperbolic.xyz/settings"},
        "nvidia_nim": {"api_key_url": "https://build.nvidia.com/explore/discover"},
        "replicate": {"api_key_url": "https://replicate.com/account/api-tokens"},
        "codeium": {"api_key_url": "https://codeium.com/account"},
    }
    for slug, name, compat, base_url, auth_type, desc in extras:
        existing = await db.get(GatewayProvider, slug)
        if existing is not None:
            # Refresh fields that may evolve across deploys (e.g. we
            # tighten the schema or rename a field). Idempotent on the
            # JSONB equality check.
            if existing.auth_type != auth_type:
                existing.auth_type = auth_type
                existing.auth_schema = auth_schema_for(auth_type)
            if existing.compat != compat:
                existing.compat = compat
            if base_url and existing.base_url != base_url:
                existing.base_url = base_url
            extra = extra_meta_extras.get(slug)
            if extra:
                merged = dict(existing.extra_metadata or {})
                changed = False
                for k, v in extra.items():
                    if merged.get(k) != v:
                        merged[k] = v
                        changed = True
                if changed:
                    existing.extra_metadata = merged
            continue
        meta = {"description": desc}
        meta.update(extra_meta_extras.get(slug, {}))
        db.add(GatewayProvider(
            slug=slug, name=name, base_url=base_url, compat=compat,
            env_var=None, auth_type=auth_type,
            auth_schema=auth_schema_for(auth_type),
            extra_metadata=meta,
        ))
        created["providers"] += 1

    # Pure metadata refresh for providers that already exist via the
    # legacy ``_PROVIDER_ENV_KEYS`` map (gemini, cohere, groq, ...) -
    # we want their "Get key" helper URLs without re-creating the row.
    for slug, extra in extra_meta_extras.items():
        existing = await db.get(GatewayProvider, slug)
        if existing is None:
            continue
        merged = dict(existing.extra_metadata or {})
        changed = False
        for k, v in extra.items():
            if merged.get(k) != v:
                merged[k] = v
                changed = True
        if changed:
            existing.extra_metadata = merged


async def _seed_credentials(
    db: AsyncSession, created: dict[str, int],
) -> None:
    existing = (
        await db.execute(select(GatewayCredential).limit(1))
    ).first()
    if existing is not None:
        return

    # Refresh the provider list (we just inserted them).
    p_rows = (await db.execute(select(GatewayProvider))).scalars().all()
    for p in p_rows:
        if not p.env_var:
            continue
        # Try the env var + alts (stored in metadata).
        candidates = [p.env_var] + list(
            p.extra_metadata.get("alt_env_vars") or []
        )
        raw = next(
            (os.environ.get(v) for v in candidates if os.environ.get(v)),
            None,
        )
        if not raw:
            continue
        try:
            encrypted = encrypt_dict({"value": raw})
        except GatewayCipherError as exc:
            logger.warning(
                "bootstrap: skipping cred for %s (cipher error: %s)",
                p.slug, exc,
            )
            continue
        db.add(GatewayCredential(
            provider_slug=p.slug,
            label="Default",
            encrypted_value=encrypted,
            cipher_version=1,
            status="active",
            created_by="bootstrap",
        ))
        created["credentials"] += 1


async def _seed_models(
    db: AsyncSession, catalog: Any, created: dict[str, int],
) -> None:
    existing = (await db.execute(select(GatewayModel).limit(1))).first()
    if existing is not None:
        return
    if catalog is None:
        return
    p_slugs = {
        p.slug for p in (await db.execute(select(GatewayProvider))).scalars()
    }
    for alias, entry in catalog.all().items():
        provider = entry.provider.lower()
        if provider not in p_slugs and not entry.is_custom:
            # Skip a model whose provider is unknown - the schema's FK
            # would reject it. Operator can add it via the dashboard.
            logger.warning(
                "bootstrap: skipping model %s (unknown provider %s)",
                alias, provider,
            )
            continue
        db.add(GatewayModel(
            alias=alias,
            provider_slug=provider,
            real_model_id=entry.model,
            cost_per_1k_input_tokens=entry.cost_per_1k_input_tokens,
            cost_per_1k_output_tokens=entry.cost_per_1k_output_tokens,
            max_context_tokens=entry.max_context_tokens,
            is_custom=entry.is_custom,
            extra_metadata=entry.extra or {},
        ))
        created["models"] += 1


# (alias, provider_slug, real_model_id, max_context, cost_in/1k, cost_out/1k)
# Costs reflect public list prices at seeding time; operators tune per
# row in the dashboard. Keep this list ordered by provider for diffability.
_DEFAULT_ALIASES: list[tuple[str, str, str, int, float, float]] = [
    # ── AWS Bedrock ─────────────────────────────────────────────────
    ("bedrock-claude-sonnet-4", "aws_bedrock",
     "anthropic.claude-sonnet-4-20250514-v1:0", 200_000, 0.003, 0.015),
    ("bedrock-claude-haiku-3-5", "aws_bedrock",
     "anthropic.claude-3-5-haiku-20241022-v1:0", 200_000, 0.0008, 0.004),
    ("bedrock-llama-3-70b", "aws_bedrock",
     "meta.llama3-70b-instruct-v1:0", 8_192, 0.00265, 0.00350),
    ("bedrock-llama-3-8b", "aws_bedrock",
     "meta.llama3-8b-instruct-v1:0", 8_192, 0.0003, 0.0006),
    ("bedrock-mistral-large", "aws_bedrock",
     "mistral.mistral-large-2402-v1:0", 32_768, 0.004, 0.012),
    # ── Together AI ─────────────────────────────────────────────────
    ("together-llama-3-3-70b", "together_ai",
     "meta-llama/Llama-3.3-70B-Instruct-Turbo", 128_000, 0.00088, 0.00088),
    ("together-deepseek-v3", "together_ai",
     "deepseek-ai/DeepSeek-V3", 64_000, 0.00125, 0.00125),
    ("together-mixtral-8x22b", "together_ai",
     "mistralai/Mixtral-8x22B-Instruct-v0.1", 65_536, 0.0012, 0.0012),
    ("together-qwen-2-5-72b", "together_ai",
     "Qwen/Qwen2.5-72B-Instruct-Turbo", 32_768, 0.0012, 0.0012),
    # ── Fireworks AI ────────────────────────────────────────────────
    ("fireworks-llama-3-3-70b", "fireworks_ai",
     "accounts/fireworks/models/llama-v3p3-70b-instruct", 128_000,
     0.0009, 0.0009),
    ("fireworks-deepseek-v3", "fireworks_ai",
     "accounts/fireworks/models/deepseek-v3", 128_000, 0.0009, 0.0009),
    ("fireworks-mixtral-8x22b", "fireworks_ai",
     "accounts/fireworks/models/mixtral-8x22b-instruct", 65_536,
     0.0012, 0.0012),
    # ── Cerebras (ultra-fast) ───────────────────────────────────────
    ("cerebras-llama-3-3-70b", "cerebras",
     "llama-3.3-70b", 128_000, 0.00085, 0.0012),
    ("cerebras-llama-3-1-8b", "cerebras",
     "llama3.1-8b", 128_000, 0.0001, 0.0001),
    ("cerebras-llama-3-1-70b", "cerebras",
     "llama3.1-70b", 128_000, 0.00085, 0.0012),
    # ── SambaNova ───────────────────────────────────────────────────
    ("sambanova-llama-3-3-70b", "sambanova",
     "Meta-Llama-3.3-70B-Instruct", 128_000, 0.0006, 0.0012),
    ("sambanova-llama-3-1-405b", "sambanova",
     "Meta-Llama-3.1-405B-Instruct", 16_384, 0.005, 0.010),
    ("sambanova-deepseek-v3", "sambanova",
     "DeepSeek-V3-0324", 16_384, 0.001, 0.0015),
    # ── Hyperbolic ──────────────────────────────────────────────────
    ("hyperbolic-llama-3-3-70b", "hyperbolic",
     "meta-llama/Llama-3.3-70B-Instruct", 131_072, 0.0004, 0.0004),
    ("hyperbolic-deepseek-v3", "hyperbolic",
     "deepseek-ai/DeepSeek-V3", 131_072, 0.00025, 0.00025),
    ("hyperbolic-deepseek-r1", "hyperbolic",
     "deepseek-ai/DeepSeek-R1", 131_072, 0.002, 0.002),
    # ── NVIDIA NIM ──────────────────────────────────────────────────
    ("nim-llama-3-3-70b", "nvidia_nim",
     "meta/llama-3.3-70b-instruct", 128_000, 0.0, 0.0),
    ("nim-deepseek-r1", "nvidia_nim",
     "deepseek-ai/deepseek-r1", 128_000, 0.0, 0.0),
    ("nim-mistral-large", "nvidia_nim",
     "mistralai/mistral-large-2-instruct", 128_000, 0.0, 0.0),
    # ── Replicate ───────────────────────────────────────────────────
    ("replicate-llama-3-70b", "replicate",
     "meta/meta-llama-3-70b-instruct", 8_192, 0.00065, 0.00275),
    ("replicate-llama-3-8b", "replicate",
     "meta/meta-llama-3-8b-instruct", 8_192, 0.00005, 0.00025),
    # ── Google AI Studio (free tier) ────────────────────────────────
    ("gemini-2-0-flash", "gemini",
     "gemini-2.0-flash", 1_048_576, 0.0001, 0.0004),
    ("gemini-2-5-pro", "gemini",
     "gemini-2.5-pro", 2_097_152, 0.00125, 0.005),
    ("gemini-1-5-flash", "gemini",
     "gemini-1.5-flash", 1_048_576, 0.000075, 0.0003),
    # ── Google Vertex AI ────────────────────────────────────────────
    ("vertex-claude-sonnet-4", "vertex_ai",
     "claude-sonnet-4@anthropic", 200_000, 0.003, 0.015),
    ("vertex-claude-haiku-3-5", "vertex_ai",
     "claude-3-5-haiku@anthropic", 200_000, 0.0008, 0.004),
    ("vertex-gemini-2-0-flash", "vertex_ai",
     "gemini-2.0-flash", 1_048_576, 0.0001, 0.0004),
    ("vertex-gemini-2-5-pro", "vertex_ai",
     "gemini-2.5-pro", 2_097_152, 0.00125, 0.005),
    # ── Azure OpenAI (deployment names; operator overrides) ─────────
    ("azure-gpt-4o", "azure_openai",
     "gpt-4o", 128_000, 0.0025, 0.01),
    ("azure-gpt-4-turbo", "azure_openai",
     "gpt-4-turbo", 128_000, 0.01, 0.03),
    ("azure-gpt-4o-mini", "azure_openai",
     "gpt-4o-mini", 128_000, 0.00015, 0.0006),
    # ── Hugging Face Router ─────────────────────────────────────────
    ("hf-llama-3-3-70b", "huggingface",
     "meta-llama/Llama-3.3-70B-Instruct", 128_000, 0.0008, 0.0008),
    ("hf-mixtral-8x7b", "huggingface",
     "mistralai/Mixtral-8x7B-Instruct-v0.1", 32_768, 0.0006, 0.0006),
    ("hf-deepseek-r1", "huggingface",
     "deepseek-ai/DeepSeek-R1", 64_000, 0.002, 0.002),
    # ── Cohere ──────────────────────────────────────────────────────
    ("cohere-command-r", "cohere",
     "command-r", 128_000, 0.00015, 0.0006),
    ("cohere-command-r-plus", "cohere",
     "command-r-plus", 128_000, 0.0025, 0.01),
    # ── Codeium / Windsurf ──────────────────────────────────────────
    ("codeium-llama-3-3-70b", "codeium",
     "llama-3.3-70b-instruct", 128_000, 0.0, 0.0),
    # ── GitHub Copilot (free with Copilot subscription, no $/token) ─
    ("copilot-gpt-4o", "github_copilot",
     "gpt-4o", 128_000, 0.0, 0.0),
    ("copilot-gpt-4o-mini", "github_copilot",
     "gpt-4o-mini", 128_000, 0.0, 0.0),
    ("copilot-claude-3-5-sonnet", "github_copilot",
     "claude-3.5-sonnet", 200_000, 0.0, 0.0),
    ("copilot-claude-3-7-sonnet", "github_copilot",
     "claude-3.7-sonnet", 200_000, 0.0, 0.0),
    ("copilot-claude-sonnet-4", "github_copilot",
     "claude-sonnet-4", 200_000, 0.0, 0.0),
    ("copilot-gemini-2-0-flash", "github_copilot",
     "gemini-2.0-flash", 1_048_576, 0.0, 0.0),
    ("copilot-o1-mini", "github_copilot",
     "o1-mini", 128_000, 0.0, 0.0),
]


async def _seed_default_aliases(
    db: AsyncSession, created: dict[str, int],
) -> None:
    """Idempotent layer: drop ready-to-use aliases for the multi-vendor
    templates so that an operator who pasted an API key for, say,
    Together AI immediately sees ``together-llama-3-3-70b`` in the
    model picker. Skips an alias if already present (the dashboard's
    edits win over the seed) or if its provider row doesn't exist."""

    p_slugs = {
        p.slug for p in (await db.execute(select(GatewayProvider))).scalars()
    }
    for alias, provider, real, max_ctx, cin, cout in _DEFAULT_ALIASES:
        if provider not in p_slugs:
            continue
        if await db.get(GatewayModel, alias) is not None:
            continue
        db.add(GatewayModel(
            alias=alias,
            provider_slug=provider,
            real_model_id=real,
            cost_per_1k_input_tokens=cin,
            cost_per_1k_output_tokens=cout,
            max_context_tokens=max_ctx,
            is_custom=False,
            extra_metadata={"seeded": "default_template"},
        ))
        created["models"] += 1
