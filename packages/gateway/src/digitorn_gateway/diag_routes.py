"""Operator-facing diagnostic endpoints.

Five admin-only tools designed to validate the gateway's health and
configuration WITHOUT touching the hot path of real user requests.

  * POST /admin/diag/credentials/{cred_id}/test
        Tries a 1-token chat completion using ONLY this credential.
        Returns {ok, latency_ms, message, error}. Does not record
        anything in usage stats.

  * POST /admin/diag/routes/{route_id}/probe
        Manual probe of a specific route. Same payload as the
        background prober but on demand.

  * POST /admin/diag/quick-chat
        Sends a tiny chat through the dispatch path. Bypasses
        `record()` so the operator's call doesn't pollute their own
        quota counters or the analytics. Returns the raw provider
        response + token usage + latency.

  * POST /admin/diag/refresh-cache
        Reload PlanRegistry + ConfigCache from DB without restart.

  * GET /admin/diag/system
        Aggregate runtime info: uptime, supervisor state, plan count,
        worker pid, memory.

Hot-path discipline:
  * Every probe uses the existing httpx pool (1 connection per probe).
  * Every probe has a 5s timeout — operator can never DoS users.
  * `quick-chat` and `test` do NOT call ``record()`` so admin probing
    never inflates real user counters.
  * `refresh-cache` is single-flight via the registry's existing lock.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn_gateway.auth import GatewayPrincipal, require_principal
from digitorn_gateway.config_cache import get_cache
from digitorn_gateway.db import get_session_factory, session_dependency
from digitorn_gateway.models_db import GatewayCredential, GatewayRoute
from digitorn_gateway.plans import get_registry
from digitorn_gateway.route_prober import _probe_one


router = APIRouter()


_PROBE_TIMEOUT_S = 5.0
_BOOT_TIME = time.monotonic()


def _require_admin(principal: GatewayPrincipal) -> None:
    if not (principal.roles and (
        "admin" in principal.roles or "developer" in principal.roles
    )):
        raise HTTPException(403, detail="admin_role_required")


# ── Credential test ────────────────────────────────────────────────


@router.post("/admin/diag/credentials/{cred_id}/test")
async def diag_test_credential(
    cred_id: str,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Try a 1-token chat completion using this credential against its
    provider. Returns success + latency, or the upstream error so the
    operator can debug an expired key / wrong scope / etc."""
    _require_admin(principal)

    row = await db.get(GatewayCredential, cred_id)
    if row is None:
        raise HTTPException(404, detail=f"credential_not_found: {cred_id}")

    cache = get_cache()
    # Find any route bound to this credential, OR synthesise one from
    # the provider default.
    route = next(
        (r for r in cache.all_routes() if str(r.credential_id) == str(cred_id)),
        None,
    )
    if route is None:
        # Try resolve via the provider's default route synthesis. We
        # need an alias; pick any model belonging to the same provider.
        provider_slug = row.provider_slug
        model = next(
            (m for m in cache.all_models() if m.provider_slug == provider_slug),
            None,
        )
        if model is None:
            return {
                "ok": False,
                "credential_id": cred_id,
                "error": (
                    f"no_route_or_model_for_provider: {provider_slug} "
                    "(create a model + route bound to this credential first)"
                ),
                "latency_ms": 0,
            }
        rd = cache.resolve_dispatch(model.alias)
    else:
        all_routes = cache._routes.get(route.model_alias, [])  # type: ignore[attr-defined]
        try:
            idx = all_routes.index(route)
        except ValueError:
            idx = 0
        rd = cache.resolve_dispatch_at(route.model_alias, idx)

    if rd is None:
        return {
            "ok": False, "credential_id": cred_id,
            "error": "could_not_resolve_dispatch", "latency_ms": 0,
        }

    t0 = time.monotonic()
    try:
        ok = await asyncio.wait_for(_probe_one(rd), timeout=_PROBE_TIMEOUT_S)
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "ok": ok,
            "credential_id": cred_id,
            "provider": rd.provider_slug,
            "model": rd.real_model_id,
            "latency_ms": latency_ms,
            "message": "credential reached the upstream successfully" if ok
                       else "probe failed (see gateway logs for details)",
        }
    except asyncio.TimeoutError:
        return {
            "ok": False, "credential_id": cred_id,
            "error": f"timeout after {_PROBE_TIMEOUT_S}s",
            "latency_ms": int(_PROBE_TIMEOUT_S * 1000),
        }
    except Exception as exc:
        return {
            "ok": False, "credential_id": cred_id,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }


# ── Route probe (manual) ───────────────────────────────────────────


