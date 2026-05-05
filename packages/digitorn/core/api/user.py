"""User-level routes: global event stream, inbox, approvals, devices.

This module carries everything that is **cross-session / cross-app**
for the authenticated user:

- ``GET  /api/users/me/events``         global SSE stream (fan-in)
- ``GET  /api/users/me/inbox``          persisted notification inbox
- ``GET  /api/users/me/inbox/unread_count``
- ``POST /api/users/me/inbox/{id}/read``
- ``POST /api/users/me/inbox/read_all``
- ``DELETE /api/users/me/inbox/{id}``   archive
- ``GET  /api/users/me/approvals``      cross-app pending approvals
- ``POST /api/users/me/devices``        push-notification device registration (stub)
- ``DELETE /api/users/me/devices/{id}`` unregister device (stub)
- ``GET  /api/users/me/notification-prefs``  server-side prefs (stub)
- ``PUT  /api/users/me/notification-prefs``  server-side prefs (stub)

The apps router (``/api/apps``) stays focused on per-app operations;
anything that spans apps lives here.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from digitorn.core.api.apps_v2 import AppResponse, _get_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users/me", tags=["user"])


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _user_id(request: Request) -> str:
    return getattr(request.state, "user_id", None) or "local"


def _get_inbox_store(request: Request):
    store = getattr(request.app.state, "inbox_store", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="Inbox store not initialized",
        )
    return store


def _require_admin(request: Request) -> None:
    perms = getattr(request.state, "permissions", []) or []
    if "*" in perms or "admin" in perms:
        return
    raise HTTPException(
        status_code=403, detail="Admin permissions required",
    )


# ════════════════════════════════════════════════════════════════════
# A. Global user SSE stream
# ════════════════════════════════════════════════════════════════════


# NOTE: The global user SSE stream (GET /api/users/me/events) has
# been removed. Clients now receive every user-scoped event via
# Socket.IO on the `/events` namespace - connecting auto-joins the
# `user:{user_id}` room, and the `connected` handshake carries the
# current `latest_seq` for replay via the `replay` Socket.IO event.
# See core/events/socketio_bus.py.


# ════════════════════════════════════════════════════════════════════
# B. Persistent inbox
# ════════════════════════════════════════════════════════════════════


@router.get("/inbox", response_model=AppResponse)
async def list_inbox(
    request: Request,
    limit: int = 100,
    since: str = "",
    include_archived: bool = False,
) -> AppResponse:
    """Return the caller's inbox items, newest first.

    Pagination via ``since=<item_id>`` (cursor). Default page size
    100, max 500. Each item is enriched with the app's visual
    metadata (``app_name``, ``app_icon``, ``app_color``) so the
    client renders without an extra /api/apps/{id} fetch per row.
    """
    store = _get_inbox_store(request)
    user_id = _user_id(request)
    items = await store.list_for_user(
        user_id=user_id,
        limit=min(limit, 500),
        since_id=since or None,
        include_archived=include_archived,
    )

    # Join the deployed app's metadata in-process. Cheap because
    # the deployed dict is an in-memory map.
    try:
        manager = _get_manager(request)
        app_meta_cache: dict[str, dict[str, Any]] = {}
        for item in items:
            app_id = item.get("app_id")
            if not app_id:
                continue
            if app_id not in app_meta_cache:
                deployed = manager.get(app_id)
                if deployed is not None:
                    m = deployed.compiled.meta
                    app_meta_cache[app_id] = {
                        "app_name": getattr(m, "name", app_id),
                        "app_icon": getattr(m, "icon", "") or "",
                        "app_color": getattr(m, "color", "") or "",
                    }
                else:
                    app_meta_cache[app_id] = {
                        "app_name": app_id,
                        "app_icon": "",
                        "app_color": "",
                    }
            item.update(app_meta_cache[app_id])
    except Exception as exc:
        logger.debug("inbox app_meta join failed: %s", exc)

    return AppResponse(
        success=True,
        data={
            "items": items,
            "count": len(items),
            "next_cursor": items[-1]["id"] if items and len(items) >= limit else None,
        },
    )


@router.get("/inbox/unread_count", response_model=AppResponse)
async def inbox_unread_count(request: Request) -> AppResponse:
    """Return the unread item count. Used by the bell badge."""
    store = _get_inbox_store(request)
    user_id = _user_id(request)
    count = await store.count_unread(user_id=user_id)
    return AppResponse(success=True, data={"unread_count": count})


@router.post("/inbox/{item_id}/read", response_model=AppResponse)
async def inbox_mark_read(
    request: Request, item_id: str,
) -> AppResponse:
    store = _get_inbox_store(request)
    user_id = _user_id(request)
    ok = await store.mark_read(user_id=user_id, item_id=item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    return AppResponse(success=True, data={"item_id": item_id, "read": True})


@router.post("/inbox/read_all", response_model=AppResponse)
async def inbox_mark_all_read(request: Request) -> AppResponse:
    store = _get_inbox_store(request)
    user_id = _user_id(request)
    count = await store.mark_all_read(user_id=user_id)
    return AppResponse(success=True, data={"marked_read": count})


@router.delete("/inbox/{item_id}", response_model=AppResponse)
async def inbox_archive(
    request: Request, item_id: str,
) -> AppResponse:
    store = _get_inbox_store(request)
    user_id = _user_id(request)
    ok = await store.archive(user_id=user_id, item_id=item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    return AppResponse(success=True, data={"item_id": item_id, "archived": True})


# ════════════════════════════════════════════════════════════════════
# C. Cross-app approvals
# ════════════════════════════════════════════════════════════════════


@router.get("/sessions", response_model=AppResponse)
async def list_user_sessions_cross_app(
    request: Request, limit: int = 50, offset: int = 0,
) -> AppResponse:
    """Return the caller's recent sessions **across every app** sorted
    by ``last_active`` descending.

    This is the backing route for the "Recent conversations" view in
    the Flutter client - the one that aggregates work in progress
    independently of which app is currently open. Each row carries
    the app's visual metadata (icon, color, name) so the list
    renders without an extra /api/apps/{id} fetch per row.
    """
    manager = _get_manager(request)
    user_id = _user_id(request)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    merged: list[dict[str, Any]] = []
    try:
        deployed_ids = list(getattr(manager, "_deployed", {}).keys())
    except Exception:
        deployed_ids = []

    for app_id in deployed_ids:
        try:
            rows = await manager.list_sessions(
                app_id, user_id=user_id, limit=200,
            )
            for r in rows:
                r["is_active"] = manager.is_session_active(
                    app_id, r.get("session_id", ""),
                )
                merged.append(r)
        except Exception as exc:
            logger.debug(
                "list_user_sessions_cross_app: %s failed: %s",
                app_id, exc,
            )

    # Sort by last_active desc
    merged.sort(
        key=lambda s: s.get("last_active") or s.get("created_at") or 0,
        reverse=True,
    )
    total = len(merged)
    page = merged[offset:offset + limit]

    return AppResponse(
        success=True,
        data={
            "sessions": page,
            "total": total,
            "limit": limit,
            "offset": offset,
        },
    )


@router.get("/approvals", response_model=AppResponse)
async def list_user_approvals(request: Request) -> AppResponse:
    """Return every pending approval request belonging to the caller,
    across all deployed apps.

    Powers the "pending approvals" section of the inbox and lets
    the Flutter client resync after a reload without iterating
    ``/api/apps/{id}/approvals`` per app.
    """
    manager = _get_manager(request)
    user_id = _user_id(request)

    pending: list[dict[str, Any]] = []
    try:
        deployed_ids = list(getattr(manager, "_deployed", {}).keys())
    except Exception:
        deployed_ids = []

    for app_id in deployed_ids:
        deployed = manager.get(app_id)
        if deployed is None:
            continue
        queue = getattr(deployed, "approval_queue", None)
        if queue is None:
            continue
        try:
            # Prefer list_pending_for_user so the filter runs inside
            # the queue (avoids leaking other users' metadata through
            # to_dict even briefly).
            if hasattr(queue, "list_pending_for_user"):
                items = queue.list_pending_for_user(user_id)
            else:
                raw = queue.list_pending() if hasattr(queue, "list_pending") else []
                items = [
                    r for r in raw
                    if not r.get("user_id") or r.get("user_id") == user_id
                ]
        except Exception as exc:
            logger.debug("approval_queue list failed for %s: %s", app_id, exc)
            continue
        for item in items:
            as_dict = dict(item)
            as_dict["app_id"] = app_id
            pending.append(as_dict)

    pending.sort(
        key=lambda x: x.get("created_at") or x.get("ts") or "",
        reverse=True,
    )
    return AppResponse(
        success=True,
        data={"approvals": pending, "count": len(pending)},
    )


# ════════════════════════════════════════════════════════════════════
# D. Devices (push notifications - stub)
# ════════════════════════════════════════════════════════════════════
#
# Real FCM/APNS delivery is P2 - these routes persist registrations
# but don't actually push anything yet. The Flutter client can wire
# register/unregister now so we don't block the push integration on
# a separate PR.


class DeviceRegisterRequest(BaseModel):
    platform: str = Field(..., description="ios | android | web")
    fcm_token: str
    device_name: str = ""
    app_version: str = ""


@router.post("/devices", response_model=AppResponse)
async def register_device(
    request: Request, body: DeviceRegisterRequest,
) -> AppResponse:
    """Register a device for future push-notification delivery.

    **Stub**: the row is persisted so we keep the token, but the
    daemon doesn't actually push anything yet. Return shape is
    stable so the client can wire `unregister` without changes.
    """
    store = _get_inbox_store(request)
    user_id = _user_id(request)
    device = await store.register_device(
        user_id=user_id,
        platform=body.platform,
        fcm_token=body.fcm_token,
        device_name=body.device_name,
        app_version=body.app_version,
    )
    return AppResponse(success=True, data=device)


@router.delete("/devices/{device_id}", response_model=AppResponse)
async def unregister_device(
    request: Request, device_id: str,
) -> AppResponse:
    store = _get_inbox_store(request)
    user_id = _user_id(request)
    ok = await store.unregister_device(
        user_id=user_id, device_id=device_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Device not found")
    return AppResponse(success=True, data={"device_id": device_id, "removed": True})


@router.get("/devices", response_model=AppResponse)
async def list_devices(request: Request) -> AppResponse:
    store = _get_inbox_store(request)
    user_id = _user_id(request)
    devices = await store.list_devices(user_id=user_id)
    return AppResponse(
        success=True, data={"devices": devices, "count": len(devices)},
    )


# ════════════════════════════════════════════════════════════════════
# E. Notification preferences (stub - server-side mirror of local prefs)
# ════════════════════════════════════════════════════════════════════


class NotificationPrefs(BaseModel):
    events: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Map of event kind → list of delivery channels. Example: "
            "`{'session.failed': ['desktop', 'push', 'email']}`."
        ),
    )
    quiet_hours: dict[str, Any] = Field(default_factory=dict)
    channels: dict[str, str] = Field(default_factory=dict)


@router.get("/notification-prefs", response_model=AppResponse)
async def get_notification_prefs(request: Request) -> AppResponse:
    store = _get_inbox_store(request)
    user_id = _user_id(request)
    prefs = await store.get_notification_prefs(user_id=user_id)
    return AppResponse(success=True, data=prefs or {
        "events": {},
        "quiet_hours": {},
        "channels": {},
    })


@router.put("/notification-prefs", response_model=AppResponse)
async def put_notification_prefs(
    request: Request, body: NotificationPrefs,
) -> AppResponse:
    store = _get_inbox_store(request)
    user_id = _user_id(request)
    saved = await store.save_notification_prefs(
        user_id=user_id,
        prefs=body.model_dump(),
    )
    return AppResponse(success=True, data=saved)



# ════════════════════════════════════════════════════════════════════
# Admin user management was removed when identity moved to the
# central digitorn-auth service. Admin user CRUD - list, search,
# inspect, update, soft/hard-delete - now lives on the auth service
# (or its dashboard). The role-catalog and audit-log endpoints below
# stay daemon-side because roles + audit are daemon-scoped concerns.
#
# Quota and usage routes were removed when quota enforcement moved
# to the digitorn LLM gateway (`packages/gateway/`). The gateway
# exposes /v1/quota/me and /admin/quota/* with the same contract.
# ════════════════════════════════════════════════════════════════════


admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


# ── Roles catalog (read-only for now) ───────────────────────────────


@admin_router.get("/audit-log", response_model=AppResponse)
async def admin_list_audit_log(
    request: Request,
    event_type: str = "",           # filter: exact type (e.g. "quota.set_app") OR "quota.*"
    actor_user_id: str = "",
    target_user_id: str = "",
    target_app_id: str = "",
    since_ts: str = "",              # ISO8601 - return rows with ts >= since_ts
    until_ts: str = "",              # ISO8601 - return rows with ts <  until_ts
    success_only: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> AppResponse:
    """Read the append-only audit trail.

    All filters compose (AND). ``event_type`` supports trailing
    wildcard: ``quota.*`` returns every quota.* row. Timestamps are
    ISO8601; pass either UTC (``2026-04-21T00:00:00Z``) or tz-aware
    offsets. Paginated via limit+offset.

    Admin-only. Needs ``*`` or ``admin`` perm. The trail itself is
    immutable - there is deliberately no PATCH / DELETE route.
    """
    _require_admin(request)
    from sqlalchemy import and_, select, func
    # Unified ledger: query history_log WHERE kind='audit'.
    from digitorn.core.models import HistoryLog
    from digitorn.core.database import get_session_factory
    from datetime import datetime as _dt

    factory = get_session_factory()
    async with factory() as session:
        stmt = select(HistoryLog).where(HistoryLog.kind == "audit")
        conds = []
        if event_type:
            if event_type.endswith("*"):
                conds.append(HistoryLog.type.like(event_type[:-1] + "%"))
            else:
                conds.append(HistoryLog.type == event_type)
        if actor_user_id:
            conds.append(HistoryLog.actor_user_id == actor_user_id)
        if target_user_id:
            conds.append(HistoryLog.target_user_id == target_user_id)
        if target_app_id:
            conds.append(HistoryLog.target_app_id == target_app_id)
        if since_ts:
            try:
                conds.append(
                    HistoryLog.ts >= _dt.fromisoformat(since_ts.replace("Z", "+00:00"))
                )
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid since_ts - use ISO8601, got {since_ts!r}",
                )
        if until_ts:
            try:
                conds.append(
                    HistoryLog.ts < _dt.fromisoformat(until_ts.replace("Z", "+00:00"))
                )
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid until_ts - use ISO8601, got {until_ts!r}",
                )
        if success_only:
            conds.append(HistoryLog.success.is_(True))
        if conds:
            stmt = stmt.where(and_(*conds))

        total = (
            await session.execute(
                select(func.count()).select_from(stmt.subquery())
            )
        ).scalar() or 0

        stmt = stmt.order_by(HistoryLog.ts.desc()).limit(limit).offset(offset)
        rows = (await session.execute(stmt)).scalars().all()

        return AppResponse(success=True, data={
            "entries": [
                {
                    "id": r.id,
                    "ts": r.ts.isoformat() if r.ts else None,
                    # Keep the legacy field name for client compat.
                    "event_type": r.type,
                    "actor_user_id": r.actor_user_id,
                    "actor_roles": r.actor_roles or [],
                    "target_user_id": r.target_user_id,
                    "target_app_id": r.target_app_id,
                    "target_resource": r.target_resource,
                    "ip_address": r.ip_address,
                    "user_agent": r.user_agent,
                    "before": r.before or {},
                    "after": r.after or {},
                    "success": r.success,
                    "message": r.message,
                }
                for r in rows
            ],
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "has_more": (offset + len(rows)) < int(total),
        })


@admin_router.get("/roles", response_model=AppResponse)
async def admin_list_roles(request: Request) -> AppResponse:
    """List all roles defined in the daemon. Used by the admin UI to
    populate the role-picker when editing a user.
    """
    _require_admin(request)
    from sqlalchemy import select
    from digitorn.core.models import Role
    from digitorn.core.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        rows = (await session.execute(
            select(Role).order_by(Role.is_builtin.desc(), Role.name),
        )).scalars().all()
        return AppResponse(success=True, data={
            "roles": [
                {
                    "id": r.id, "name": r.name,
                    "description": r.description,
                    "is_builtin": r.is_builtin,
                    "permissions": r.permissions or [],
                }
                for r in rows
            ],
        })


# ── Daemon-wide counters for the Overview dashboard ─────────────────
# Cached in-process for 30s so the admin panel can hit this on every
# focus / poll without burning DB round-trips. Fail-soft: each counter
# has its own try/except returning 0 - one missing component (e.g. MCP
# pool not initialised) must not break the whole dashboard.

_admin_stats_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_ADMIN_STATS_TTL_S = 30.0


@admin_router.get("/stats", response_model=AppResponse)
async def admin_get_stats(request: Request) -> AppResponse:
    """Daemon-wide counters for the admin Overview dashboard.

    Returns counts scoped to the whole daemon - users, deployed apps,
    installed packages (split by user/system scope), credentials (split
    by owner_type), MCP servers in the connected pool, recently-active
    user sessions, and current-month LLM spend in USD. Admin-only.

    Cached in-process for 30 s to protect the DB from the admin panel's
    focus/poll cadence.
    """
    _require_admin(request)

    import time as _time
    now = _time.monotonic()
    cached = _admin_stats_cache.get("data")
    if cached is not None and (now - _admin_stats_cache.get("ts", 0.0)) < _ADMIN_STATS_TTL_S:
        return AppResponse(success=True, data={"stats": cached})

    from sqlalchemy import select, func
    from datetime import datetime, timezone, timedelta
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import (
        Application, InstalledPackage, Credential, UserSession,
    )

    stats: dict[str, Any] = {
        # User count is owned by the central auth service - the admin
        # dashboard should fetch it from there. Kept here as 0 for
        # legacy clients that read the field unconditionally.
        "users": 0,
        "apps": 0,
        "packages": 0,
        "system_packages": 0,
        "credentials": 0,
        "system_credentials": 0,
        "mcp_servers": 0,
        "active_sessions": 0,
        # Monthly cost is owned by the digitorn LLM gateway -
        # admin dashboard reads it from /admin/quota/users on the
        # gateway. Kept here as 0 for legacy clients.
        "monthly_cost_usd": 0.0,
    }

    async def _count(db: Any, stmt: Any) -> int:
        try:
            return int((await db.execute(stmt)).scalar() or 0)
        except Exception as exc:
            logger.warning("admin_stats_count_failed: %s", exc)
            return 0

    async def _scalar_float(db: Any, stmt: Any) -> float:
        try:
            return float((await db.execute(stmt)).scalar() or 0.0)
        except Exception as exc:
            logger.warning("admin_stats_scalar_failed: %s", exc)
            return 0.0

    factory = get_session_factory()
    if factory is not None:
        async with factory() as db:
            stats["apps"] = await _count(db, select(func.count(Application.id)))
            stats["packages"] = await _count(
                db, select(func.count(InstalledPackage.id))
                .where(InstalledPackage.scope == "user"),
            )
            stats["system_packages"] = await _count(
                db, select(func.count(InstalledPackage.id))
                .where(InstalledPackage.scope == "system"),
            )
            stats["credentials"] = await _count(
                db, select(func.count(Credential.id))
                .where(Credential.owner_type == "user"),
            )
            stats["system_credentials"] = await _count(
                db, select(func.count(Credential.id))
                .where(Credential.owner_type == "system"),
            )
            active_cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
            stats["active_sessions"] = await _count(
                db, select(func.count(UserSession.id))
                .where(UserSession.last_active_at >= active_cutoff),
            )
            # monthly_cost_usd intentionally left at 0.0 - cost is
            # tracked by the gateway, not the daemon.

    # MCP pool lives on app.state and is optional - if the pool never
    # started (no MCP dependency installed / creds missing) we just
    # report 0 without failing the whole dashboard.
    try:
        pool = getattr(request.app.state, "mcp_pool", None)
        if pool is not None and hasattr(pool, "list_connected"):
            connected = pool.list_connected()
            stats["mcp_servers"] = len(connected) if connected is not None else 0
    except Exception as exc:
        logger.warning("admin_stats_mcp_pool_failed: %s", exc)

    _admin_stats_cache["ts"] = now
    _admin_stats_cache["data"] = stats
    return AppResponse(success=True, data={"stats": stats})


# ════════════════════════════════════════════════════════════════════
# G. Profile management (display_name, email, password, avatar)
# ════════════════════════════════════════════════════════════════════


class ProfileUpdateRequest(BaseModel):
    """Self-service profile update body.

    Forwarded as-is to ``PATCH /auth/me`` on the central auth service,
    which owns identity. All fields optional - only set ones get applied.
    Email is locked there (re-verification flow). Reserved attribute
    keys (password_hash, mfa_*, lockout counters) are filtered by the
    auth service.
    """
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    attributes: dict[str, Any] | None = None


def _auth_service_url(request: Request) -> str:
    """Return the central auth-service base URL or raise 503.

    Identity is owned by digitorn-auth (https://auth.digitorn.ai by
    default). The daemon never reads identity from its local DB - it
    proxies to this URL.
    """
    url = getattr(request.app.state, "auth_service_url", None)
    if not url:
        raise HTTPException(
            status_code=503,
            detail="auth.service_url not configured - daemon cannot proxy profile",
        )
    return url.rstrip("/")


def _bearer(request: Request) -> str:
    """Extract the caller's bearer token to forward to the auth service."""
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return h.split(" ", 1)[1].strip()


