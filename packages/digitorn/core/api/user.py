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
    # This is the LEGACY per-user token usage store — SQLAlchemy-backed,
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


def _get_rich_quota_store(request: Request):
    """Return the SQL-backed rich quota store wired by ``AppManager``.

    The store implements the same public API as ``core/quota.py::QuotaStore``
    (set/get/remove_*_quota, effective_quota, snapshot_usage,
    check_and_charge) but persists in the main SQL DB. Every admin
    write is enforced at the next ``agent_turn()`` through this
    same store — so policy set here is policy observed at runtime.
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
    """Rich admin payload — matches the ``QuotaDefinition`` schema from
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
      - ``app_id`` — restrict to one app
      - ``scope`` — ``app`` or ``user``
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
            # No global index on user overrides — walk app quotas first.
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
# Admin user management — /api/admin/users/*
#
# Fills the gap that existed before: the admin UI needs to list,
# filter, inspect, update roles, disable/enable, and delete users so
# the quota override table + app-access management can work. Built on
# top of the existing ``User`` + ``Role`` + ``UserRole`` models.
# ════════════════════════════════════════════════════════════════════


class AdminUserUpdateRequest(BaseModel):
    """PATCH body for /api/admin/users/{user_id}.

    All fields optional; only set ones get applied. ``roles`` replaces
    the user's role set for the given ``app_id`` (None == global).
    ``is_active`` toggles the soft-disable flag used by the auth layer.
    """
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    is_active: bool | None = None
    # New roles (by name: ["admin", "developer", …]) that will REPLACE
    # the current set for the given app_id scope.
    roles: list[str] | None = None
    app_id: str | None = None   # scope for the roles update; None = global


def _serialize_user(u: Any, roles: list[dict] | None = None) -> dict[str, Any]:
    return {
        "id": u.id,
        "external_id": u.external_id,
        "provider": u.provider,
        "app_id": u.app_id,
        "email": u.email,
        "display_name": u.display_name,
        "phone": u.phone,
        "avatar_url": u.avatar_url,
        "is_active": u.is_active,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "updated_at": u.updated_at.isoformat() if u.updated_at else None,
        "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
        # Reserved keys (password_hash, mfa_secret, lockout counters, …)
        # are daemon-managed and must not leak even to admins via the
        # listing route.
        "attributes": _safe_attributes(u.attributes),
        "roles": roles or [],
    }


@admin_router.get("/users", response_model=AppResponse)
async def admin_list_users(
    request: Request,
    q: str = "",               # free-text: matches email / display_name / external_id
    app_id: str = "",          # filter: users with a role on this app
    role: str = "",            # filter: users holding this role name
    is_active: str = "",       # "true" | "false" | "" (any)
    provider: str = "",        # "local" | "google" | …
    limit: int = 50,
    offset: int = 0,
) -> AppResponse:
    """Paginated user listing for the admin panel.

    Filters compose: ``q`` narrows by text AND ``app_id`` by role-scope
    AND ``role`` by role-name, etc. Returns a payload compatible with
    the Flutter admin user table.

    Each row carries its roles (name + app_id scope) so the UI can
    show "Alice — developer on digitorn-chat, admin global".
    """
    _require_admin(request)
    from sqlalchemy import select, or_, and_
    from digitorn.core.models import User, Role, UserRole
    from digitorn.core.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        stmt = select(User)
        conds = []
        if q:
            needle = f"%{q.strip().lower()}%"
            conds.append(or_(
                User.email.ilike(needle),
                User.display_name.ilike(needle),
                User.external_id.ilike(needle),
            ))
        if is_active.lower() in ("true", "1", "yes"):
            conds.append(User.is_active.is_(True))
        elif is_active.lower() in ("false", "0", "no"):
            conds.append(User.is_active.is_(False))
        if provider:
            conds.append(User.provider == provider)

        # Join via UserRole when filtering by role / app_id.
        if role or app_id:
            stmt = stmt.join(UserRole, UserRole.user_id == User.id)
            if app_id:
                conds.append(or_(
                    UserRole.app_id == app_id,
                    UserRole.app_id.is_(None),   # global roles count too
                ))
            if role:
                stmt = stmt.join(Role, Role.id == UserRole.role_id)
                conds.append(Role.name == role)
            stmt = stmt.distinct()

        if conds:
            stmt = stmt.where(and_(*conds))

        # Total count (for the UI paginator) — cheaper via subquery.
        from sqlalchemy import func
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await session.execute(count_stmt)).scalar() or 0

        stmt = stmt.order_by(User.created_at.desc()).limit(limit).offset(offset)
        rows = (await session.execute(stmt)).scalars().all()

        # Batch-fetch roles for the visible page so we don't N+1.
        user_ids = [u.id for u in rows]
        roles_by_user: dict[str, list[dict]] = {}
        if user_ids:
            rstmt = (
                select(UserRole, Role)
                .join(Role, Role.id == UserRole.role_id)
                .where(UserRole.user_id.in_(user_ids))
            )
            for ur, r in (await session.execute(rstmt)).all():
                roles_by_user.setdefault(ur.user_id, []).append({
                    "name": r.name,
                    "permissions": r.permissions or [],
                    "app_id": ur.app_id,      # None = global
                    "granted_at": ur.granted_at.isoformat() if ur.granted_at else None,
                })

        return AppResponse(success=True, data={
            "users": [_serialize_user(u, roles_by_user.get(u.id, [])) for u in rows],
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "has_more": (offset + len(rows)) < int(total),
        })


