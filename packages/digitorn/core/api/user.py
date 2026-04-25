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

from digitorn.core.api.apps import AppResponse, _get_manager

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


def _get_usage_store(request: Request):
    store = getattr(request.app.state, "usage_store", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="Usage store not initialized",
        )
    return store


def _get_quota_store(request: Request):
    store = getattr(request.app.state, "quota_store", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="Quota store not initialized",
        )
    return store


def _require_admin(request: Request) -> None:
    perms = getattr(request.state, "permissions", []) or []
    if "*" in perms or "admin" in perms or "quota.admin" in perms:
        return
    raise HTTPException(
        status_code=403, detail="Admin permissions required",
    )


# ════════════════════════════════════════════════════════════════════
# A. Global user SSE stream
# ════════════════════════════════════════════════════════════════════


# NOTE: The global user SSE stream (GET /api/users/me/events) has
# been removed. Clients now receive every user-scoped event via
# Socket.IO on the `/events` namespace — connecting auto-joins the
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
    the Flutter client — the one that aggregates work in progress
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
# D. Devices (push notifications — stub)
# ════════════════════════════════════════════════════════════════════
#
# Real FCM/APNS delivery is P2 — these routes persist registrations
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
# E. Notification preferences (stub — server-side mirror of local prefs)
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
# F. Usage & quotas (token monitoring)
# ════════════════════════════════════════════════════════════════════


@router.get("/usage", response_model=AppResponse)
async def get_my_usage(request: Request) -> AppResponse:
    """Complete usage summary for the authenticated user.

    Powers the Settings → Usage screen. Combines monthly totals,
    cost by model, 24h hourly time series, 30d daily time series,
    a per-app breakdown, and the caller's active quota status.

    The response is computed on the fly from ``usage_events`` —
    no caching in v1. SQLite can easily handle the aggregation
    for the scale we target (single-daemon multi-user).
    """
    usage = _get_usage_store(request)
    quotas = _get_quota_store(request)
    user_id = _user_id(request)

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    thirty_days_ago = now - timedelta(days=30)

    # Next month boundary — same calendar trick as the quota window
    if now.month == 12:
        next_month = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)

    monthly = await usage.monthly_totals(user_id=user_id, at=now)
    cost_by_model = await usage.cost_by_model(
        user_id=user_id, since=month_start,
    )
    by_app = await usage.by_app(user_id=user_id, since=thirty_days_ago)
    ts_24h = await usage.timeseries_hourly(user_id=user_id, hours=24, at=now)
    ts_30d = await usage.timeseries_daily(user_id=user_id, days=30, at=now)

    # Active quota for the user (cross-app). If no explicit quota
    # is set, we return None → client shows "unlimited".
    quota_rows = await quotas.list_quotas(
        scope_type="user", scope_id=user_id,
    )
    quota_block: dict[str, Any] | None = None
    if quota_rows:
        # Pick the tightest monthly one first (99% of configs)
        monthly_quota = next(
            (q for q in quota_rows if q["period"] == "month"), None,
        )
        if monthly_quota is not None:
            used = monthly["total_tokens"]
            quota_block = {
                "tokens_per_month": monthly_quota["tokens_limit"],
                "tokens_used_this_month": used,
                "tokens_remaining": max(
                    0, monthly_quota["tokens_limit"] - used,
                ),
                "resets_at": next_month.isoformat(),
                "period": "month",
            }

    return AppResponse(
        success=True,
        data={
            "quota": quota_block,
            "cost": {
                "currency": "USD",
                "this_month": round(float(monthly["cost_usd"]), 6),
                "by_model": cost_by_model,
            },
            "tokens_this_month": {
                "prompt": monthly["prompt_tokens"],
                "completion": monthly["completion_tokens"],
                "total": monthly["total_tokens"],
            },
            "tokens_timeseries_24h": ts_24h,
            "tokens_timeseries_30d": ts_30d,
            "by_app": by_app,
        },
    )


# ── Admin: quotas ──────────────────────────────────────────────────


