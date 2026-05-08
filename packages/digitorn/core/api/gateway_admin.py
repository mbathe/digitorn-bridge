"""Admin-only proxy from the daemon to the digitorn LLM gateway.

The web client speaks only to the daemon's origin (same-host). When
the admin dashboard needs gateway-internal state (multi-account
credential health, route health, etc.), it hits these proxy routes.
The daemon forwards each request to the gateway with the same JWT
the user already presented, then mirrors the response back.

Why proxy and not direct gateway calls from the browser:

  * Single origin. Avoids CORS configuration on the gateway.
  * Same auth flow. The gateway accepts the same JWT issued by
    auth.digitorn.ai that the daemon already validated, so no extra
    token plumbing is needed.
  * Pre-flight. The daemon enforces ``_require_admin`` BEFORE any
    network hop, so an unauthorised user gets a fast 403 without
    burning a gateway round-trip.

Endpoints exposed:

    GET /api/admin/gateway/credentials/health
    GET /api/admin/gateway/credentials/{cred_id}/health

The body of each is whatever the gateway returned, unmodified, so
the dashboard can rely on the gateway's contract directly.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Path, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/gateway", tags=["admin", "gateway"])


def _require_admin(request: Request) -> None:
    perms = getattr(request.state, "permissions", []) or []
    if "*" in perms or "admin" in perms:
        return
    raise HTTPException(403, detail="Admin permissions required")


def _gateway_base(request: Request) -> str:
    """Pull the gateway base URL from settings, strip the ``/v1``
    suffix used by the LLM dispatch path so admin paths land at
    ``{host}/admin/...`` not ``{host}/v1/admin/...``.
    """
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        from digitorn.core.config import get_settings
        settings = get_settings()
    base = settings.runtime.gateway_base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def _forward_auth(request: Request) -> dict[str, str]:
    """Forward the incoming Authorization header so the gateway
    validates the same JWT the daemon already accepted."""
    auth = request.headers.get("authorization") or request.headers.get(
        "Authorization",
    )
    return {"Authorization": auth} if auth else {}


async def _gateway_get(
    request: Request, path: str,
) -> dict[str, Any]:
    base = _gateway_base(request)
    url = f"{base}{path}"
    headers = _forward_auth(request)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
    except httpx.RequestError as exc:
        logger.warning("gateway_admin_proxy_unreachable url=%s err=%s", url, exc)
        raise HTTPException(
            502,
            detail=f"gateway_unreachable: {type(exc).__name__}",
        )
    if resp.status_code == 401:
        raise HTTPException(401, detail="gateway_auth_failed")
    if resp.status_code == 403:
        raise HTTPException(403, detail="gateway_forbidden")
    if resp.status_code >= 400:
        raise HTTPException(
            resp.status_code,
            detail=f"gateway_error: {resp.text[:200]}",
        )
    try:
        return resp.json()
    except ValueError:
        raise HTTPException(502, detail="gateway_returned_non_json")


@router.get("/credentials/health")
async def list_gateway_credentials_health(
    request: Request,
) -> dict[str, Any]:
    """Live multi-account health for every gateway credential.

    Returns the gateway's ``/admin/credentials/health`` payload
    verbatim: ``{count, rows: [{cred_id, label, provider_slug,
    status, inflight, consecutive_429s, is_429_blocked,
    blocked_for_s, total_dispatched}, ...]}``.
    """
    _require_admin(request)
    return await _gateway_get(request, "/admin/credentials/health")


@router.get("/credentials/{cred_id}/health")
async def get_gateway_credential_health(
    request: Request,
    cred_id: str = Path(...),
) -> dict[str, Any]:
    """Single-credential drill-down. Mirrors gateway's
    ``/admin/credentials/{cred_id}/health``."""
    _require_admin(request)
    return await _gateway_get(
        request, f"/admin/credentials/{cred_id}/health",
    )