@admin_router.get("/users/{user_id}", response_model=AppResponse)
async def admin_get_user(request: Request, user_id: str) -> AppResponse:
    """Full detail of a single user. Admin-only."""
    _require_admin(request)
    from sqlalchemy import select
    from digitorn.core.models import User, Role, UserRole
    from digitorn.core.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        u = (await session.execute(
            select(User).where(User.id == user_id),
        )).scalar_one_or_none()
        if u is None:
            raise HTTPException(status_code=404, detail="User not found")

        rstmt = (
            select(UserRole, Role)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == user_id)
        )
        roles = [
            {
                "name": r.name,
                "permissions": r.permissions or [],
                "app_id": ur.app_id,
                "granted_at": ur.granted_at.isoformat() if ur.granted_at else None,
                "granted_by": ur.granted_by,
            }
            for ur, r in (await session.execute(rstmt)).all()
        ]
        return AppResponse(success=True, data=_serialize_user(u, roles))


@admin_router.patch("/users/{user_id}", response_model=AppResponse)
async def admin_update_user(
    request: Request, user_id: str, body: AdminUserUpdateRequest,
) -> AppResponse:
    """Update profile fields, the is_active flag, and/or replace roles.

    Partial — only fields actually set in the body get applied. Roles
    replacement is scoped to the given ``app_id`` (None = global).
    Returns the updated user with its full role list.

    Admin-only. Updating yourself is allowed but you CAN'T remove your
    own last admin role (would lock you out).
    """
    _require_admin(request)
    from sqlalchemy import delete, select
    from digitorn.core.models import User, Role, UserRole
    from digitorn.core.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        u = (await session.execute(
            select(User).where(User.id == user_id),
        )).scalar_one_or_none()
        if u is None:
            raise HTTPException(status_code=404, detail="User not found")

        # Profile fields
        changes: list[str] = []
        if body.display_name is not None:
            u.display_name = body.display_name.strip() or None
            changes.append("display_name")
        if body.email is not None:
            u.email = body.email.strip() or None
            changes.append("email")
        if body.phone is not None:
            u.phone = body.phone.strip() or None
            changes.append("phone")
        if body.is_active is not None:
            # Self-lockout guard: an admin can't disable themselves.
            if (
                body.is_active is False
                and user_id == _user_id(request)
            ):
                raise HTTPException(
                    status_code=400,
                    detail="You cannot disable your own account. "
                           "Ask another admin.",
                )
            u.is_active = body.is_active
            changes.append("is_active")

        # Role replacement (scope-aware)
        if body.roles is not None:
            target_app_id = body.app_id  # None = global
            # Resolve target role rows by name.
            rstmt = select(Role).where(Role.name.in_(body.roles))
            role_rows = (await session.execute(rstmt)).scalars().all()
            missing = set(body.roles) - {r.name for r in role_rows}
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown role(s): {sorted(missing)}",
                )

            # Self-lockout guard: removing your own admin role.
            if user_id == _user_id(request) and "admin" not in body.roles:
                # Verify the caller still has admin somewhere else.
                other_admin = (await session.execute(
                    select(UserRole)
                    .join(Role, Role.id == UserRole.role_id)
                    .where(
                        UserRole.user_id == user_id,
                        Role.name == "admin",
                        UserRole.app_id != target_app_id,
                    )
                )).first()
                if other_admin is None:
                    raise HTTPException(
                        status_code=400,
                        detail="You cannot remove your own last admin role. "
                               "Ask another admin.",
                    )

            # Drop existing scope roles.
            await session.execute(
                delete(UserRole).where(
                    UserRole.user_id == user_id,
                    UserRole.app_id == target_app_id,
                )
            )
            for r in role_rows:
                session.add(UserRole(
                    user_id=user_id,
                    role_id=r.id,
                    app_id=target_app_id,
                    granted_by=_user_id(request),
                ))
            changes.append(f"roles@{target_app_id or 'global'}")

        await session.commit()
        await session.refresh(u)

        # Re-fetch roles for the response.
        rstmt = (
            select(UserRole, Role)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id == user_id)
        )
        roles = [
            {
                "name": r.name, "permissions": r.permissions or [],
                "app_id": ur.app_id,
                "granted_at": ur.granted_at.isoformat() if ur.granted_at else None,
            }
            for ur, r in (await session.execute(rstmt)).all()
        ]

        # Audit trail — include the requested changes.
        from digitorn.core.audit import audit_log
        await audit_log(
            request, event_type="user.update", target_user_id=user_id,
            before={},   # the PATCH body captures the intent; full diff
                          # would require a pre-query, skipped for perf
            after={
                "changes": changes,
                "is_active": u.is_active,
                "display_name": u.display_name,
                "email": u.email,
            },
            message=f"admin updated user {user_id}: {changes}",
        )

        return AppResponse(success=True, data={
            "user": _serialize_user(u, roles),
            "changes": changes,
        })