class QuotaUpsertRequest(BaseModel):
    scope_type: str = Field(..., description="user | user_app | app")
    scope_id: str
    app_id: str | None = None
    period: str = Field(default="month", description="day | week | month")
    tokens_limit: int = Field(..., ge=0)


@router.get("/quotas", response_model=AppResponse)
async def list_my_quotas(request: Request) -> AppResponse:
    """Return every quota applicable to the caller (self-set + admin-set)."""
    quotas = _get_quota_store(request)
    user_id = _user_id(request)
    rows = await quotas.list_quotas(scope_type="user", scope_id=user_id)
    rows += await quotas.list_quotas(scope_type="user_app", scope_id=user_id)
    return AppResponse(
        success=True, data={"quotas": rows, "count": len(rows)},
    )


# Admin-only: one CRUD endpoint set under /api/admin/quotas/*


admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


@admin_router.get("/quotas", response_model=AppResponse)
async def admin_list_quotas(
    request: Request,
    scope_type: str = "",
    scope_id: str = "",
    app_id: str = "",
) -> AppResponse:
    _require_admin(request)
    quotas = _get_quota_store(request)
    rows = await quotas.list_quotas(
        scope_type=scope_type or None,
        scope_id=scope_id or None,
        app_id=app_id or None,
    )
    return AppResponse(
        success=True, data={"quotas": rows, "count": len(rows)},
    )