@router.post("/admin/diag/routes/{route_id}/probe")
async def diag_probe_route(
    route_id: str,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Force a probe of one specific route id. Uses the same code path
    as the background prober so the result mirrors what the dispatch
    layer would see."""
    _require_admin(principal)

    cache = get_cache()
    route = next(
        (r for r in cache.all_routes() if str(r.id) == str(route_id)),
        None,
    )
    if route is None:
        raise HTTPException(404, detail=f"route_not_found: {route_id}")

    all_routes = cache._routes.get(route.model_alias, [])  # type: ignore[attr-defined]
    try:
        idx = all_routes.index(route)
    except ValueError:
        idx = 0
    rd = cache.resolve_dispatch_at(route.model_alias, idx)
    if rd is None:
        return {
            "ok": False, "route_id": route_id,
            "error": "could_not_resolve_dispatch", "latency_ms": 0,
        }

    t0 = time.monotonic()
    try:
        ok = await asyncio.wait_for(_probe_one(rd), timeout=_PROBE_TIMEOUT_S)
        latency_ms = int((time.monotonic() - t0) * 1000)
        if ok:
            cache.mark_route_success(route.id)
        return {
            "ok": ok,
            "route_id": route_id,
            "model_alias": route.model_alias,
            "provider": rd.provider_slug,
            "latency_ms": latency_ms,
        }
    except asyncio.TimeoutError:
        cache.mark_route_failure(route.id, "probe_timeout", cooldown_s=30.0)
        return {"ok": False, "route_id": route_id, "error": "timeout",
                "latency_ms": int(_PROBE_TIMEOUT_S * 1000)}
    except Exception as exc:
        cache.mark_route_failure(route.id, "probe_failed", cooldown_s=30.0)
        return {"ok": False, "route_id": route_id,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_ms": int((time.monotonic() - t0) * 1000)}


# ── Quick chat (bypass quota recording) ────────────────────────────


class QuickChatRequest(BaseModel):
    model: str
    message: str = "ping"
    max_tokens: int = 10


@router.post("/admin/diag/quick-chat")
async def diag_quick_chat(
    body: QuickChatRequest,
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """Send a tiny chat through the dispatch path. Bypasses ``record()``
    so this admin probe never charges anyone's quota nor pollutes the
    analytics. Use to confirm an alias dispatches end-to-end."""
    _require_admin(principal)

    if not body.model:
        raise HTTPException(400, detail="missing_field: model")

    from digitorn_gateway.llm_call import (
        check_provider_supported,
        dispatch,
    )

    supported, provider, missing_key = check_provider_supported(body.model)
    if not supported:
        raise HTTPException(
            404,
            detail={
                "code": "model_not_provided_by_digitorn",
                "category": "configuration",
                "provider": provider,
                "model": body.model,
                "missing_env_key": missing_key,
            },
        )

    t0 = time.monotonic()
    try:
        resp, usage = await asyncio.wait_for(
            dispatch(body={
                "model": body.model,
                "messages": [{"role": "user", "content": body.message}],
                "max_tokens": min(body.max_tokens, 50),
            }),
            timeout=30.0,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        # We deliberately DO NOT call get_engine().record(usage) here.
        # The admin's diagnostic call is not a real user request, so it
        # must not appear in usage stats nor inflate quota counters.
        text = ""
        try:
            text = resp["choices"][0]["message"]["content"]
        except Exception:
            pass
        return {
            "ok": True,
            "model_alias": body.model,
            "provider": usage.provider,
            "real_model": usage.model_alias,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": usage.cost_usd,
            "latency_ms": latency_ms,
            "response_preview": text[:500],
        }
    except asyncio.TimeoutError:
        raise HTTPException(504, detail="quick_chat_timeout_30s")
    except HTTPException:
        raise
    except Exception as exc:
        return {
            "ok": False,
            "model_alias": body.model,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }


# ── Refresh cache ──────────────────────────────────────────────────


@router.post("/admin/diag/refresh-cache")
async def diag_refresh_cache(
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """Reload PlanRegistry + ConfigCache from DB. Use after a manual
    DB edit to apply changes without a service restart."""
    _require_admin(principal)
    t0 = time.monotonic()
    plan_count = await get_registry().reload_plans()
    cache_stats = await get_cache().reload_from_db(get_session_factory())
    return {
        "ok": True,
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "plans_reloaded": plan_count,
        "cache": cache_stats,
    }


# ── System info ────────────────────────────────────────────────────


@router.get("/admin/diag/system")
async def diag_system(
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """Aggregate runtime info."""
    _require_admin(principal)
    cache = get_cache()
    registry = get_registry()
    uptime_s = time.monotonic() - _BOOT_TIME
    try:
        from digitorn_gateway.quota import get_engine
        engine = get_engine()
        supervisor_state = {
            "supervisor_running": engine._supervisor_task is not None
                                  and not engine._supervisor_task.done(),
            "dirty_users": len(engine._supervisor_dirty),
            "scheduled_users": len(engine._next_check_at),
            "active_blocks": len(engine._blocks),
        }
    except Exception:
        supervisor_state = {"supervisor_running": False}

    try:
        import psutil
        proc = psutil.Process(os.getpid())
        rss_mb = proc.memory_info().rss / 1024 / 1024
        cpu_pct = proc.cpu_percent(interval=0.1)
    except Exception:
        rss_mb = -1
        cpu_pct = -1

    return {
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "uptime_s": int(uptime_s),
        "worker_pid": os.getpid(),
        "rss_mb": round(rss_mb, 1) if rss_mb >= 0 else None,
        "cpu_percent": round(cpu_pct, 1) if cpu_pct >= 0 else None,
        "providers_count": len(cache._providers),  # type: ignore[attr-defined]
        "models_count": len(cache._models),  # type: ignore[attr-defined]
        "credentials_count": len(cache._credentials),  # type: ignore[attr-defined]
        "routes_count": len(cache.all_routes()),
        "plans_count": len(registry._plans),  # type: ignore[attr-defined]
        "supervisor": supervisor_state,
    }