@router.get("/profile", response_model=AppResponse)
async def get_my_profile(request: Request) -> AppResponse:
    """Return the caller's full profile from the central auth service.

    The daemon does not store identity - this is a thin proxy to
    ``GET /auth/me`` on digitorn-auth so the frontend has a single
    consistent shape regardless of which daemon it's talking to.
    """
    import httpx
    base = _auth_service_url(request)
    token = _bearer(request)
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"{base}/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Auth service unreachable: {exc}",
        )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return AppResponse(success=True, data=r.json())


@router.put("/profile", response_model=AppResponse)
async def update_my_profile(
    request: Request, body: ProfileUpdateRequest,
) -> AppResponse:
    """Update the caller's profile.

    Thin proxy to ``PATCH /auth/me`` on digitorn-auth. Email is locked
    (re-verification flow lives on the central). Reserved attributes
    (password_hash, mfa_*, lockout counters, ...) are filtered server-side
    by the auth service.
    """
    import httpx
    base = _auth_service_url(request)
    token = _bearer(request)
    payload: dict[str, Any] = {}
    if body.display_name is not None:
        payload["display_name"] = body.display_name
    if body.phone is not None:
        payload["phone"] = body.phone
    if body.attributes is not None:
        payload["attributes"] = body.attributes
    if body.email is not None:
        # Email change is not supported via this route - ignored
        # silently rather than 400 so existing clients keep working.
        logger.debug("profile_update_email_ignored caller=%s", _user_id(request))
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.patch(
                f"{base}/auth/me",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Auth service unreachable: {exc}",
        )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return AppResponse(
        success=True,
        data={
            **r.json(),
            # Hint kept for the frontend that uses this key today.
            "changes": r.json().get("changed", []),
        },
    )