@admin_router.post("/quotas", response_model=AppResponse)
async def admin_upsert_quota(
    request: Request, body: QuotaUpsertRequest,
) -> AppResponse:
    _require_admin(request)
    quotas = _get_quota_store(request)
    try:
        row = await quotas.upsert_quota(
            scope_type=body.scope_type,
            scope_id=body.scope_id,
            app_id=body.app_id,
            period=body.period,
            tokens_limit=body.tokens_limit,
            set_by=_user_id(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return AppResponse(success=True, data=row)


@admin_router.delete("/quotas/{quota_id}", response_model=AppResponse)
async def admin_delete_quota(
    request: Request, quota_id: str,
) -> AppResponse:
    _require_admin(request)
    quotas = _get_quota_store(request)
    ok = await quotas.delete_quota(quota_id=quota_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Quota not found")
    return AppResponse(success=True, data={"deleted": True})


# ════════════════════════════════════════════════════════════════════
# G. Profile management (display_name, email, password, avatar)
# ════════════════════════════════════════════════════════════════════


class ProfileUpdateRequest(BaseModel):
    display_name: str | None = None
    email: str | None = None


class PasswordChangeRequest(BaseModel):
    current: str
    new: str = Field(..., min_length=8)


@router.get("/profile", response_model=AppResponse)
async def get_my_profile(request: Request) -> AppResponse:
    """Return the caller's full profile row, including avatar_url,
    created_at, last_seen_at — the extra fields the Flutter settings
    screen needs beyond the basic /auth/me response."""
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import User
    from sqlalchemy import select
    user_id = _user_id(request)
    async with get_session_factory()() as db:
        row = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
    if row is None:
        # Dev mode / anonymous — return a stub profile
        return AppResponse(
            success=True,
            data={
                "id": user_id,
                "display_name": user_id,
                "email": None,
                "avatar_url": None,
                "created_at": None,
                "last_seen_at": None,
            },
        )
    return AppResponse(
        success=True,
        data={
            "id": row.id,
            "display_name": row.display_name,
            "email": row.email,
            "avatar_url": row.avatar_url,
            "phone": row.phone,
            "is_active": row.is_active,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_seen_at": (
                row.last_seen_at.isoformat() if row.last_seen_at else None
            ),
            "attributes": dict(row.attributes or {}),
        },
    )


@router.put("/profile", response_model=AppResponse)
async def update_my_profile(
    request: Request, body: ProfileUpdateRequest,
) -> AppResponse:
    """Update display_name and/or email on the caller's User row."""
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import User
    from sqlalchemy import select
    from datetime import datetime, timezone

    user_id = _user_id(request)
    async with get_session_factory()() as db:
        row = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=404, detail="User record not found",
            )
        if body.display_name is not None:
            row.display_name = body.display_name[:512] or None
        if body.email is not None:
            row.email = body.email[:512] or None
        row.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(row)
    return AppResponse(
        success=True,
        data={
            "id": row.id,
            "display_name": row.display_name,
            "email": row.email,
            "avatar_url": row.avatar_url,
        },
    )


@router.post("/password", response_model=AppResponse)
async def change_my_password(
    request: Request, body: PasswordChangeRequest,
) -> AppResponse:
    """Change the caller's password. Verifies the current password
    before applying the new one. Works only for local-provider users
    — OAuth-managed accounts cannot have a local password."""
    from digitorn.core.database import get_session_factory
    from digitorn.core.auth.service import get_auth_service

    user_id = _user_id(request)
    auth_service = get_auth_service()
    if auth_service is None:
        raise HTTPException(
            status_code=503, detail="Auth service not initialized",
        )
    try:
        ok = await auth_service.change_password(
            user_id=user_id,
            current=body.current,
            new=body.new,
        )
    except AttributeError:
        # Auth service doesn't expose change_password in this build
        raise HTTPException(
            status_code=501,
            detail="Password change not supported by the auth provider",
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(
            status_code=400, detail="Current password is incorrect",
        )
    return AppResponse(success=True, data={"changed": True})


@router.post("/avatar", response_model=AppResponse)
async def upload_my_avatar(request: Request) -> AppResponse:
    """Upload an avatar image. Accepts a multipart file upload,
    writes it to ``~/.digitorn/avatars/<user_id>.<ext>``, and
    returns the URL the client should use in the UI."""
    from fastapi import UploadFile
    from pathlib import Path
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import User
    from sqlalchemy import select
    from datetime import datetime, timezone

    # FastAPI's dependency mechanism can't be used from inside an
    # async function with a body and a file together, so we parse
    # the multipart form manually.
    form = await request.form()
    file = form.get("file") or form.get("avatar")
    if file is None or not hasattr(file, "filename"):
        raise HTTPException(status_code=400, detail="No file provided")

    user_id = _user_id(request)

    ext = "png"
    fn = getattr(file, "filename", "") or ""
    if "." in fn:
        ext = fn.rsplit(".", 1)[-1].lower()
        if ext not in {"png", "jpg", "jpeg", "gif", "webp"}:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported image format: {ext!r}",
            )

    avatar_dir = Path.home() / ".digitorn" / "avatars"
    avatar_dir.mkdir(parents=True, exist_ok=True)
    # Safe filename — strip path separators from user_id just in case
    safe_uid = user_id.replace("/", "_").replace("\\", "_")
    target = avatar_dir / f"{safe_uid}.{ext}"
    # Reject files larger than 5 MB
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=413, detail="Avatar exceeds 5 MB limit",
        )
    target.write_bytes(data)

    # URL served by /api/users/me/avatar/{filename} (defined below)
    avatar_url = f"/api/users/me/avatar/{target.name}"

    # Persist to User row
    async with get_session_factory()() as db:
        row = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if row is not None:
            row.avatar_url = avatar_url
            row.updated_at = datetime.now(timezone.utc)
            await db.commit()

    return AppResponse(
        success=True,
        data={"avatar_url": avatar_url, "bytes": len(data)},
    )


@router.get("/avatar/{filename}")
async def serve_my_avatar(request: Request, filename: str):
    """Serve a stored avatar file.

    No auth beyond existence — avatars are user-public by design
    (they show up in chat headers). The filename includes the
    user_id so enumeration isn't interesting.
    """
    from pathlib import Path
    from fastapi.responses import FileResponse

    # Strip path components to prevent traversal
    safe = Path(filename).name
    target = Path.home() / ".digitorn" / "avatars" / safe
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Avatar not found")
    return FileResponse(str(target))
