"""Daemon admin REST + WebSocket routes.

Surface (all gated by admin role, all under ``/api/admin/daemon/*``):

  GET    /api/admin/daemon/summary               — top-level overview
  GET    /api/admin/daemon/diagnostics           — current snapshot
  GET    /api/admin/daemon/diagnostics/history   — last N seconds of snapshots
  WS     /api/admin/daemon/diagnostics/stream    — live snapshots, 1 Hz
  GET    /api/admin/daemon/workers               — worker subprocess list
  POST   /api/admin/daemon/workers/{id}/restart  — terminate (monitor respawns)
  GET    /api/admin/daemon/sessions/active       — active session list
  POST   /api/admin/daemon/sessions/{app}/{sid}/kill  — cancel an agent turn
  GET    /api/admin/daemon/config                — full schema + current values
  GET    /api/admin/daemon/config/overrides      — current persisted overrides
  PUT    /api/admin/daemon/config/{key}          — set an override
  DELETE /api/admin/daemon/config/{key}          — clear an override

Every endpoint reads from the in-memory telemetry hub / config registry.
There is NO synchronous I/O on the request path — disk writes go through
``asyncio.to_thread`` inside the registry; psutil sampling happens on the
hub's own background task.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import (
    APIRouter, HTTPException, Path, Query, Request, WebSocket,
    WebSocketDisconnect, status,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/daemon", tags=["admin", "daemon"])


# ── Auth ─────────────────────────────────────────────────────────


def _require_admin(request: Request) -> None:
    """Mirror gateway_admin's gate: ``admin`` or ``developer`` role
    accepted. The daemon's auth middleware has already validated the
    bearer; we only check the resulting permission set.
    """
    perms = getattr(request.state, "permissions", []) or []
    roles = getattr(request.state, "roles", []) or []
    if "*" in perms or "admin" in perms:
        return
    if "admin" in roles or "developer" in roles:
        return
    raise HTTPException(403, detail="admin_or_developer_role_required")


async def _require_admin_ws(websocket: WebSocket) -> bool:
    """Same check for WebSocket upgrades. The auth middleware may not
    populate ``state`` on WS depending on the routing path; fall back
    to checking the Authorization header verbatim. Returns ``True`` on
    accept; on reject the socket is closed with policy-violation 1008.
    """
    perms = getattr(websocket.state, "permissions", []) or []
    roles = getattr(websocket.state, "roles", []) or []
    if "*" in perms or "admin" in perms or "admin" in roles or "developer" in roles:
        return True
    # Middleware did not run on this upgrade — be conservative: reject.
    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
    return False


# ── Helpers ──────────────────────────────────────────────────────


def _hub() -> Any:
    from digitorn.core.runtime.telemetry_hub import get_hub
    return get_hub()


def _registry() -> Any:
    from digitorn.core.config_registry import get_registry
    return get_registry()


def _snap_to_json(snap: Any) -> dict[str, Any] | None:
    if snap is None:
        return None
    return snap.to_dict()


# ── Summary ──────────────────────────────────────────────────────


@router.get("/summary")
async def daemon_summary(request: Request) -> dict[str, Any]:
    """Lightweight top-card data for the Overview tab. Aggregates the
    last snapshot + counts. Cheap (no I/O)."""
    _require_admin(request)
    hub = _hub()
    snap = hub.current() if hub is not None else None
    reg = _registry()
    return {
        "telemetry_enabled": hub is not None,
        "config_registry_enabled": reg is not None,
        "schema_fields": len(reg.schema()) if reg is not None else 0,
        "overrides_active": len(reg.overrides()) if reg is not None else 0,
        "last_snapshot": _snap_to_json(snap),
        "workers_running": int(_count_running_workers(request)),
    }


def _count_running_workers(request: Request) -> int:
    try:
        lifecycle = getattr(request.app.state, "worker_lifecycle", None)
        if lifecycle is None:
            return 0
        return int(getattr(lifecycle, "worker_count", 0) or 0)
    except Exception:
        return 0


# ── Diagnostics ──────────────────────────────────────────────────


@router.get("/diagnostics")
async def diagnostics_current(request: Request) -> dict[str, Any]:
    _require_admin(request)
    hub = _hub()
    if hub is None:
        return {"enabled": False, "snapshot": None}
    return {"enabled": True, "snapshot": _snap_to_json(hub.current())}


@router.get("/diagnostics/history")
async def diagnostics_history(
    request: Request,
    seconds: int = Query(60, ge=1, le=300),
) -> dict[str, Any]:
    _require_admin(request)
    hub = _hub()
    if hub is None:
        return {"enabled": False, "snapshots": []}
    snaps = [_snap_to_json(s) for s in hub.history(seconds)]
    return {"enabled": True, "snapshots": snaps, "count": len(snaps)}


@router.websocket("/diagnostics/stream")
async def diagnostics_stream(websocket: WebSocket) -> None:
    """Push live snapshots to subscribers at 1 Hz. The collector pushes
    into a per-consumer queue with drop-oldest overflow, so a slow
    client never back-pressures the collector."""
    await websocket.accept()
    if not await _require_admin_ws(websocket):
        return
    hub = _hub()
    if hub is None:
        await websocket.send_json({"error": "telemetry_disabled"})
        await websocket.close()
        return
    q = hub.subscribe()
    try:
        # Send the latest available snapshot first so the UI paints
        # immediately rather than waiting for the next 1 Hz tick.
        latest = hub.current()
        if latest is not None:
            await websocket.send_json(_snap_to_json(latest))
        while True:
            snap = await q.get()
            await websocket.send_json(_snap_to_json(snap))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("diagnostics_stream_error: %s", exc)
    finally:
        hub.unsubscribe(q)


# ── Workers ──────────────────────────────────────────────────────


@router.get("/workers")
async def list_workers(request: Request) -> dict[str, Any]:
    _require_admin(request)
    lifecycle = getattr(request.app.state, "worker_lifecycle", None)
    if lifecycle is None:
        return {"enabled": False, "workers": []}
    running = getattr(lifecycle, "_running", None) or {}
    out: list[dict[str, Any]] = []
    for wid, rw in running.items():
        proc = getattr(rw, "proc", None)
        cfg = getattr(rw, "cfg", None)
        out.append({
            "worker_id": wid,
            "pid": int(getattr(proc, "pid", 0) or 0) if proc else 0,
            "returncode": getattr(proc, "returncode", None) if proc else None,
            "running": proc.returncode is None if proc is not None else False,
            "port": int(getattr(cfg, "port", 0) or 0) if cfg else 0,
            "modules": list(getattr(cfg, "modules", []) or []) if cfg else [],
            "restart_count": int(getattr(rw, "restart_count", 0) or 0),
        })

    # Enrich each row with the latest cpu_percent + rss_mb from the
    # telemetry hub's snapshot. The hub samples once per second in the
    # background, so this read is microseconds and never hits psutil
    # on the request path. Match by worker_id (PIDs can change between
    # the hub's last tick and this call - id is stable, pid is not).
    hub = _hub()
    if hub is not None:
        snap = hub.current()
        if snap is not None:
            by_id: dict[str, dict[str, Any]] = {
                str(w.get("worker_id") or ""): w
                for w in (snap.workers or [])
            }
            for row in out:
                enriched = by_id.get(row["worker_id"])
                if enriched is None:
                    continue
                if "cpu_percent" in enriched:
                    row["cpu_percent"] = enriched["cpu_percent"]
                if "rss_mb" in enriched:
                    row["rss_mb"] = enriched["rss_mb"]

    out.sort(key=lambda w: w["worker_id"])
    return {"enabled": True, "workers": out, "count": len(out)}


@router.post("/workers/{worker_id}/restart")
async def restart_worker(
    request: Request,
    worker_id: str = Path(..., min_length=1, max_length=128),
) -> dict[str, Any]:
    """Terminate the worker process. The per-worker monitor task
    detects the death and respawns with the standard back-off. We do
    NOT call any private API of the lifecycle — we just send SIGTERM
    and let the existing supervision do its job."""
    _require_admin(request)
    lifecycle = getattr(request.app.state, "worker_lifecycle", None)
    if lifecycle is None:
        raise HTTPException(503, detail="workers_subsystem_not_running")
    running = getattr(lifecycle, "_running", None) or {}
    rw = running.get(worker_id)
    if rw is None:
        raise HTTPException(404, detail=f"unknown_worker:{worker_id}")
    proc = getattr(rw, "proc", None)
    if proc is None or proc.returncode is not None:
        return {
            "ok": True,
            "worker_id": worker_id,
            "noop": "already_stopped",
            "monitor_will_respawn": True,
        }
    try:
        proc.terminate()
    except ProcessLookupError:
        return {"ok": True, "worker_id": worker_id, "noop": "vanished"}
    except Exception as exc:
        raise HTTPException(500, detail=f"terminate_failed:{exc}")
    logger.info(
        "admin_worker_restart_triggered worker_id=%s pid=%s",
        worker_id, proc.pid,
    )
    return {
        "ok": True,
        "worker_id": worker_id,
        "pid": proc.pid,
        "monitor_will_respawn": True,
    }


# ── Sessions ─────────────────────────────────────────────────────


@router.get("/sessions/active")
async def list_active_sessions(request: Request) -> dict[str, Any]:
    """Return the active sessions as known by the daemon's app manager.
    Reads the in-memory ``_session_tasks`` + ``_active_contexts`` dicts —
    no DB hop, no hot-path mutation. Per-session detail is read-only.
    """
    _require_admin(request)
    manager = _get_manager_safe(request)
    if manager is None:
        return {"enabled": False, "sessions": []}
    tasks = getattr(manager, "_session_tasks", None) or {}
    ctxs = getattr(manager, "_active_contexts", None) or {}
    out: list[dict[str, Any]] = []
    for key, task in list(tasks.items()):
        app_id, _, session_id = key.partition(":")
        ctx = ctxs.get(key)
        agent_id = getattr(ctx, "agent_id", None) if ctx else None
        workspace = getattr(ctx, "workspace", None) if ctx else None
        messages = getattr(ctx, "messages", None) if ctx else None
        messages_count = (
            len(messages) if isinstance(messages, list) else 0
        )
        run_id = getattr(ctx, "current_run_id", None) if ctx else None
        cancel_evt = getattr(ctx, "cancel_event", None) if ctx else None
        cancel_signaled = False
        if cancel_evt is not None:
            try:
                cancel_signaled = bool(cancel_evt.is_set())
            except Exception:
                cancel_signaled = False
        out.append({
            "app_id": app_id,
            "session_id": session_id,
            "active": task is not None and not task.done(),
            "agent_id": agent_id or "",
            "workspace": workspace or "",
            "messages_count": messages_count,
            "run_id": run_id or "",
            "cancel_signaled": cancel_signaled,
        })
    out.sort(key=lambda s: (s["app_id"], s["session_id"]))
    return {"enabled": True, "sessions": out, "count": len(out)}


@router.post("/sessions/{app_id}/{session_id}/kill")
async def kill_session(
    request: Request,
    app_id: str = Path(..., min_length=1, max_length=128),
    session_id: str = Path(..., min_length=1, max_length=128),
) -> dict[str, Any]:
    """Cancel a running agent turn. Re-uses the same machinery as the
    user-facing ``/abort`` endpoint: cancels the asyncio task AND sets
    the cooperative cancel_event so a long tool loop bails at the next
    checkpoint."""
    _require_admin(request)
    manager = _get_manager_safe(request)
    if manager is None:
        raise HTTPException(503, detail="app_manager_not_ready")
    active_key = f"{app_id}:{session_id}"

    task_cancelled = False
    task = (manager._session_tasks or {}).get(active_key)
    if task is not None and not task.done():
        task.cancel()
        task_cancelled = True

    cooperative_signaled = 0
    try:
        active_ctxs = getattr(manager, "_active_contexts", None) or {}
        ctx_obj = active_ctxs.get(active_key)
        if ctx_obj is not None:
            ev = getattr(ctx_obj, "cancel_event", None)
            if ev is not None:
                try:
                    ev.set()
                    cooperative_signaled = 1
                except Exception:
                    pass
            try:
                ctx_obj.cancel_reason = "admin_kill"
            except Exception:
                pass
    except Exception as exc:
        logger.debug("admin_kill_session_ctx_signal_failed: %s", exc)

    logger.info(
        "admin_session_killed app=%s session=%s task_cancelled=%s "
        "cooperative=%d",
        app_id, session_id, task_cancelled, cooperative_signaled,
    )
    return {
        "ok": True,
        "app_id": app_id,
        "session_id": session_id,
        "task_cancelled": task_cancelled,
        "cooperative_signaled": bool(cooperative_signaled),
    }


def _get_manager_safe(request: Request) -> Any:
    try:
        return getattr(request.app.state, "app_manager", None)
    except Exception:
        return None


# ── Config ───────────────────────────────────────────────────────


@router.get("/config")
async def config_schema(request: Request) -> dict[str, Any]:
    _require_admin(request)
    reg = _registry()
    if reg is None:
        return {"enabled": False, "schema": []}
    return {"enabled": True, "schema": reg.schema()}


@router.get("/config/overrides")
async def config_overrides(request: Request) -> dict[str, Any]:
    _require_admin(request)
    reg = _registry()
    if reg is None:
        return {"enabled": False, "overrides": {}}
    return {"enabled": True, "overrides": reg.overrides()}


@router.put("/config/{key:path}")
async def config_set(
    request: Request,
    key: str = Path(..., min_length=1, max_length=256),
) -> dict[str, Any]:
    _require_admin(request)
    reg = _registry()
    if reg is None:
        raise HTTPException(503, detail="config_registry_not_running")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, detail="invalid_json_body")
    if not isinstance(body, dict) or "value" not in body:
        raise HTTPException(
            400, detail="body_must_be_object_with_value_field",
        )
    result = await reg.set(key, body["value"])
    if not result.get("ok"):
        # Coercion / range failures land as 422 rather than 500 so the
        # UI can render the underlying validation message inline next
        # to the form field that was rejected.
        if result.get("error") == "invalid_value":
            raise HTTPException(422, detail=result)
        if result.get("error", "").startswith("unknown_key"):
            raise HTTPException(404, detail=result)
        raise HTTPException(500, detail=result)
    return result


@router.delete("/config/{key:path}")
async def config_reset(
    request: Request,
    key: str = Path(..., min_length=1, max_length=256),
) -> dict[str, Any]:
    _require_admin(request)
    reg = _registry()
    if reg is None:
        raise HTTPException(503, detail="config_registry_not_running")
    result = await reg.reset(key)
    if not result.get("ok"):
        raise HTTPException(404, detail=result)
    return result
