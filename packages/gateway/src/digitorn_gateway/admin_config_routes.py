"""Read-only endpoints exposing the gateway's runtime config to the
admin dashboard.

The gateway treats provider keys as env-var-managed and the model
catalogue as YAML-managed (``models.yaml``). These routes turn that
into a structured JSON the React dashboard can render. None of them
mutate state - to add a provider key the operator sets the env var
and restarts; to add a model alias they edit the YAML and reload.

Routes:

    GET /admin/providers   - canonical provider list + key-presence flag
    GET /admin/models      - model catalogue (alias -> provider/model/cost)

Both require admin role on the JWT.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from digitorn_gateway.auth import GatewayPrincipal, require_principal
from digitorn_gateway.llm_call import _PROVIDER_ENV_KEYS
from digitorn_gateway.models import get_catalog

logger = logging.getLogger(__name__)

router = APIRouter()


# Display names for the providers the gateway recognises. Keep in sync
# with ``_PROVIDER_ENV_KEYS`` in llm_call.py - any new entry there
# should get a label here so the dashboard renders something nicer than
# the bare slug.
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


def _require_admin(principal: GatewayPrincipal) -> None:
    if "admin" not in principal.roles:
        raise HTTPException(403, detail="admin_role_required")


@router.get("/admin/providers")
async def list_providers(
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """List of providers the gateway can route to + which have a key
    configured in the process env.

    The dashboard's Providers + Credentials pages consume this. The
    response intentionally never includes the actual key value, so
    any authenticated user may read it - the same way ``/v1/models``
    is reachable to all callers.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for slug, env_vars in _PROVIDER_ENV_KEYS.items():
        # Skip duplicate aliases (gemini / google share keys).
        if slug in seen:
            continue
        seen.add(slug)
        configured = any(os.environ.get(v) for v in env_vars)
        active_env = next(
            (v for v in env_vars if os.environ.get(v)), env_vars[0],
        )
        rows.append({
            "slug": slug,
            "name": _PROVIDER_LABELS.get(slug, slug.title()),
            "env_vars": list(env_vars),
            "active_env_var": active_env,
            "configured": configured,
            "status": "active" if configured else "missing_key",
        })
    rows.sort(key=lambda r: r["slug"])
    return {"count": len(rows), "rows": rows}


@router.get("/admin/models")
async def list_models(
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """Model catalogue exposed for the dashboard's Models + Routing pages.

    Each row mirrors one entry of ``models.yaml``. Pricing is the
    catalogue's published rate (same info LiteLLM exposes externally),
    so any authenticated user may read it - the same posture as
    ``/v1/models`` which is also non-admin.
    """
    catalog = get_catalog()
    rows: list[dict[str, Any]] = []
    for alias, entry in catalog.all().items():
        rows.append({
            "alias": alias,
            "provider": entry.provider,
            "model": entry.model,
            "is_custom": entry.is_custom,
            "cost_per_1k_input_tokens": entry.cost_per_1k_input_tokens,
            "cost_per_1k_output_tokens": entry.cost_per_1k_output_tokens,
            "max_context_tokens": entry.max_context_tokens,
            "extra": entry.extra or {},
        })
    rows.sort(key=lambda r: r["alias"])
    return {"count": len(rows), "rows": rows}