@admin_router.delete("/users/{user_id}", response_model=AppResponse)
async def admin_delete_user(
    request: Request, user_id: str, hard: bool = False,
) -> AppResponse:
    """Soft-delete (default) or hard-delete a user.

    - **Soft delete** (``?hard=false``, default): flips ``is_active=false``
      and wipes refresh tokens so all devices log out. Preserves history.
    - **Hard delete** (``?hard=true``): cascades Session/UserRole/Token
      rows. Irreversible.

    Self-delete is refused.
    """
    _require_admin(request)
    if user_id == _user_id(request):
        raise HTTPException(
            status_code=400,
            detail="You cannot delete your own account.",
        )
    from sqlalchemy import delete, select
    from digitorn.core.models import User, RefreshToken
    from digitorn.core.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        u = (await session.execute(
            select(User).where(User.id == user_id),
        )).scalar_one_or_none()
        if u is None:
            raise HTTPException(status_code=404, detail="User not found")

        before_snapshot = _serialize_user(u, [])
        if hard:
            await session.delete(u)
            deleted_kind = "hard"
        else:
            u.is_active = False
            # Wipe refresh tokens so all active sessions get bounced.
            await session.execute(
                delete(RefreshToken).where(RefreshToken.user_id == user_id),
            )
            deleted_kind = "soft"
        await session.commit()

        from digitorn.core.audit import audit_log
        await audit_log(
            request,
            event_type=("user.delete" if hard else "user.disable"),
            target_user_id=user_id,
            before=before_snapshot,
            after={"deleted": True, "kind": deleted_kind},
            message=f"admin {deleted_kind}-deleted user {user_id}",
        )

        return AppResponse(success=True, data={
            "user_id": user_id,
            "deleted": True,
            "kind": deleted_kind,
        })