@router.post("/avatar", response_model=AppResponse)
async def upload_my_avatar(request: Request) -> AppResponse:
    """Forward a multipart avatar upload to the central auth service.

    The auth service stores the bytes on its own persistent volume and
    owns ``avatar_url``. The URL it returns is relative to the auth
    service host (``/auth/avatars/<file>``) - the frontend prepends
    ``DIGITORN_AUTH_SERVICE_URL`` when rendering.
    """
    import httpx
    base = _auth_service_url(request)
    token = _bearer(request)

    form = await request.form()
    file = form.get("file") or form.get("avatar")
    if file is None or not hasattr(file, "filename"):
        raise HTTPException(status_code=400, detail="No file provided")
    data = await file.read()

    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                f"{base}/auth/me/avatar",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (file.filename, data, file.content_type or "application/octet-stream")},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Auth service unreachable: {exc}",
        )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return AppResponse(success=True, data=r.json())


@router.delete("/avatar", response_model=AppResponse)
async def delete_my_avatar(request: Request) -> AppResponse:
    """Drop the caller's avatar via the central auth service."""
    import httpx
    base = _auth_service_url(request)
    token = _bearer(request)
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.delete(
                f"{base}/auth/me/avatar",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"Auth service unreachable: {exc}",
        )
    if r.status_code not in (200, 204):
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return AppResponse(success=True, data={"deleted": True})


@router.get("/avatar/{filename}")
async def serve_my_avatar(request: Request, filename: str):
    """Redirect avatar requests to the central auth service.

    Old avatars (uploaded before the central-auth migration) lived on
    the daemon's local disk under ~/.digitorn/avatars/. Going forward,
    the canonical URL is ``<auth-service>/auth/avatars/<filename>``;
    we 308 here so legacy clients keep working without a code change.
    """
    from fastapi.responses import RedirectResponse
    from pathlib import Path

    base = _auth_service_url(request)
    safe = Path(filename).name
    return RedirectResponse(url=f"{base}/auth/avatars/{safe}", status_code=308)
