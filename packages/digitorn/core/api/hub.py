"""Daemon-side bridge to a remote Digitorn Hub.

This module exposes the user-facing routes that let a daemon's clients
browse and install apps from the configured hub:

    POST /api/hub/login        - authenticate the daemon user against the hub,
                                  cache the JWT in ``hub_sessions``
    POST /api/hub/logout       - drop the cached hub session for this user
    GET  /api/hub/me           - return the cached hub user (if logged in)
    GET  /api/hub/search       - proxy to the hub's hybrid semantic search
    GET  /api/hub/packages/{publisher}/{package_id}  - proxy to hub detail
    POST /api/hub/install      - install ``hub://{publisher}/{package_id}[@v]``

The hub URL is read from ``settings.hub.url``. When unset, every route
returns 503 with a clear message instead of partial behaviour.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field  # noqa: F401  (BaseModel reused below)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn.core.api.packages import (
    _build_install_flow,
    _caller_user_id,
    _resolve_deploy_callback,
)
from digitorn.core.config import get_settings
from digitorn.core.database import get_session
from digitorn.core.models import HubSession
from digitorn.core.packages import SourceType
from digitorn.core.packages.install import (
    InstallError,
    PackageIdCollision,
    PermissionsRequired,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hub", tags=["hub"])


# ---------- helpers ----------


def _hub_url() -> str:
    url = (get_settings().hub.url or "").rstrip("/")
    if not url:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "hub_not_configured",
                "message": (
                    "The daemon is not configured with a hub URL. Set "
                    "DIGITORN_HUB__URL or hub.url in ~/.digitorn/config.yaml."
                ),
            },
        )
    return url


def _hub_client(token: str | None = None) -> httpx.AsyncClient:
    cfg = get_settings().hub
    headers = {"User-Agent": "digitorn-daemon"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(
        base_url=_hub_url(),
        timeout=cfg.timeout_seconds,
        verify=cfg.verify_ssl,
        headers=headers,
        follow_redirects=False,
    )


async def _load_session(
    session: AsyncSession, daemon_user_id: str, hub_url: str
) -> HubSession | None:
    stmt = select(HubSession).where(
        HubSession.daemon_user_id == daemon_user_id,
        HubSession.hub_url == hub_url,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _try_bridge_session(
    request: Request, session: AsyncSession
) -> HubSession | None:
    """When `hub.daemon_bridge.enabled` is True, transparently mint a
    Hub session via the daemon's ed25519 keypair so the user never has
    to log in to the Hub explicitly.

    Returns the persisted [HubSession] row on success, or None when the
    bridge is disabled, the daemon isn't authenticated, or the Hub
    refuses the bridge (caller falls back to the cached email/password
    session)."""
    cfg = get_settings().hub.daemon_bridge
    if not cfg.enabled:
        return None
    daemon_user_id = _caller_user_id(request)
    if not daemon_user_id or daemon_user_id in {"local", "anonymous", "system"}:
        # Bridge requires a real user identity - anonymous / local-dev
        # callers fall through to the no-token path.
        return None

    # Pull the user's email & display_name (needed in the signed payload).
    from digitorn.core.models import User as _User

    user = (
        await session.execute(select(_User).where(_User.id == daemon_user_id))
    ).scalar_one_or_none()
    if user is None or not user.email:
        return None

    from digitorn.core.api.hub_bridge import ensure_hub_session

    return await ensure_hub_session(
        session=session,
        daemon_user_id=daemon_user_id,
        daemon_user_email=user.email,
        daemon_user_display_name=user.display_name or user.email.split("@")[0],
        hub_url=_hub_url(),
        http_client_factory=lambda: _hub_client(None),
    )


async def _current_token(request: Request, session: AsyncSession) -> str | None:
    user_id = _caller_user_id(request)
    if not user_id:
        return None
    rec = await _load_session(session, user_id, _hub_url())
    if rec and rec.access_token:
        if not rec.expires_at or rec.expires_at >= datetime.now(timezone.utc):
            return rec.access_token

    # Cache miss / expired - try auto-bridging before giving up. The
    # caller (review/report/install) treats `None` as "user must sign in
    # explicitly", so a successful bridge here flips the experience to
    # zero-friction.
    bridged = await _try_bridge_session(request, session)
    if bridged and bridged.access_token:
        return bridged.access_token
    return None


# ---------- request bodies ----------


class HubLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=1, max_length=200)


class HubInstallRequest(BaseModel):
    publisher: str = Field(min_length=3, max_length=64)
    package_id: str = Field(min_length=3, max_length=64)
    version: str | None = None
    accept_permissions: bool = False
    scope: str = Field(default="user", pattern="^(user|system)$")


# ---------- auth lifecycle ----------


@router.post("/login")
async def hub_login(
    payload: HubLoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    daemon_user_id = _caller_user_id(request)
    if not daemon_user_id or daemon_user_id == "local":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "you must be logged in to the daemon to use the hub",
        )

    hub_url = _hub_url()
    async with _hub_client() as c:
        r = await c.post(
            "/api/v1/auth/login",
            json={"email": payload.email, "password": payload.password},
        )
        if r.status_code == 401:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid hub credentials")
        if r.status_code >= 400:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"hub login failed: {r.status_code} {r.text[:200]}",
            )
        token_pair = r.json()

        me = await c.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token_pair['access_token']}"},
        )
        me.raise_for_status()
        hub_user = me.json()

    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(token_pair.get("expires_in", 3600))
    )

    rec = await _load_session(session, daemon_user_id, hub_url)
    if rec is None:
        rec = HubSession(
            daemon_user_id=daemon_user_id,
            hub_url=hub_url,
            hub_user_email=hub_user["email"],
            hub_user_id=str(hub_user["id"]),
            access_token=token_pair["access_token"],
            refresh_token=token_pair.get("refresh_token", ""),
            expires_at=expires_at,
        )
        session.add(rec)
    else:
        rec.hub_user_email = hub_user["email"]
        rec.hub_user_id = str(hub_user["id"])
        rec.access_token = token_pair["access_token"]
        rec.refresh_token = token_pair.get("refresh_token", "")
        rec.expires_at = expires_at
    await session.commit()

    return {
        "logged_in": True,
        "hub_url": hub_url,
        "hub_user": {"email": hub_user["email"], "id": str(hub_user["id"])},
        "expires_at": expires_at.isoformat(),
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def hub_logout(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> None:
    daemon_user_id = _caller_user_id(request)
    if not daemon_user_id:
        return
    await session.execute(
        delete(HubSession).where(
            HubSession.daemon_user_id == daemon_user_id,
            HubSession.hub_url == _hub_url(),
        )
    )
    await session.commit()


@router.get("/me")
async def hub_me(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    daemon_user_id = _caller_user_id(request)
    rec = await _load_session(session, daemon_user_id or "", _hub_url())

    # Cache miss → opportunistic bridge so the UI can show the
    # "Connected to Hub" state on first paint, no login click needed.
    if rec is None or not rec.access_token or (
        rec.expires_at and rec.expires_at < datetime.now(timezone.utc)
    ):
        bridged = await _try_bridge_session(request, session)
        if bridged is not None:
            rec = bridged

    if not rec or not rec.access_token:
        return {
            "logged_in": False,
            "hub_url": _hub_url(),
            "bridge_enabled": get_settings().hub.daemon_bridge.enabled,
        }
    return {
        "logged_in": True,
        "hub_url": rec.hub_url,
        "hub_user": {"email": rec.hub_user_email, "id": rec.hub_user_id},
        "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
        "bridge_enabled": get_settings().hub.daemon_bridge.enabled,
    }


# ---------- catalog (proxied) ----------


@router.get("/search")
async def hub_search(
    request: Request,
    q: str = Query(default="", max_length=200),
    category: str | None = Query(default=None),
    tag: list[str] | None = Query(default=None),
    risk_level: str | None = Query(default=None, pattern="^(low|medium|high)$"),
    publisher: str | None = Query(default=None),
    include_unverified: bool = Query(
        default=False,
        description="Include packages from non-verified publishers (default: false)",
    ),
    page: int = Query(default=1, ge=1, le=1000),
    page_size: int = Query(default=20, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    token = await _current_token(request, session)
    params: dict[str, Any] = {
        "q": q, "page": page, "page_size": page_size,
        "include_unverified": str(include_unverified).lower(),
    }
    if category: params["category"] = category
    if risk_level: params["risk_level"] = risk_level
    if publisher: params["publisher"] = publisher
    if tag:
        params["tag"] = tag
    async with _hub_client(token) as c:
        r = await c.get("/api/v1/search", params=params)
        if r.status_code >= 400:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, r.text[:300])
        return r.json()


@router.get("/packages/{publisher}/{package_id}")
async def hub_package_detail(
    publisher: str,
    package_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    token = await _current_token(request, session)
    async with _hub_client(token) as c:
        r = await c.get(f"/api/v1/packages/{publisher}/{package_id}")
        if r.status_code == 404:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "package not found on hub")
        if r.status_code >= 400:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, r.text[:300])
        return r.json()


# ---------- install ----------


# ---------- reviews / reports / stats (proxied) ----------


@router.get("/packages/{publisher}/{package_id}/reviews")
async def hub_get_reviews(
    publisher: str,
    package_id: str,
    request: Request,
    sort: str = Query(default="recent", pattern="^(recent|rating_desc|rating_asc)$"),
    page: int = Query(default=1, ge=1, le=1000),
    page_size: int = Query(default=20, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    token = await _current_token(request, session)
    async with _hub_client(token) as c:
        r = await c.get(
            f"/api/v1/packages/{publisher}/{package_id}/reviews",
            params={"sort": sort, "page": page, "page_size": page_size},
        )
        if r.status_code >= 400:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, r.text[:300])
        return r.json()


class HubReviewIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    body: str | None = Field(default=None, max_length=4000)


@router.post("/packages/{publisher}/{package_id}/reviews")
async def hub_post_review(
    publisher: str,
    package_id: str,
    payload: HubReviewIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    token = await _current_token(request, session)
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "log in to the hub first (POST /api/hub/login)"
        )
    async with _hub_client(token) as c:
        r = await c.post(
            f"/api/v1/packages/{publisher}/{package_id}/reviews",
            json=payload.model_dump(),
        )
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text[:300])
            except Exception:
                detail = r.text[:300]
            raise HTTPException(r.status_code, detail)
        return r.json()


class HubReportIn(BaseModel):
    reason: str = Field(min_length=1, max_length=40, pattern=r"^(malware|spam|abuse|copyright|broken|other)$")
    details: str | None = Field(default=None, max_length=4000)


@router.post("/packages/{publisher}/{package_id}/reports")
async def hub_post_report(
    publisher: str,
    package_id: str,
    payload: HubReportIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    token = await _current_token(request, session)
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "log in to the hub first (POST /api/hub/login)"
        )
    async with _hub_client(token) as c:
        r = await c.post(
            f"/api/v1/packages/{publisher}/{package_id}/reports",
            json=payload.model_dump(),
        )
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text[:300])
            except Exception:
                detail = r.text[:300]
            raise HTTPException(r.status_code, detail)
        return r.json()


@router.get("/packages/{publisher}/{package_id}/stats")
async def hub_package_stats(
    publisher: str,
    package_id: str,
    request: Request,
    range_days: int = Query(default=30, alias="range", ge=1, le=365),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    token = await _current_token(request, session)
    async with _hub_client(token) as c:
        r = await c.get(
            f"/api/v1/packages/{publisher}/{package_id}/stats",
            params={"range": range_days},
        )
        if r.status_code >= 400:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, r.text[:300])
        return r.json()


# ---------- install (last endpoint) ----------


@router.post("/install")
async def hub_install(
    payload: HubInstallRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    daemon_user_id = _caller_user_id(request)
    if not daemon_user_id or daemon_user_id == "local":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "you must be logged in to the daemon to install hub apps",
        )

    token = await _current_token(request, session)
    if token:
        request.state.hub_token = token

    if payload.scope == "system":
        perms = getattr(request.state, "permissions", []) or []
        if not ("*" in perms or "admin" in perms or "packages.admin" in perms):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "system-scope install requires admin"
            )

    suffix = f"@{payload.version}" if payload.version else ""
    source_uri = f"hub://{payload.publisher}/{payload.package_id}{suffix}"

    flow = _build_install_flow(request)
    deploy_cb = _resolve_deploy_callback(request)

    owner = daemon_user_id if payload.scope == "user" else None
    install_kwargs = dict(
        source_type=SourceType.HUB,
        source_uri=source_uri,
        installed_by=daemon_user_id,
        accept_permissions=payload.accept_permissions,
        on_deploy=deploy_cb,
        scope=payload.scope,
        owner_user_id=owner,
    )

    try:
        result = await flow.install(**install_kwargs)
    except PermissionsRequired as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "permissions_required",
                "package_id": exc.manifest_id,
                "permissions": exc.perms,
            },
        )
    except PackageIdCollision:
        # Already installed at this scope. Treat the second click on
        # "Install" as an upgrade - same UX the user expects from the
        # web/Flutter clients. The InstallFlow.upgrade() pipeline
        # rolls back automatically on deploy failure (locked D8).
        try:
            result = await flow.upgrade(
                payload.package_id,
                source_type=SourceType.HUB,
                source_uri=source_uri,
                installed_by=daemon_user_id,
                accept_permissions=payload.accept_permissions,
                on_deploy=deploy_cb,
                scope=payload.scope,
                owner_user_id=owner,
            )
        except PermissionsRequired as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "permissions_required",
                    "package_id": exc.manifest_id,
                    "permissions": exc.perms,
                },
            )
        except InstallError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except InstallError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    return {
        "installed": True,
        "package_id": result.package_id,
        "version": result.version,
        "scope": payload.scope,
        "deployed": getattr(result, "deployed", False),
    }