# ── Roles catalog (read-only for now) ───────────────────────────────


@admin_router.get("/audit-log", response_model=AppResponse)
async def admin_list_audit_log(
    request: Request,
    event_type: str = "",           # filter: exact type (e.g. "quota.set_app") OR "quota.*"
    actor_user_id: str = "",
    target_user_id: str = "",
    target_app_id: str = "",
    since_ts: str = "",              # ISO8601 — return rows with ts >= since_ts
    until_ts: str = "",              # ISO8601 — return rows with ts <  until_ts
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
    immutable — there is deliberately no PATCH / DELETE route.
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
                    detail=f"Invalid since_ts — use ISO8601, got {since_ts!r}",
                )
        if until_ts:
            try:
                conds.append(
                    HistoryLog.ts < _dt.fromisoformat(until_ts.replace("Z", "+00:00"))
                )
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid until_ts — use ISO8601, got {until_ts!r}",
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
# has its own try/except returning 0 — one missing component (e.g. MCP
# pool not initialised) must not break the whole dashboard.

_admin_stats_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_ADMIN_STATS_TTL_S = 30.0


@admin_router.get("/stats", response_model=AppResponse)
async def admin_get_stats(request: Request) -> AppResponse:
    """Daemon-wide counters for the admin Overview dashboard.

    Returns counts scoped to the whole daemon — users, deployed apps,
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
        User, Application, InstalledPackage, Credential, UserSession,
        UsageEvent,
    )

    stats: dict[str, Any] = {
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
            stats["users"] = await _count(db, select(func.count(User.id)))
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

    # MCP pool lives on app.state and is optional — if the pool never
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

    All fields optional. Only provided fields get applied — omitting a
    field leaves it untouched. For ``attributes`` the update is a
    **deep-merge**: send ``{"attributes": {"locale": "fr"}}`` to set
    that one key without wiping the rest. Send a key with ``null`` to
    delete it (e.g. ``{"attributes": {"timezone": null}}``).

    Security: the ``attributes`` bag is used internally for sensitive
    things (password hashes, OAuth refresh cursors, risk flags). The
    route filters out reserved keys before any read or write — the
    client can only touch keys that aren't on the deny-list.
    """
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    # Free-form preferences bag: locale, timezone, notification_prefs,
    # theme, etc. Reserved keys (see _ATTR_RESERVED_KEYS) are rejected.
    attributes: dict[str, Any] | None = None


# Keys inside ``user.attributes`` that are MANAGED BY THE DAEMON and
# must never leak to clients or be writable from the self-service
# route. Read paths strip them; write paths raise 400.
_ATTR_RESERVED_KEYS: frozenset[str] = frozenset({
    "password_hash",       # local provider's bcrypt hash
    "password_salt",       # legacy / PBKDF2 variants
    "password_updated_at", # lockout protection
    "failed_login_count",  # lockout protection
    "locked_until",        # lockout protection
    "risk_score",          # daemon-computed
    "mfa_secret",          # TOTP shared secret
    "mfa_recovery_codes",  # recovery codes
    "oauth_refresh_cursor",
    "sso_raw_claims",
})


