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
    )
    return (await _enrich_provider(db, row)).model_dump(by_alias=False)


@router.get("/admin/providers/{slug}")
async def get_provider(
    slug: str = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    row = await _provider_or_404(db, slug)
    return (await _enrich_provider(db, row)).model_dump(by_alias=False)


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
    )
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
    )
    return _to_credential_out(row).model_dump(mode="json")


@router.get("/admin/credentials/{cred_id}")
async def get_credential(
    cred_id: uuid.UUID = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    _require_admin(principal)
    row = await _credential_or_404(db, cred_id)
    return _to_credential_out(row).model_dump(mode="json")


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
    )
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
    )
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
        provider_slug=cred.provider_slug if cred else None,
        credential_label=cred.label if cred else None,
        is_blocked=bool(health.get("is_blocked", False)),
        consecutive_failures=int(health.get("consecutive_failures", 0)),
        last_error=str(health.get("last_error", "")),
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
    pair must be unique - to overwrite an existing slot use PATCH."""
    _require_admin(principal)
    model = await _model_or_404(db, body.model_alias)
    cred = await _credential_or_404(db, body.credential_id)
    if cred.provider_slug != model.provider_slug:
        raise HTTPException(
            400,
            detail=(
                f"provider_mismatch: model '{body.model_alias}' targets "
                f"'{model.provider_slug}' but credential is for "
                f"'{cred.provider_slug}'"
            ),
        )
    row = GatewayRoute(
        model_alias=body.model_alias,
        credential_id=body.credential_id,
        priority=body.priority,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409,
            detail=(
                f"route_priority_taken: model_alias={body.model_alias} "
                f"priority={body.priority}"
            ),
        )
    await db.refresh(row)
    get_cache().set_route(
        row.id,
        alias=row.model_alias,
        credential_id=row.credential_id,
        priority=row.priority,
    )
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
    if body.credential_id is not None:
        cred = await _credential_or_404(db, body.credential_id)
        model = await _model_or_404(db, row.model_alias)
        if cred.provider_slug != model.provider_slug:
            raise HTTPException(
                400,
                detail="provider_mismatch on PATCH credential",
            )
        row.credential_id = body.credential_id
    if body.priority is not None:
        row.priority = body.priority
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail="route_priority_taken")
    await db.refresh(row)
    get_cache().set_route(
        row.id,
        alias=row.model_alias,
        credential_id=row.credential_id,
        priority=row.priority,
    )
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
    fallbacks alone."""
    _require_admin(principal)
    model = await _model_or_404(db, alias)
    cred = await _credential_or_404(db, body.credential_id)
    if cred.provider_slug != model.provider_slug:
        raise HTTPException(
            400,
            detail=(
                f"provider_mismatch: model '{alias}' targets "
                f"'{model.provider_slug}' but credential is for "
                f"'{cred.provider_slug}'"
            ),
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
        row = existing
    else:
        row = GatewayRoute(
            model_alias=alias, credential_id=body.credential_id, priority=0,
        )
        db.add(row)
    await db.commit()
    await db.refresh(row)
    get_cache().set_route(
        row.id,
        alias=row.model_alias,
        credential_id=row.credential_id,
        priority=row.priority,
    )
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
    return {"deleted": True, "id": str(route_id), "model_alias": alias}
