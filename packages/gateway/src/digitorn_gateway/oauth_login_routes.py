"""HTTP routes for the OAuth login flows.

Endpoints (all require an authenticated principal; admin role gate
applies the same way as the rest of /admin/*):

  POST /admin/oauth/copilot/start
       body: {"label": "Default", "target_provider_slug": "github_copilot"}
       -> {state, user_code, verification_uri, expires_in, interval}

  POST /admin/oauth/oauth2/start
       body: {label, provider_slug, device_code_url, token_endpoint,
              client_id, scope}
       -> same shape as copilot/start

  GET  /admin/oauth/status/{state}
       -> {status: pending|connected|expired|error, ...,
           credential_id?: when status=connected}

  POST /admin/oauth/forget/{state}
       -> {forgot: true}

The dashboard's "Login with X" dialog kicks /start, polls /status
every ``interval`` seconds, and when ``status == "connected"`` reads
``credential_id`` and refreshes the credentials list.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn_gateway.auth import GatewayPrincipal, require_principal
from digitorn_gateway.cipher import GatewayCipherError, encrypt_dict
from digitorn_gateway.config_cache import get_cache
from digitorn_gateway.db import session_dependency
from digitorn_gateway.models_db import GatewayCredential, GatewayProvider
from digitorn_gateway.oauth_login import (
    DeviceFlowRecord,
    get_default_store,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_admin(principal: GatewayPrincipal) -> None:
    if not (principal.roles and (
        "admin" in principal.roles or "developer" in principal.roles
    )):
        raise HTTPException(403, detail="admin_role_required")


class CopilotStartIn(BaseModel):
    label: str = Field(default="Default", min_length=1, max_length=128)
    target_provider_slug: str = Field(default="github_copilot")


class OAuth2StartIn(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    provider_slug: str
    device_code_url: str
    token_endpoint: str
    client_id: str
    scope: str = ""


def _make_persist_callback(
    db_factory, principal: GatewayPrincipal,
):
    """Build the on_success callback the device flow store calls when
    the user authorizes. Encrypts the secret_data and writes a row into
    ``gateway_credentials`` + the in-memory cache."""

    async def _persist(flow: DeviceFlowRecord, secret: dict[str, str]) -> str:
        async with db_factory() as db:
            # Sanity: provider must exist.
            provider = await db.get(
                GatewayProvider, flow.target_provider_slug,
            )
            if provider is None:
                raise RuntimeError(
                    f"provider '{flow.target_provider_slug}' not found - "
                    "create it first before running the OAuth flow"
                )
            try:
                encrypted = encrypt_dict(secret)
            except GatewayCipherError as exc:
                raise RuntimeError(f"cipher_error: {exc}") from exc
            row = GatewayCredential(
                provider_slug=flow.target_provider_slug,
                label=flow.target_label,
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
                # Label already exists - rotate the existing row instead
                # so a re-login of the same label refreshes the tokens.
                from sqlalchemy import select
                existing = (
                    await db.execute(
                        select(GatewayCredential).where(
                            GatewayCredential.provider_slug == flow.target_provider_slug,
                            GatewayCredential.label == flow.target_label,
                        )
                    )
                ).scalar_one()
                existing.encrypted_value = encrypted
                existing.cipher_version = 1
                existing.status = "active"
                await db.commit()
                row = existing
            await db.refresh(row)

            # Cache write-through so the very next dispatch sees it.
            get_cache().upsert_credential(
                row.id,
                provider_slug=row.provider_slug,
                label=row.label,
                secret_data=secret,
                status=row.status,
                live_pool=getattr(row, "live_pool", True),
            )
            return str(row.id)

    return _persist


@router.post("/admin/oauth/copilot/start")
async def copilot_start(
    body: CopilotStartIn,
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """Kick off GitHub Copilot's OAuth device flow. Returns the public
    halves the dashboard needs to display (user_code, verification_uri,
    state for polling)."""
    _require_admin(principal)
    store = get_default_store()
    try:
        flow = await store.start_copilot(
            target_provider_slug=body.target_provider_slug,
            target_label=body.label,
        )
    except RuntimeError as exc:
        raise HTTPException(502, detail=str(exc)) from exc
    return flow.public_dict()


@router.post("/admin/oauth/oauth2/start")
async def oauth2_start(
    body: OAuth2StartIn,
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """Generic RFC 8628 device flow. Caller supplies the upstream
    endpoints + client_id; the gateway runs the dance against them."""
    _require_admin(principal)
    store = get_default_store()
    try:
        flow = await store.start_oauth2(
            device_code_url=body.device_code_url,
            token_endpoint=body.token_endpoint,
            client_id=body.client_id,
            scope=body.scope,
            target_provider_slug=body.provider_slug,
            target_label=body.label,
        )
    except RuntimeError as exc:
        raise HTTPException(502, detail=str(exc)) from exc
    return flow.public_dict()


@router.get("/admin/oauth/status/{state}")
async def oauth_status(
    state: str = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """Poll the upstream for this flow. The gateway hits the upstream
    once per call; callers should respect the ``interval`` field
    returned by /start (5s for Copilot)."""
    _require_admin(principal)
    store = get_default_store()
    flow = await store.get(state)
    if flow is None:
        raise HTTPException(404, detail=f"flow_not_found: {state}")

    if flow.status == "pending":
        from digitorn_gateway.db import get_session_factory
        factory = get_session_factory()
        await store.poll(
            state, on_success=_make_persist_callback(factory, principal),
        )
        flow = await store.get(state)
        assert flow is not None
    return flow.public_dict()


@router.post("/admin/oauth/forget/{state}")
async def oauth_forget(
    state: str = Path(...),
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    _require_admin(principal)
    await get_default_store().forget(state)
    return {"forgot": True, "state": state}