def _safe_attributes(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of ``raw`` with reserved keys stripped."""
    if not raw:
        return {}
    return {k: v for k, v in raw.items() if k not in _ATTR_RESERVED_KEYS}


def _deep_merge_attributes(
    base: dict[str, Any], patch: dict[str, Any],
) -> dict[str, Any]:
    """Recursive deep-merge of ``patch`` into ``base``.

    Rules:
      * ``patch[k] is None``                    → delete ``base[k]``
      * ``patch[k]`` is a dict AND
        ``base[k]`` is a dict                   → merge recursively
      * anything else (scalar, list, type swap) → overwrite

    Mutates and returns ``base``. Without the recursion, a caller
    that sends ``{"ui": {"language": "en"}}`` to update ONE nested
    key would clobber every sibling under ``ui`` — the previous
    shallow implementation did exactly that and wiped
    ``ui.theme_mode`` / ``ui.theme_palette`` / ``ui.density`` on
    every language change.
    """
    for k, v in patch.items():
        if v is None:
            base.pop(k, None)
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge_attributes(base[k], v)
        else:
            base[k] = v
    return base


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
            # Strip daemon-managed reserved keys (password_hash, mfa_*,
            # lockout counters, …) so they never leak to the client.
            "attributes": _safe_attributes(row.attributes),
        },
    )


@router.put("/profile", response_model=AppResponse)
async def update_my_profile(
    request: Request, body: ProfileUpdateRequest,
) -> AppResponse:
    """Self-service profile update.

    Supported fields (all optional):

    - ``display_name`` — visible name
    - ``email``        — contact address
    - ``phone``        — contact phone (free-form)
    - ``attributes``   — deep-merged into the existing attributes bag.
                         Pass ``{key: null}`` to delete a key.

    Other fields (``is_active``, ``roles``, ``provider``, ``external_id``,
    ``app_id``) are admin-only — they're not accepted here. Non-admins
    have no way to escalate via this route.

    Returns the updated profile row so the client can hydrate its
    state without a second round-trip.
    """
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import User
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified
    from datetime import datetime, timezone

    user_id = _user_id(request)
    changes: list[str] = []
    async with get_session_factory()() as db:
        row = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=404, detail="User record not found",
            )
        if body.display_name is not None:
            new_val = body.display_name.strip()[:512] or None
            if new_val != row.display_name:
                row.display_name = new_val
                changes.append("display_name")
        if body.email is not None:
            new_email = body.email.strip()[:512] or None
            if new_email != row.email:
                row.email = new_email
                changes.append("email")
        if body.phone is not None:
            new_phone = body.phone.strip()[:64] or None
            if new_phone != row.phone:
                row.phone = new_phone
                changes.append("phone")
        if body.attributes is not None:
            # Reject attempts to write reserved/daemon-managed keys
            # (password_hash, mfa_secret, lockout counters, …). Any
            # such key would be a privilege-escalation or tamper vector.
            forbidden = (
                set(body.attributes) & _ATTR_RESERVED_KEYS
            )
            if forbidden:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Attributes {sorted(forbidden)} are managed by "
                        f"the daemon and cannot be set via this route. "
                        f"Use the dedicated endpoints (e.g. POST /password)."
                    ),
                )
            # Recursive deep-merge: caller's keys win at every level;
            # a None value deletes the key at its level. See
            # ``_deep_merge_attributes`` for the full contract —
            # specifically, sending ``{"ui": {"language": "en"}}``
            # only updates ``ui.language`` and leaves the other
            # ``ui.*`` siblings intact.
            import copy as _copy
            current = _copy.deepcopy(dict(row.attributes or {}))
            _deep_merge_attributes(current, body.attributes)
            if current != (row.attributes or {}):
                row.attributes = current
                # JSON column: tell SQLAlchemy the dict instance changed
                # in-place so it actually writes back on commit.
                flag_modified(row, "attributes")
                changes.append("attributes")
        if changes:
            row.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(row)
    return AppResponse(
        success=True,
        data={
            "id": row.id,
            "display_name": row.display_name,
            "email": row.email,
            "phone": row.phone,
            "avatar_url": row.avatar_url,
            # Strip reserved keys on response too — never leak them.
            "attributes": _safe_attributes(row.attributes),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "changes": changes,
        },
    )


@router.post("/password", response_model=AppResponse)
async def change_my_password(
    request: Request, body: PasswordChangeRequest,
) -> AppResponse:
    """Change the caller's password. Verifies the current password
    before applying the new one. Works only for local-provider users
    — OAuth-managed accounts cannot have a local password."""
    user_id = _user_id(request)
    auth_service = getattr(request.app.state, "auth_service", None)
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
    except HTTPException:
        raise
    except Exception as exc:
        logger.info("change_password rejected user=%s: %s", user_id, exc)
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
