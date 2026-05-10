"""Writable admin endpoints: providers, credentials, models, routes.

All routes require admin role. Mutations go through SQLAlchemy
transactions; the cipher module wraps every credential the moment the
plaintext leaves the request body. The dashboard never sees a key
again after creation.

Path layout::

    /admin/providers                GET, POST
    /admin/providers/{slug}         GET, PATCH, DELETE

    /admin/credentials              GET, POST
    /admin/credentials/{id}         GET, PATCH, DELETE
    /admin/credentials/{id}/rotate  POST
    /admin/credentials/{id}/test    POST   (best-effort live ping)

    /admin/models                   GET, POST
    /admin/models/{alias}           GET, PATCH, DELETE

    /admin/routes                   GET
    /admin/routes/{alias}           GET, PUT, DELETE

The ``GET /admin/providers`` and ``GET /admin/models`` here REPLACE
the read-only endpoints in ``admin_config_routes.py`` once we
register this router after that one (FastAPI keeps the LAST match,
so order matters in main.py).
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Path, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn_gateway.admin_writable_schema import (
    CredentialCreateIn,
    CredentialOut,
    CredentialPatchIn,
    CredentialRotateIn,
    ModelIn,
    ModelOut,
    ModelUpdate,
    ProviderIn,
    ProviderOut,
    ProviderUpdate,
    RouteCreateIn,
    RouteOut,
    RoutePatchIn,
    RoutePromoteIn,
    RouteSetIn,
)
from digitorn_gateway.auth import GatewayPrincipal, require_principal
from digitorn_gateway.auth_dispatchers import (
    schema_for as auth_schema_for,
    validate_secret_data,
)
from digitorn_gateway.cipher import (
    GatewayCipherError,
    decrypt_dict,
    encrypt_dict,
    mask,
    mask_dict,
)
from digitorn_gateway.config_cache import get_cache
from digitorn_gateway.config_redis import publish_invalidation
from digitorn_gateway.db import session_dependency
from digitorn_gateway.models_db import (
    GatewayCredential,
    GatewayModel,
    GatewayProvider,
    GatewayRoute,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_admin(principal: GatewayPrincipal) -> None:
    """Authorization gate for the writable admin endpoints.

    Accepts ``admin`` and ``developer`` roles. The latter is included
    because the dashboard's own JWTs (issued to operators by
    auth.digitorn.ai during initial deployment) carry the developer
    role; tightening this to admin-only would mean nobody could ever
    onboard the gateway through the UI. Locking it down further is a
    one-line change once a proper admin onboarding flow exists.
    """
    if not (principal.roles and (
        "admin" in principal.roles or "developer" in principal.roles
    )):
        raise HTTPException(403, detail="admin_role_required")


# ── Helpers ─────────────────────────────────────────────────────────


async def _provider_or_404(db: AsyncSession, slug: str) -> GatewayProvider:
    row = await db.get(GatewayProvider, slug)
    if row is None or row.archived_at is not None:
        raise HTTPException(404, detail=f"provider_not_found: {slug}")
    return row


async def _credential_or_404(db: AsyncSession, cid: uuid.UUID) -> GatewayCredential:
    row = await db.get(GatewayCredential, cid)
    if row is None:
        raise HTTPException(404, detail=f"credential_not_found: {cid}")
    return row


async def _model_or_404(db: AsyncSession, alias: str) -> GatewayModel:
    row = await db.get(GatewayModel, alias)
    if row is None or row.archived_at is not None:
        raise HTTPException(404, detail=f"model_not_found: {alias}")
    return row


async def _enrich_provider(
    db: AsyncSession, p: GatewayProvider,
) -> ProviderOut:
    """Compute live ``configured`` + credential_count for a provider."""
    cred_count = (
        await db.execute(
            select(func.count())
            .select_from(GatewayCredential)
            .where(
                GatewayCredential.provider_slug == p.slug,
                GatewayCredential.status == "active",
            )
        )
    ).scalar_one()
    out = ProviderOut.model_validate(p)
    has_env = bool(p.env_var and os.environ.get(p.env_var))
    out.configured = bool(cred_count) or has_env
    out.credential_count = int(cred_count)
    return out


# ── Providers ───────────────────────────────────────────────────────


@router.get("/admin/providers")
async def list_providers(
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """List active (non-archived) providers."""
    rows = (
        await db.execute(
            select(GatewayProvider)
            .where(GatewayProvider.archived_at.is_(None))
            .order_by(GatewayProvider.slug)
        )
    ).scalars().all()
    out = [await _enrich_provider(db, p) for p in rows]
    return {"count": len(out), "rows": [r.model_dump(by_alias=False) for r in out]}


@router.post("/admin/providers", status_code=201)
async def create_provider(
    body: ProviderIn,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    existing = await db.get(GatewayProvider, body.slug)
    if existing is not None and existing.archived_at is None:
        raise HTTPException(409, detail=f"provider_exists: {body.slug}")
    # Resolve the auth_schema from the auth_type when the caller didn't
    # supply one explicitly (the dashboard's "advanced" form lets ops
    # tweak the schema; the simple form just picks an auth_type).
    schema = body.auth_schema if body.auth_schema is not None else auth_schema_for(body.auth_type)
    if existing is not None:
        # Resurrect an archived row.
        existing.name = body.name
        existing.base_url = body.base_url
        existing.compat = body.compat
        existing.env_var = body.env_var
        existing.auth_type = body.auth_type
        existing.auth_schema = schema
        existing.extra_metadata = body.metadata
        existing.archived_at = None
        row = existing
    else:
        row = GatewayProvider(
            slug=body.slug,
            name=body.name,
            base_url=body.base_url,
            compat=body.compat,
            env_var=body.env_var,
            auth_type=body.auth_type,
            auth_schema=schema,
            extra_metadata=body.metadata,
        )
        db.add(row)
    await db.commit()
    await db.refresh(row)
    get_cache().upsert_provider(
        slug=row.slug, name=row.name, base_url=row.base_url,
        compat=row.compat, env_var=row.env_var,
        auth_type=row.auth_type,
        extra_metadata=row.extra_metadata,
    )
    await publish_invalidation("provider", key=row.slug)
    return (await _enrich_provider(db, row)).model_dump(by_alias=False)


@router.get("/admin/providers/{slug}")
async def get_provider(
    slug: str = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    row = await _provider_or_404(db, slug)
    return (await _enrich_provider(db, row)).model_dump(by_alias=False)


@router.get("/admin/providers/{slug}/upstream-models")
async def list_upstream_models(
    slug: str = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Fetch the live list of model ids exposed by this provider's API.

    Lets the dashboard's "Add model" form show a dropdown of real
    model ids the upstream offers right now (instead of asking the
    operator to type ``gpt-4o-mini`` from memory or paste from docs).

    Implementation:
      * Look up the provider's primary active credential in the cache.
      * Hit the provider's "list models" endpoint (varies per compat).
      * Map the JSON shape to a uniform ``{id, owned_by, created}`` row.
      * Returns ``[]`` (no error) when the provider has no
        decryptable credential or its compat dialect doesn't expose a
        list-models endpoint - the form falls back to manual entry.

    The response is intentionally tiny: most providers return up to a
    few dozen models, no pagination needed at the dashboard's scale.
    """
    _require_admin(principal)
    row = await _provider_or_404(db, slug)
    cache = get_cache()
    cred_id = cache._provider_default_cred.get(slug)
    if cred_id is None:
        return {"provider_slug": slug, "rows": [], "reason": "no_active_credential"}
    cred = cache._credentials.get(cred_id)
    if cred is None:
        return {"provider_slug": slug, "rows": [], "reason": "credential_unreadable"}

    secret = dict(cred.secret_data or {})
    api_key = secret.get("api_key") or secret.get("value") or secret.get("access_token") or ""
    base_url = (row.base_url or "").rstrip("/")
    compat = row.compat
    timeout = httpx.Timeout(15.0, connect=5.0)

    # Compose the right request per dialect. Anything we can't easily
    # introspect returns ``[]`` with a hint -- the dashboard then
    # shows the manual real_model_id input.
    url: str | None = None
    headers: dict[str, str] = {}
    if compat == "openai" or compat == "openai_compat":
        # Standard OpenAI-shape /models endpoint. Works for OpenAI,
        # DeepSeek, xAI, Together, Fireworks, Cerebras, NVIDIA NIM,
        # Mistral, Groq, Hyperbolic, SambaNova, HuggingFace router,
        # Codeium, etc. Just need the right base_url.
        canonical_base = base_url or {
            "openai": "https://api.openai.com/v1",
            "deepseek": "https://api.deepseek.com/v1",
            "xai": "https://api.x.ai/v1",
            "mistral": "https://api.mistral.ai/v1",
            "groq": "https://api.groq.com/openai/v1",
            "perplexity": "https://api.perplexity.ai",
        }.get(slug, "")
        if not canonical_base:
            return {"provider_slug": slug, "rows": [], "reason": "no_base_url"}
        url = f"{canonical_base}/models"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    elif compat == "anthropic":
        url = (base_url or "https://api.anthropic.com") + "/v1/models"
        if api_key:
            headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        # OAuth (claude_code): Bearer token + special betas
        if row.auth_type == "claude_code":
            headers.pop("x-api-key", None)
            headers["Authorization"] = f"Bearer {api_key}"
            headers["anthropic-beta"] = (
                "oauth-2025-04-20,claude-code-20250219"
            )
    elif compat == "bedrock":
        # AWS Bedrock has its own list-foundation-models call signed
        # with SigV4 — out of scope for this generic fetcher; the
        # operator types the model id explicitly.
        return {"provider_slug": slug, "rows": [], "reason": "bedrock_uses_sigv4"}
    elif compat == "vertex_ai":
        return {"provider_slug": slug, "rows": [], "reason": "vertex_uses_sa_json"}
    elif compat == "azure":
        return {"provider_slug": slug, "rows": [], "reason": "azure_per_deployment"}
    else:
        return {"provider_slug": slug, "rows": [], "reason": f"unknown_compat:{compat}"}

    # Provider-level dispatch headers (e.g. Copilot Editor-Version).
    extra = (row.extra_metadata or {}).get("dispatch_headers") or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            if isinstance(k, str) and v is not None:
                headers.setdefault(k, str(v))

    if not url:
        return {"provider_slug": slug, "rows": [], "reason": "no_url"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
    except httpx.RequestError as exc:
        raise HTTPException(
            502,
            detail=f"upstream_unreachable: {type(exc).__name__}: {exc}",
        )
    if resp.status_code == 401 or resp.status_code == 403:
        return {
            "provider_slug": slug, "rows": [],
            "reason": f"upstream_auth_failed_{resp.status_code}",
        }
    if resp.status_code >= 400:
        raise HTTPException(
            resp.status_code,
            detail=f"upstream_error: {resp.text[:200]}",
        )
    try:
        body = resp.json()
    except ValueError:
        return {"provider_slug": slug, "rows": [], "reason": "non_json"}

    # Normalise the response shape. OpenAI + most compat providers
    # use ``{"data": [{"id": "...", "owned_by": "...", "created": ...}]}``.
    # Anthropic returns ``{"data": [{"id": "...", "type": "model",
    # "display_name": "...", "created_at": "..."}]}``.
    raw = body.get("data") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        return {"provider_slug": slug, "rows": [], "reason": "unexpected_shape"}

    rows: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rows.append({
            "id": str(entry.get("id", "")),
            "display_name": str(
                entry.get("display_name") or entry.get("name") or entry.get("id", "")
            ),
            "owned_by": str(entry.get("owned_by", "")),
            "created": entry.get("created") or entry.get("created_at"),
        })
    rows = [r for r in rows if r["id"]]
    rows.sort(key=lambda r: r["id"])
    return {"provider_slug": slug, "count": len(rows), "rows": rows}


@router.patch("/admin/providers/{slug}")
async def update_provider(
    body: ProviderUpdate,
    slug: str = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    row = await _provider_or_404(db, slug)
    if body.name is not None:
        row.name = body.name
    if body.base_url is not None:
        row.base_url = body.base_url
    if body.compat is not None:
        row.compat = body.compat
    if body.env_var is not None:
        row.env_var = body.env_var
    if body.auth_type is not None:
        row.auth_type = body.auth_type
        if body.auth_schema is None:
            row.auth_schema = auth_schema_for(body.auth_type)
    if body.auth_schema is not None:
        row.auth_schema = body.auth_schema
    if body.metadata is not None:
        row.extra_metadata = body.metadata
    await db.commit()
    await db.refresh(row)
    get_cache().upsert_provider(
        slug=row.slug, name=row.name, base_url=row.base_url,
        compat=row.compat, env_var=row.env_var,
        auth_type=row.auth_type,
        extra_metadata=row.extra_metadata,
    )
    await publish_invalidation("provider", key=row.slug)
    return (await _enrich_provider(db, row)).model_dump(by_alias=False)


@router.delete("/admin/providers/{slug}")
async def delete_provider(
    slug: str = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    row = await _provider_or_404(db, slug)
    # Block delete when models reference this provider.
    in_use = (
        await db.execute(
            select(func.count()).select_from(GatewayModel).where(
                GatewayModel.provider_slug == slug,
                GatewayModel.archived_at.is_(None),
            )
        )
    ).scalar_one()
    if in_use:
        raise HTTPException(
            409,
            detail=f"provider_in_use: {in_use} model(s) still reference '{slug}'",
        )
    # Cascade deletes credentials (FK ON DELETE CASCADE).
    await db.delete(row)
    await db.commit()
    get_cache().remove_provider(slug)
    await publish_invalidation("provider", key=slug)
    return {"deleted": True, "slug": slug}


# ── Credentials ─────────────────────────────────────────────────────


def _to_credential_out(row: GatewayCredential) -> CredentialOut:
    """Decrypt only the masked preview - not the full plaintext."""
    try:
        secret = decrypt_dict(row.encrypted_value)
        masked_fields = mask_dict(secret)
        # Pick the most "primary" field for the legacy single-string preview.
        primary = (
            secret.get("value")
            or secret.get("api_key")
            or secret.get("access_token")
            or secret.get("password")
            or next(iter(secret.values()), "")
        )
        masked = mask(primary) if primary else "***"
    except GatewayCipherError as exc:
        logger.error("decrypt_failed cred_id=%s: %s", row.id, exc)
        masked = "***unreadable***"
        masked_fields = {}
    return CredentialOut(
        id=row.id,
        provider_slug=row.provider_slug,
        label=row.label,
        masked_value=masked,
        masked_fields=masked_fields,
        status=row.status,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        created_by=row.created_by,
        live_pool=getattr(row, "live_pool", True),
    )


def _resolve_secret_data(
    body_raw: str | None,
    body_secret: dict[str, str] | None,
    auth_type: str,
) -> dict[str, str]:
    """Normalise the create/rotate payload into a single ``secret_data``
    dict, then validate against the provider's ``auth_type`` schema.

    Accepts either ``raw_value`` (legacy single-string) or
    ``secret_data`` (multi-field). Empty/missing fields raise 400.
    """
    if body_secret is None:
        if body_raw is None or body_raw == "":
            raise HTTPException(
                400, detail="credential_payload_empty: provide raw_value or secret_data",
            )
        # Legacy: map the single string into the canonical 'value'
        # field for ``api_key`` providers, or 'access_token' for
        # OAuth-shaped ones.
        primary_field = (
            "access_token" if auth_type in ("oauth2", "claude_code")
            else "value"
        )
        body_secret = {primary_field: body_raw}
    ok, missing = validate_secret_data(auth_type, body_secret)
    if not ok:
        raise HTTPException(
            400,
            detail=f"missing_required_fields: {','.join(missing)} (auth_type={auth_type})",
        )
    return body_secret


@router.get("/admin/credentials")
async def list_credentials(
    principal: GatewayPrincipal = Depends(require_principal),
    provider_slug: str | None = None,
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    stmt = select(GatewayCredential)
    if provider_slug:
        stmt = stmt.where(GatewayCredential.provider_slug == provider_slug)
    rows = (await db.execute(stmt.order_by(GatewayCredential.created_at.desc()))).scalars().all()
    return {
        "count": len(rows),
        "rows": [_to_credential_out(r).model_dump(mode="json") for r in rows],
    }


@router.post("/admin/credentials", status_code=201)
async def create_credential(
    body: CredentialCreateIn,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    provider = await _provider_or_404(db, body.provider_slug)
    secret = _resolve_secret_data(
        body.raw_value, body.secret_data, provider.auth_type,
    )
    try:
        encrypted = encrypt_dict(secret)
    except GatewayCipherError as exc:
        raise HTTPException(500, detail=f"cipher_error: {exc}") from exc
    row = GatewayCredential(
        provider_slug=body.provider_slug,
        label=body.label,
        encrypted_value=encrypted,
        cipher_version=1,
        status="active",
        created_by=principal.user_id,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409,
            detail=f"credential_label_exists: {body.provider_slug}/{body.label}",
        )
    await db.refresh(row)
    get_cache().upsert_credential(
        row.id,
        provider_slug=row.provider_slug,
        label=row.label,
        secret_data=secret,
        status=row.status,
        live_pool=getattr(row, "live_pool", True),
    )
    await publish_invalidation("credential", key=str(row.id))
    return _to_credential_out(row).model_dump(mode="json")


# NOTE: ``/admin/credentials/health`` MUST be declared BEFORE
# ``/admin/credentials/{cred_id}`` -- FastAPI matches the first route
# definition that accepts the path. With ``{cred_id}`` first, a request
# for ``/admin/credentials/health`` is parsed as cred_id="health" and
# 422s on UUID validation. Keep this aggregate route at the top.
@router.get("/admin/credentials/health")
async def all_credentials_health(
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """Live multi-account health: per-credential in-flight count + 429
    cooldown state. Drives the dashboard "Account A: 3 in-flight, 0
    throttled / Account B: 5 in-flight, blocked 42s" tile and powers
    the production alerting (e.g. fire when any active credential
    has been blocked for >5 min - probable revoked key or quota
    exhausted at the provider)."""
    _require_admin(principal)
    cache = get_cache()
    snap = cache.credential_health_snapshot()
    rows = []
    for cid, h in snap.items():
        cred = cache._credentials.get(cid)
        rows.append({
            "cred_id": str(cid),
            "label": cred.label if cred else None,
            "provider_slug": cred.provider_slug if cred else None,
            "status": cred.status if cred else None,
            "inflight": h["inflight"],
            "consecutive_429s": h["consecutive_429s"],
            "is_429_blocked": h["is_429_blocked"],
            "blocked_for_s": h["blocked_for_s"],
            "total_dispatched": h["total_dispatched"],
        })
    rows.sort(
        key=lambda r: (r["provider_slug"] or "", r["label"] or ""),
    )
    return {"count": len(rows), "rows": rows}


@router.get("/admin/credentials/{cred_id}")
async def get_credential(
    cred_id: uuid.UUID = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    row = await _credential_or_404(db, cred_id)
    return _to_credential_out(row).model_dump(mode="json")


@router.get("/admin/credentials/{cred_id}/pool-stats")
async def credential_pool_stats(
    cred_id: uuid.UUID = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """Live pool stats for one credential. Drives the dashboard's
    "warm 12m, last hit 4s ago, 1234 hits" line under the toggle."""
    _require_admin(principal)
    from digitorn_gateway.connection_pool import get_pool
    s = get_pool().stats(cred_id)
    return {
        "cred_id": s.cred_id,
        "warm": s.warm,
        "warm_age_s": s.warm_age_s,
        "last_used_age_s": s.last_used_age_s,
        "hit_count": s.hit_count,
    }


@router.get("/admin/pool-stats")
async def all_pool_stats(
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """Aggregate pool snapshot. Lets the dashboard show a global view
    of which credentials are warm and how many TCP connections are
    currently held open."""
    _require_admin(principal)
    from digitorn_gateway.connection_pool import get_pool
    rows = [
        {
            "cred_id": s.cred_id,
            "warm": s.warm,
            "warm_age_s": s.warm_age_s,
            "last_used_age_s": s.last_used_age_s,
            "hit_count": s.hit_count,
        }
        for s in get_pool().all_stats()
    ]
    return {"count": len(rows), "rows": rows}


@router.get("/admin/credentials/{cred_id}/health")
async def credential_health(
    cred_id: uuid.UUID = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """Single-credential health drill-down. Use ``/admin/credentials/
    health`` for the aggregate view."""
    _require_admin(principal)
    cache = get_cache()
    snap = cache.credential_health_snapshot().get(cred_id)
    if snap is None:
        return {
            "cred_id": str(cred_id),
            "inflight": 0,
            "consecutive_429s": 0,
            "is_429_blocked": False,
            "blocked_for_s": 0.0,
            "total_dispatched": 0,
        }
    return {"cred_id": str(cred_id), **snap}


@router.patch("/admin/credentials/{cred_id}")
async def patch_credential(
    body: CredentialPatchIn,
    cred_id: uuid.UUID = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    row = await _credential_or_404(db, cred_id)
    if body.label is not None:
        row.label = body.label
    if body.status is not None:
        row.status = body.status
    if body.live_pool is not None:
        row.live_pool = body.live_pool
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail="credential_label_exists")
    await db.refresh(row)
    try:
        secret = decrypt_dict(row.encrypted_value)
    except GatewayCipherError:
        secret = {}
    get_cache().upsert_credential(
        row.id,
        provider_slug=row.provider_slug,
        label=row.label,
        secret_data=secret,
        status=row.status,
        live_pool=getattr(row, "live_pool", True),
    )
    await publish_invalidation("credential", key=str(row.id))
    # When the operator toggles live_pool off, evict the warm client
    # so we don't keep an idle socket open. Toggle on is lazy: the
    # next dispatch picks up the warm client.
    try:
        from digitorn_gateway.connection_pool import get_pool
        get_pool().on_credential_changed(row.id, getattr(row, "live_pool", True))
    except Exception:  # pragma: no cover - pool absent in tests
        pass
    return _to_credential_out(row).model_dump(mode="json")


@router.post("/admin/credentials/{cred_id}/rotate")
async def rotate_credential(
    body: CredentialRotateIn,
    cred_id: uuid.UUID = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    row = await _credential_or_404(db, cred_id)
    provider = await _provider_or_404(db, row.provider_slug)
    secret = _resolve_secret_data(
        body.raw_value, body.secret_data, provider.auth_type,
    )
    try:
        row.encrypted_value = encrypt_dict(secret)
        row.cipher_version = 1
        row.status = "active"
    except GatewayCipherError as exc:
        raise HTTPException(500, detail=f"cipher_error: {exc}") from exc
    await db.commit()
    await db.refresh(row)
    get_cache().upsert_credential(
        row.id,
        provider_slug=row.provider_slug,
        label=row.label,
        secret_data=secret,
        status=row.status,
        live_pool=getattr(row, "live_pool", True),
    )
    await publish_invalidation("credential", key=str(row.id))
    # Rotated key -> evict the warm client if any so the next dispatch
    # picks up the new credential bytes (the pooled httpx client doesn't
    # know about the new bearer otherwise).
    try:
        from digitorn_gateway.connection_pool import get_pool
        get_pool().invalidate(row.id)
    except Exception:  # pragma: no cover
        pass
    return _to_credential_out(row).model_dump(mode="json")


@router.delete("/admin/credentials/{cred_id}")
async def delete_credential(
    cred_id: uuid.UUID = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    row = await _credential_or_404(db, cred_id)
    # Block delete when a route currently uses this credential.
    in_use = (
        await db.execute(
            select(func.count()).select_from(GatewayRoute).where(
                GatewayRoute.credential_id == row.id,
            )
        )
    ).scalar_one()
    if in_use:
        raise HTTPException(
            409,
            detail=f"credential_in_use: {in_use} route(s) point at it",
        )
    await db.delete(row)
    await db.commit()
    get_cache().remove_credential(row.id)
    await publish_invalidation("credential", key=str(row.id))
    try:
        from digitorn_gateway.connection_pool import get_pool
        get_pool().invalidate(row.id)
    except Exception:  # pragma: no cover
        pass
    return {"deleted": True, "id": str(row.id)}


# ── Models ──────────────────────────────────────────────────────────


def _to_model_out(row: GatewayModel) -> ModelOut:
    return ModelOut.model_validate(row)


@router.get("/admin/models")
async def list_models(
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(GatewayModel)
            .where(GatewayModel.archived_at.is_(None))
            .order_by(GatewayModel.alias)
        )
    ).scalars().all()
    return {
        "count": len(rows),
        "rows": [_to_model_out(r).model_dump(by_alias=False) for r in rows],
    }


@router.post("/admin/models", status_code=201)
async def create_model(
    body: ModelIn,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    await _provider_or_404(db, body.provider_slug)
    existing = await db.get(GatewayModel, body.alias)
    if existing is not None and existing.archived_at is None:
        raise HTTPException(409, detail=f"model_exists: {body.alias}")
    if existing is not None:
        existing.provider_slug = body.provider_slug
        existing.real_model_id = body.real_model_id
        existing.cost_per_1k_input_tokens = body.cost_per_1k_input_tokens
        existing.cost_per_1k_output_tokens = body.cost_per_1k_output_tokens
        existing.max_context_tokens = body.max_context_tokens
        existing.is_custom = body.is_custom
        existing.extra_metadata = body.metadata
        existing.archived_at = None
        row = existing
    else:
        row = GatewayModel(
            alias=body.alias,
            provider_slug=body.provider_slug,
            real_model_id=body.real_model_id,
            cost_per_1k_input_tokens=body.cost_per_1k_input_tokens,
            cost_per_1k_output_tokens=body.cost_per_1k_output_tokens,
            max_context_tokens=body.max_context_tokens,
            is_custom=body.is_custom,
            extra_metadata=body.metadata,
        )
        db.add(row)
    await db.commit()
    await db.refresh(row)
    get_cache().upsert_model(
        alias=row.alias,
        provider_slug=row.provider_slug,
        real_model_id=row.real_model_id,
        cost_per_1k_input=float(row.cost_per_1k_input_tokens),
        cost_per_1k_output=float(row.cost_per_1k_output_tokens),
        max_context=row.max_context_tokens,
        is_custom=row.is_custom,
    )
    await publish_invalidation("model", key=row.alias)
    return _to_model_out(row).model_dump(by_alias=False)


@router.get("/admin/models/{alias:path}")
async def get_model(
    alias: str = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    row = await _model_or_404(db, alias)
    return _to_model_out(row).model_dump(by_alias=False)


@router.patch("/admin/models/{alias:path}")
async def update_model(
    body: ModelUpdate,
    alias: str = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    row = await _model_or_404(db, alias)
    if body.provider_slug is not None:
        await _provider_or_404(db, body.provider_slug)
        row.provider_slug = body.provider_slug
    if body.real_model_id is not None:
        row.real_model_id = body.real_model_id
    if body.cost_per_1k_input_tokens is not None:
        row.cost_per_1k_input_tokens = body.cost_per_1k_input_tokens
    if body.cost_per_1k_output_tokens is not None:
        row.cost_per_1k_output_tokens = body.cost_per_1k_output_tokens
    if body.max_context_tokens is not None:
        row.max_context_tokens = body.max_context_tokens
    if body.is_custom is not None:
        row.is_custom = body.is_custom
    if body.metadata is not None:
        row.extra_metadata = body.metadata
    await db.commit()
    await db.refresh(row)
    get_cache().upsert_model(
        alias=row.alias,
        provider_slug=row.provider_slug,
        real_model_id=row.real_model_id,
        cost_per_1k_input=float(row.cost_per_1k_input_tokens),
        cost_per_1k_output=float(row.cost_per_1k_output_tokens),
        max_context=row.max_context_tokens,
        is_custom=row.is_custom,
    )
    await publish_invalidation("model", key=row.alias)
    return _to_model_out(row).model_dump(by_alias=False)


@router.delete("/admin/models/{alias:path}")
async def delete_model(
    alias: str = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    row = await _model_or_404(db, alias)
    # Cascade deletes route (FK ON DELETE CASCADE).
    await db.delete(row)
    await db.commit()
    get_cache().remove_model(alias)
    await publish_invalidation("model", key=alias)
    return {"deleted": True, "alias": alias}


# ── Routes ──────────────────────────────────────────────────────────


async def _hydrate_route(db: AsyncSession, r: GatewayRoute) -> RouteOut:
    cred = await db.get(GatewayCredential, r.credential_id)
    health = get_cache().route_health_snapshot().get(r.id, {})
    return RouteOut(
        id=r.id,
        model_alias=r.model_alias,
        credential_id=r.credential_id,
        priority=r.priority,
        updated_at=r.updated_at,
        provider_slug=r.provider_slug,
        real_model_id=r.real_model_id,
        compat=r.compat,
        base_url=r.base_url,
        dispatch_headers=dict(r.dispatch_headers or {}),
        credential_label=cred.label if cred else None,
        credential_provider_slug=cred.provider_slug if cred else None,
        is_blocked=bool(health.get("is_blocked", False)),
        consecutive_failures=int(health.get("consecutive_failures", 0)),
        last_error=str(health.get("last_error", "")),
    )


async def _resolve_route_identity(
    db: AsyncSession,
    *,
    model: GatewayModel,
    cred: GatewayCredential,
    provider_slug: str | None,
    real_model_id: str | None,
    compat: str | None,
    base_url: str | None,
    dispatch_headers: dict[str, str] | None,
) -> tuple[str, str, str, str | None, dict[str, Any]]:
    """Validate + fill in the per-route dispatch identity.

    The user provides 0..N override fields; we resolve the rest from
    the model + provider rows. Final tuple is always fully-populated:
    ``(provider_slug, real_model_id, compat, base_url, dispatch_headers)``.
    Raises HTTP 400 when the credential's provider doesn't match the
    final route ``provider_slug``."""
    final_provider_slug = provider_slug or model.provider_slug
    if cred.provider_slug != final_provider_slug:
        raise HTTPException(
            400,
            detail=(
                f"provider_mismatch: route targets provider "
                f"'{final_provider_slug}' but credential is for "
                f"'{cred.provider_slug}'"
            ),
        )
    # Walk the provider row only AFTER the cred check passes - the
    # provider must exist, otherwise the FK on the new column would
    # blow up at commit anyway.
    p = await _provider_or_404(db, final_provider_slug)
    final_real_model_id = real_model_id or model.real_model_id
    final_compat = compat or p.compat
    final_base_url = base_url if base_url is not None else p.base_url
    if dispatch_headers is None:
        # Inherit the provider's dispatch_headers metadata so the route
        # behaviour matches the legacy single-provider path. Operator
        # can override later via PATCH.
        provider_meta_headers = (p.extra_metadata or {}).get(
            "dispatch_headers"
        ) or {}
        if isinstance(provider_meta_headers, dict):
            final_headers: dict[str, Any] = {
                k: str(v) for k, v in provider_meta_headers.items()
                if isinstance(k, str) and v is not None
            }
        else:
            final_headers = {}
    else:
        final_headers = {
            k: str(v) for k, v in dispatch_headers.items()
            if isinstance(k, str) and v is not None
        }
    return (
        final_provider_slug, final_real_model_id, final_compat,
        final_base_url, final_headers,
    )


@router.get("/admin/routes")
async def list_routes(
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
    model_alias: str | None = None,
) -> dict[str, Any]:
    """List every route, ordered by alias then priority. Filter to a
    single alias with ``?model_alias=...``."""
    stmt = select(GatewayRoute).order_by(
        GatewayRoute.model_alias, GatewayRoute.priority,
    )
    if model_alias:
        stmt = stmt.where(GatewayRoute.model_alias == model_alias)
    rows = (await db.execute(stmt)).scalars().all()
    out = [(await _hydrate_route(db, r)).model_dump(mode="json") for r in rows]
    return {"count": len(out), "rows": out}


@router.post("/admin/routes", status_code=201)
async def create_route(
    body: RouteCreateIn,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Add a NEW route at the requested priority. The (alias, priority)
    pair must be unique - to overwrite an existing slot use PATCH.

    Cross-provider routing: the route can target a DIFFERENT provider
    than the alias's metadata (``model.provider_slug``). When
    ``provider_slug`` / ``real_model_id`` / ``compat`` / ``base_url`` /
    ``dispatch_headers`` are omitted, the route inherits the alias +
    provider defaults. The credential must match the FINAL
    ``provider_slug`` (after override resolution)."""
    _require_admin(principal)
    model = await _model_or_404(db, body.model_alias)
    cred = await _credential_or_404(db, body.credential_id)
    (
        final_provider, final_model_id, final_compat,
        final_base_url, final_headers,
    ) = await _resolve_route_identity(
        db,
        model=model, cred=cred,
        provider_slug=body.provider_slug,
        real_model_id=body.real_model_id,
        compat=body.compat,
        base_url=body.base_url,
        dispatch_headers=body.dispatch_headers,
    )
    row = GatewayRoute(
        model_alias=body.model_alias,
        credential_id=body.credential_id,
        priority=body.priority,
        provider_slug=final_provider,
        real_model_id=final_model_id,
        compat=final_compat,
        base_url=final_base_url,
        dispatch_headers=final_headers,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # Multi-account routing: collision is on (alias, credential_id),
        # not (alias, priority). Several creds can share the same
        # priority (load-balance tier); they just cannot bind the same
        # credential to the same alias twice.
        await db.rollback()
        raise HTTPException(
            409,
            detail=(
                f"route_credential_taken: model_alias={body.model_alias} "
                f"credential_id={body.credential_id} - this credential "
                f"is already bound to this alias. Use PATCH to update "
                f"its priority/identity, or DELETE first."
            ),
        )
    await db.refresh(row)
    get_cache().set_route(
        row.id,
        alias=row.model_alias,
        credential_id=row.credential_id,
        priority=row.priority,
        provider_slug=row.provider_slug,
        real_model_id=row.real_model_id,
        compat=row.compat,
        base_url=row.base_url,
        dispatch_headers=dict(row.dispatch_headers or {}),
    )
    await publish_invalidation("route", key=str(row.id))
    return (await _hydrate_route(db, row)).model_dump(mode="json")


@router.patch("/admin/routes/{route_id}")
async def patch_route(
    body: RoutePatchIn,
    route_id: uuid.UUID = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    row = await db.get(GatewayRoute, route_id)
    if row is None:
        raise HTTPException(404, detail=f"route_not_found: {route_id}")

    # Build the (possibly partial) target identity. We need the
    # post-patch values to validate cred / provider consistency
    # together rather than one field at a time.
    target_provider = body.provider_slug or row.provider_slug
    target_cred_id = body.credential_id or row.credential_id
    cred = await _credential_or_404(db, target_cred_id)
    if cred.provider_slug != target_provider:
        raise HTTPException(
            400,
            detail=(
                f"provider_mismatch: route would target '{target_provider}'"
                f" but credential is for '{cred.provider_slug}'"
            ),
        )
    if body.provider_slug is not None:
        # The provider must exist (and be active).
        await _provider_or_404(db, body.provider_slug)
        row.provider_slug = body.provider_slug
    if body.credential_id is not None:
        row.credential_id = body.credential_id
    if body.priority is not None:
        row.priority = body.priority
    if body.real_model_id is not None:
        row.real_model_id = body.real_model_id
    if body.compat is not None:
        row.compat = body.compat
    if body.base_url is not None:
        row.base_url = body.base_url or None
    if body.dispatch_headers is not None:
        row.dispatch_headers = {
            k: str(v) for k, v in body.dispatch_headers.items()
            if isinstance(k, str) and v is not None
        }
    try:
        await db.commit()
    except IntegrityError:
        # See create_route for the rationale: multi-account collisions
        # are on (alias, credential_id), not (alias, priority).
        await db.rollback()
        raise HTTPException(409, detail="route_credential_taken")
    await db.refresh(row)
    get_cache().set_route(
        row.id,
        alias=row.model_alias,
        credential_id=row.credential_id,
        priority=row.priority,
        provider_slug=row.provider_slug,
        real_model_id=row.real_model_id,
        compat=row.compat,
        base_url=row.base_url,
        dispatch_headers=dict(row.dispatch_headers or {}),
    )
    await publish_invalidation("route", key=str(row.id))
    return (await _hydrate_route(db, row)).model_dump(mode="json")


@router.post("/admin/routes/{route_id}/promote")
async def promote_route(
    route_id: uuid.UUID = Path(...),
    body: RoutePromoteIn = Body(default=RoutePromoteIn()),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """1-click "make this route primary" for the dashboard.

    Bumps the chosen route to priority 0. With ``shift_existing=True``
    every other route on the same alias is shifted down by one slot.
    With ``shift_existing=False`` the route is simply set to 0 - and
    other routes already at priority 0 stay there (multi-account
    routing: same-tier routes are load-balanced)."""
    _require_admin(principal)
    row = await db.get(GatewayRoute, route_id)
    if row is None:
        raise HTTPException(404, detail=f"route_not_found: {route_id}")
    alias = row.model_alias

    if not body.shift_existing:
        # Force priority 0. Multi-account allows several routes at the
        # same priority, so this can no longer trip the unique
        # constraint -- the only remaining 409 path is
        # (alias, credential_id) duplication, which the row.id check
        # rules out.
        row.priority = 0
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, detail="route_credential_taken")
        await db.refresh(row)
        get_cache().set_route(
            row.id,
            alias=row.model_alias, credential_id=row.credential_id,
            priority=row.priority, provider_slug=row.provider_slug,
            real_model_id=row.real_model_id, compat=row.compat,
            base_url=row.base_url,
            dispatch_headers=dict(row.dispatch_headers or {}),
        )
        await publish_invalidation("route", key=str(row.id))
        return (await _hydrate_route(db, row)).model_dump(mode="json")

    # Pull every route for the alias, sorted by current priority. The
    # promoted route slides to position 0; the rest keep their relative
    # order but shift down. Since we're updating multiple rows under a
    # ``UNIQUE (alias, priority)`` constraint, we move them through
    # negative scratch slots first so no two rows ever collide.
    siblings = (
        await db.execute(
            select(GatewayRoute)
            .where(GatewayRoute.model_alias == alias)
            .order_by(GatewayRoute.priority)
        )
    ).scalars().all()
    if len(siblings) == 1:
        # Already the only route - just make sure it's at 0.
        if siblings[0].priority != 0:
            siblings[0].priority = 0
            await db.commit()
            await db.refresh(siblings[0])
        get_cache().set_route(
            siblings[0].id,
            alias=siblings[0].model_alias,
            credential_id=siblings[0].credential_id,
            priority=siblings[0].priority,
            provider_slug=siblings[0].provider_slug,
            real_model_id=siblings[0].real_model_id,
            compat=siblings[0].compat,
            base_url=siblings[0].base_url,
            dispatch_headers=dict(siblings[0].dispatch_headers or {}),
        )
        await publish_invalidation("route", key=str(siblings[0].id))
        return (await _hydrate_route(db, siblings[0])).model_dump(mode="json")

    # Phase 1: park every sibling at unique negative slots so the
    # final assignment can't collide.
    for i, r in enumerate(siblings):
        r.priority = -(i + 1)
    await db.flush()

    # Phase 2: settle the new ascending order with the promoted row first.
    new_order: list[GatewayRoute] = [row]
    for r in siblings:
        if r.id != row.id:
            new_order.append(r)
    for i, r in enumerate(new_order):
        r.priority = i
    await db.commit()

    # Refresh the promoted row + repopulate the cache for every sibling
    # so the in-memory state matches the DB exactly.
    for r in new_order:
        await db.refresh(r)
        get_cache().set_route(
            r.id,
            alias=r.model_alias, credential_id=r.credential_id,
            priority=r.priority, provider_slug=r.provider_slug,
            real_model_id=r.real_model_id, compat=r.compat,
            base_url=r.base_url,
            dispatch_headers=dict(r.dispatch_headers or {}),
        )
    # One bulk publish at the end -- subscribers reload the whole DB
    # anyway, so emitting N publishes for the N rows we just shifted is
    # wasteful. The "alias" key carries the impacted alias for audit /
    # future granular reloads.
    await publish_invalidation("route", key=alias)
    return (await _hydrate_route(db, row)).model_dump(mode="json")


@router.put("/admin/routes/{alias:path}")
async def set_route(
    body: RouteSetIn,
    alias: str = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Backwards-compat shortcut: set the primary (priority=0) route
    for a model. Drops any existing priority-0 row, leaves higher
    fallbacks alone.

    Cross-provider routing: pass ``provider_slug`` (and any of the
    other identity overrides) to swap the primary onto a different
    provider in a single PUT. Omitted fields fall back to the alias's
    metadata defaults so the legacy form ``PUT {credential_id: X}``
    keeps working unchanged."""
    _require_admin(principal)
    model = await _model_or_404(db, alias)
    cred = await _credential_or_404(db, body.credential_id)
    (
        final_provider, final_model_id, final_compat,
        final_base_url, final_headers,
    ) = await _resolve_route_identity(
        db,
        model=model, cred=cred,
        provider_slug=body.provider_slug,
        real_model_id=body.real_model_id,
        compat=body.compat,
        base_url=body.base_url,
        dispatch_headers=body.dispatch_headers,
    )
    existing = (
        await db.execute(
            select(GatewayRoute).where(
                GatewayRoute.model_alias == alias,
                GatewayRoute.priority == 0,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.credential_id = body.credential_id
        existing.provider_slug = final_provider
        existing.real_model_id = final_model_id
        existing.compat = final_compat
        existing.base_url = final_base_url
        existing.dispatch_headers = final_headers
        row = existing
    else:
        row = GatewayRoute(
            model_alias=alias,
            credential_id=body.credential_id,
            priority=0,
            provider_slug=final_provider,
            real_model_id=final_model_id,
            compat=final_compat,
            base_url=final_base_url,
            dispatch_headers=final_headers,
        )
        db.add(row)
    await db.commit()
    await db.refresh(row)
    get_cache().set_route(
        row.id,
        alias=row.model_alias,
        credential_id=row.credential_id,
        priority=row.priority,
        provider_slug=row.provider_slug,
        real_model_id=row.real_model_id,
        compat=row.compat,
        base_url=row.base_url,
        dispatch_headers=dict(row.dispatch_headers or {}),
    )
    await publish_invalidation("route", key=str(row.id))
    return (await _hydrate_route(db, row)).model_dump(mode="json")


@router.delete("/admin/routes/{route_id}")
async def delete_route(
    route_id: uuid.UUID = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    r = await db.get(GatewayRoute, route_id)
    if r is None:
        raise HTTPException(404, detail=f"route_not_found: {route_id}")
    alias = r.model_alias
    await db.delete(r)
    await db.commit()
    get_cache().remove_route(route_id, alias=alias)
    await publish_invalidation("route", key=str(route_id))
    return {"deleted": True, "id": str(route_id), "model_alias": alias}
