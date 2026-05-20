"""Admin-only proxy from the daemon to the digitorn LLM gateway."""
from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Path, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/gateway", tags=["admin", "gateway"])


_FORWARDED_REQUEST_HEADERS = {"authorization", "content-type"}
_HOP_BY_HOP_RESPONSE_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-encoding",
    "content-length",  # httpx already decoded; let Starlette set it
}


def _require_admin(request: Request) -> None:
    """Gate gateway-admin proxy calls."""
    perms = getattr(request.state, "permissions", []) or []
    roles = getattr(request.state, "roles", []) or []
    if "*" in perms or "admin" in perms:
        return
    if "admin" in roles or "developer" in roles:
        return
    raise HTTPException(403, detail="admin_or_developer_role_required")


def _gateway_base(request: Request) -> str:
    """Strip the `/v1` suffix from `runtime.gateway_base_url` so"""
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        from digitorn.core.config import get_settings
        settings = get_settings()
    base = settings.runtime.gateway_base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def _forwarded_request_headers(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in _FORWARDED_REQUEST_HEADERS:
            out[k] = v
    return out


def _filtered_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        k: v for k, v in headers.items()
        if k.lower() not in _HOP_BY_HOP_RESPONSE_HEADERS
    }


@router.api_route(
    "/{rest:path}",
    methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    include_in_schema=False,
)
async def proxy(rest: str, request: Request) -> Response:
    """Catch-all proxy."""
    _require_admin(request)
    base = _gateway_base(request)
    target = f"{base}/admin/{rest}"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    body = await request.body()
    headers = _forwarded_request_headers(request)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(
                request.method,
                target,
                headers=headers,
                content=body if body else None,
            )
    except httpx.RequestError as exc:
        logger.warning(
            "gateway_admin_proxy_unreachable method=%s url=%s err=%s",
            request.method, target, exc,
        )
        raise HTTPException(
            502, detail=f"gateway_unreachable: {type(exc).__name__}",
        )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=_filtered_response_headers(resp.headers),
        media_type=resp.headers.get("content-type"),
    )
