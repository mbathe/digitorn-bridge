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
from digitorn.core.quota import MetricQuota, QuotaDefinition

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
    # This is the LEGACY per-user token usage store - SQLAlchemy-backed,
    # scopes = user / user_app / app, enforces token caps through
    # ``agent_loop`` hooks. It's a DIFFERENT object than the admin-
    # contract ``app.state.quota_store`` used by the new 6 quota routes
    # in ``apps.py``, which uses the rich ``core/quota.py`` model (rolling
    # windows, per-model overrides, etc). We read from the dedicated
    # ``usage_quota_store`` slot to keep the two systems apart.
    store = getattr(request.app.state, "usage_quota_store", None)
    if store is None:
        # Backward compat: old deployments may still have the legacy
        # store mounted under ``quota_store``.
        store = getattr(request.app.state, "quota_store", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="Usage quota store not initialized",
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
# F. Usage & quotas (token monitoring)
# ════════════════════════════════════════════════════════════════════


@router.get("/usage", response_model=AppResponse)
async def get_my_usage(request: Request) -> AppResponse:
    """Complete usage summary for the authenticated user.

    Powers the Settings → Usage screen. Combines monthly totals,
    cost by model, 24h hourly time series, 30d daily time series,
    a per-app breakdown, and the caller's active quota status.

    The response is computed on the fly from ``usage_events`` -
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

    # Next month boundary - same calendar trick as the quota window
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


def _get_rich_quota_store(request: Request):
    """Return the SQL-backed rich quota store wired by ``AppManager``.

    The store implements the same public API as ``core/quota.py::QuotaStore``
    (set/get/remove_*_quota, effective_quota, snapshot_usage,
    check_and_charge) but persists in the main SQL DB. Every admin
    write is enforced at the next ``agent_turn()`` through this
    same store - so policy set here is policy observed at runtime.
    """
    manager = _get_manager(request)
    store = getattr(manager, "_quota_store", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="Quota store not initialized",
        )
    return store


def _legacy_upsert_to_rich(body: "QuotaUpsertRequest") -> tuple[
    str, str, str | None, QuotaDefinition,
]:
    """Translate the legacy ``{scope_type, scope_id, app_id, period,
    tokens_limit}`` body into the rich ``QuotaDefinition`` schema.

    Returns ``(scope, app_id, user_id, QuotaDefinition)``:
      - ``scope_type='user_app'`` → scope='user', app_id=body.app_id, user_id=body.scope_id
      - ``scope_type='app'``      → scope='app',  app_id=body.app_id or body.scope_id, user_id=None
      - ``scope_type='user'``     → not storable in per-app rich store; raise 400.
    """
    period_to_window = {
        "day": "per_day", "week": "per_week", "month": "per_month",
    }
    window = period_to_window.get(body.period, "per_month")
    metric_quota = MetricQuota.model_validate({
        window: {"limit": body.tokens_limit, "reset": "fixed"},
    })
    rich = QuotaDefinition(tokens_total=metric_quota)

    if body.scope_type == "user_app":
        if not body.app_id:
            raise ValueError("user_app quota requires app_id")
        return ("user", body.app_id, body.scope_id, rich)
    if body.scope_type == "app":
        target_app = body.app_id or body.scope_id
        if not target_app:
            raise ValueError("app quota requires app_id")
        return ("app", target_app, None, rich)
    if body.scope_type == "user":
        raise ValueError(
            "Cross-app 'user' scope quotas are not supported by the rich "
            "store. Set a 'user_app' quota per app, or define a "
            "cross-app global default in settings.server.rate_limit_rpm."
        )
    raise ValueError(f"Unknown scope_type: {body.scope_type!r}")


class QuotaRichUpsertRequest(BaseModel):
    """Rich admin payload - matches the ``QuotaDefinition`` schema from
    ``core/quota.py`` 1:1 plus scope selectors.

    Examples
    --------

    App-level quota (applies to every user of the app)::

        {
          "scope": "app",
          "app_id": "my-app",
          "quota": {
            "requests": {"per_minute": 1000},
            "tokens_total": {"per_day": 500000},
            "cost_usd": {"per_month": 100.0}
          }
        }

    User override with per-model stricter limit::

        {
          "scope": "user",
          "app_id": "my-app",
          "user_id": "alice",
          "quota": {
            "tokens_total": {"per_day": 10000},
            "models": {
              "claude-opus-4-6": {
                "tokens_total": {"per_day": 5000}
              }
            }
          }
        }
    """
    scope: str = Field(..., description="'app' or 'user'")
    app_id: str
    user_id: str | None = None
    quota: QuotaDefinition


@admin_router.get("/quotas", response_model=AppResponse)
async def admin_list_quotas(
    request: Request,
    app_id: str = "",
    scope: str = "",
) -> AppResponse:
    """List every quota definition in the SQL rich store.

    Optional filters:
      - ``app_id`` - restrict to one app
      - ``scope`` - ``app`` or ``user``
    """
    _require_admin(request)
    store = _get_rich_quota_store(request)
    out: list[dict[str, Any]] = []
    if not scope or scope == "app":
        for env in store.list_app_quotas():
            if app_id and env.get("app_id") != app_id:
                continue
            out.append({"scope": "app", **env})
    if not scope or scope == "user":
        if app_id:
            for env in store.list_user_overrides(app_id):
                out.append({"scope": "user", "app_id": app_id, **env})
        else:
            # No global index on user overrides - walk app quotas first.
            for app_env in store.list_app_quotas():
                aid = app_env.get("app_id")
                if not aid:
                    continue
                for env in store.list_user_overrides(aid):
                    out.append({"scope": "user", "app_id": aid, **env})
    return AppResponse(
        success=True, data={"quotas": out, "count": len(out)},
    )


@admin_router.post("/quotas", response_model=AppResponse)
async def admin_upsert_quota(
    request: Request, body: dict[str, Any],
) -> AppResponse:
    """Create or update a quota definition.

    Accepts the **rich** body ``{scope, app_id, user_id?, quota:
    QuotaDefinition}`` (recommended) OR the legacy body ``{scope_type,
    scope_id, app_id?, period, tokens_limit}`` (kept for existing admin
    panel builds during the transition). Both write to the SQL store.
    """
    _require_admin(request)
    store = _get_rich_quota_store(request)
    caller = _user_id(request)

    try:
        if "quota" in body and "scope" in body:
            rich = QuotaRichUpsertRequest.model_validate(body)
            if rich.scope == "app":
                env = store.set_app_quota(
                    rich.app_id, rich.quota, updated_by=caller,
                )
                return AppResponse(
                    success=True,
                    data={"scope": "app", "app_id": rich.app_id, **env},
                )
            if rich.scope == "user":
                if not rich.user_id:
                    raise HTTPException(
                        status_code=400,
                        detail="scope='user' requires user_id",
                    )
                env = store.set_user_quota(
                    rich.app_id, rich.user_id, rich.quota,
                    updated_by=caller,
                )
                return AppResponse(success=True, data={
                    "scope": "user", "app_id": rich.app_id,
                    "user_id": rich.user_id, **env,
                })
            raise HTTPException(
                status_code=400,
                detail=f"Unknown scope: {rich.scope!r}",
            )
        # Legacy body → translate.
        legacy = QuotaUpsertRequest.model_validate(body)
        scope, target_app, target_user, rich_def = _legacy_upsert_to_rich(legacy)
        if scope == "app":
            env = store.set_app_quota(target_app, rich_def, updated_by=caller)
            return AppResponse(success=True, data={
                "scope": "app", "app_id": target_app, **env,
            })
        env = store.set_user_quota(
            target_app, target_user or "", rich_def, updated_by=caller,
        )
        return AppResponse(success=True, data={
            "scope": "user", "app_id": target_app,
            "user_id": target_user, **env,
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@admin_router.delete("/quotas", response_model=AppResponse)
async def admin_delete_quota(
    request: Request, app_id: str, user_id: str = "",
) -> AppResponse:
    """Delete an app or user quota.

    ``user_id`` empty → deletes the app-level quota.
    ``user_id`` set  → deletes the user override for that app.
    """
    _require_admin(request)
    store = _get_rich_quota_store(request)
    if user_id:
        ok = store.remove_user_quota(app_id, user_id)
    else:
        ok = store.remove_app_quota(app_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Quota not found")
    return AppResponse(success=True, data={
        "deleted": True, "app_id": app_id,
        "user_id": user_id or None,
    })


# ════════════════════════════════════════════════════════════════════
# Admin user management was removed when identity moved to the
# central digitorn-auth service. Admin user CRUD - list, search,
# inspect, update, soft/hard-delete - now lives on the auth service
# (or its dashboard). The role-catalog and audit-log endpoints below
# stay daemon-side because roles + audit are daemon-scoped concerns.
# ════════════════════════════════════════════════════════════════════


# AdminUserUpdateRequest + _serialize_user removed - admin user CRUD
# now lives on the central digitorn-auth service.




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
        UsageEvent,
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
            month_start = datetime.now(timezone.utc).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0,
            )
            stats["monthly_cost_usd"] = round(
                await _scalar_float(
                    db, select(func.coalesce(func.sum(UsageEvent.cost_usd), 0.0))
                    .where(UsageEvent.created_at >= month_start),
                ),
                4,
            )

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
