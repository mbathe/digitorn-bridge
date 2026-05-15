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

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select, text
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


@router.post("/admin/diag/routes/{route_id}/test")
async def diag_test_route(
    route_id: str,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Test ONE route end-to-end with a real tiny LLM call.

    Unlike ``/probe`` (which is the light-touch background health
    pulse), this runs the FULL dispatch path with every
    provider-specific kwarg (AWS SigV4, Vertex service-account,
    Azure deployment, Copilot OAuth headers, responses-only models)
    and returns rich diagnostic info: latency, http status when
    available, classified error_class, hint, real model id used,
    content preview.

    Never records usage. Never charges quota. Never mutates route
    health (unlike /probe which marks success/failure).

    Use case: dashboard "Test this route" button per row in
    /routing - especially valuable for non-OpenAI providers
    (Bedrock, Vertex, Azure, Copilot) where credential shape is
    complex and OpenAI's smoke test wouldn't catch the bug."""
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
            "ok": False,
            "route_id": route_id,
            "model_alias": route.model_alias,
            "error_class": "resolve_failed",
            "error": "could not resolve dispatch from this route id",
            "latency_ms": 0,
        }

    from digitorn_gateway.route_tester import test_one_route
    result = await test_one_route(rd)
    result["model_alias"] = route.model_alias
    result["credential_id"] = (
        str(route.credential_id) if route.credential_id else None
    )
    return result


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
    # New optional fields (backward-compat: existing UI calls keep working)
    messages: list[dict[str, Any]] | None = None  # raw messages array overrides `message`
    temperature: float | None = None
    tools: list[dict[str, Any]] | None = None
    # Pin a specific route_id: skips priority resolution + disables failover.
    # Operator can test ANY route (priority 0 / 1 / fallback) end-to-end.
    route_id: str | None = None


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
        # When a route is pinned, override the alias with the route's
        # model_alias so dispatch internals stay consistent.
        effective_model = body.model
        if body.route_id:
            cache = get_cache()
            target = next(
                (r for r in cache.all_routes() if str(r.id) == str(body.route_id)),
                None,
            )
            if target is None:
                raise HTTPException(404, detail=f"route_not_found: {body.route_id}")
            effective_model = target.model_alias

        msgs = body.messages or [{"role": "user", "content": body.message}]
        dispatch_body: dict[str, Any] = {
            "model": effective_model,
            "messages": msgs,
            "max_tokens": min(body.max_tokens, 4000),
        }
        if body.temperature is not None:
            dispatch_body["temperature"] = body.temperature
        if body.tools:
            dispatch_body["tools"] = body.tools

        from digitorn_gateway.llm_call import DispatchTrace
        trace = DispatchTrace()
        resp, usage = await asyncio.wait_for(
            dispatch(
                body=dispatch_body,
                trace=trace,
                record_health=False,
                pin_route_id=body.route_id,
            ),
            timeout=30.0,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        # We deliberately DO NOT call get_engine().record(usage) here.
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
            "response_preview": text[:4000],
            "served_by": trace.served_by,
            "attempts": trace.attempts,
            "route_id": str(trace.route_id) if trace.route_id else None,
            "failover_trail": trace.trail,
            "raw_response": resp,
        }
    except asyncio.TimeoutError:
        raise HTTPException(504, detail="quick_chat_timeout_30s")
    except HTTPException:
        raise
    except Exception as exc:
        from digitorn_gateway.llm_call import classify_upstream_error
        try:
            http_status, error_class, hint = classify_upstream_error(exc)
        except Exception:
            http_status, error_class, hint = 0, "unknown", str(exc)
        return {
            "ok": False,
            "model_alias": body.model,
            "error": f"{type(exc).__name__}: {exc}",
            "error_class": error_class,
            "error_hint": hint,
            "upstream_status": http_status,
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


# ── Catalog cache-pricing backfill ─────────────────────────────────


@router.post("/admin/diag/seed-cache-pricing")
async def diag_seed_cache_pricing(
    overwrite: bool = False,
    principal: GatewayPrincipal = Depends(require_principal),
    db: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """Backfill ``cost_per_1k_cache_read_tokens`` and
    ``cost_per_1k_cache_write_tokens`` from LiteLLM's static catalog
    (``litellm.model_cost``). Operators run this once after migration
    0017 to populate the new columns for every catalog row whose
    ``real_model_id`` LiteLLM knows about.

    Default behaviour is safe (``overwrite=false``): rows where the
    operator has ALREADY set a non-zero cache price are left alone.
    Pass ``?overwrite=true`` to force re-sync with LiteLLM's table.

    Returns the count of rows updated + skipped + missing.
    """
    _require_admin(principal)
    try:
        import litellm
    except Exception as exc:
        raise HTTPException(500, detail=f"litellm not available: {exc}")
    catalog = getattr(litellm, "model_cost", None) or {}
    if not catalog:
        return {"updated": 0, "skipped": 0, "missing": 0,
                "note": "litellm.model_cost empty"}

    rows = (await db.execute(
        text("""
            SELECT alias, real_model_id, provider_slug,
                   cost_per_1k_cache_read_tokens,
                   cost_per_1k_cache_write_tokens
            FROM gateway_models
            WHERE archived_at IS NULL
        """),
    )).mappings().all()

    updated = 0
    skipped = 0
    missing = 0
    details: list[dict[str, Any]] = []

    for r in rows:
        # Try several lookup shapes since LiteLLM keys vary by provider.
        candidates = [
            r["real_model_id"],
            f"{r['provider_slug']}/{r['real_model_id']}",
        ]
        info = None
        for c in candidates:
            if c in catalog:
                info = catalog[c]
                break
        if info is None:
            for k, v in catalog.items():
                if k == r["real_model_id"] or k.endswith("/" + r["real_model_id"]):
                    info = v
                    break
        if info is None:
            missing += 1
            details.append({
                "alias": r["alias"], "status": "missing_from_litellm_catalog",
            })
            continue

        # per-token in LiteLLM. Convert to per-1k.
        cache_read_per_tok = info.get("cache_read_input_token_cost")
        cache_write_per_tok = info.get("cache_creation_input_token_cost")
        new_read = float(cache_read_per_tok) * 1000.0 if cache_read_per_tok else 0.0
        new_write = float(cache_write_per_tok) * 1000.0 if cache_write_per_tok else 0.0

        # Skip if both unknown.
        if new_read == 0.0 and new_write == 0.0:
            skipped += 1
            details.append({"alias": r["alias"], "status": "no_cache_pricing_in_catalog"})
            continue

        # Skip if operator already set, unless overwrite=true.
        cur_read = float(r["cost_per_1k_cache_read_tokens"] or 0)
        cur_write = float(r["cost_per_1k_cache_write_tokens"] or 0)
        if not overwrite and (cur_read > 0 or cur_write > 0):
            skipped += 1
            details.append({"alias": r["alias"], "status": "skipped_already_set"})
            continue

        await db.execute(
            text("""
                UPDATE gateway_models
                SET cost_per_1k_cache_read_tokens = :r,
                    cost_per_1k_cache_write_tokens = :w
                WHERE alias = :a
            """),
            {"r": new_read, "w": new_write, "a": r["alias"]},
        )
        updated += 1
        details.append({
            "alias": r["alias"], "status": "updated",
            "cache_read_per_1k": round(new_read, 8),
            "cache_write_per_1k": round(new_write, 8),
        })

    await db.commit()
    # Reload cache so the dispatch picks up the new prices.
    await get_cache().reload_from_db(get_session_factory())

    return {
        "updated": updated,
        "skipped": skipped,
        "missing": missing,
        "details": details,
    }


# ── Quick transcribe (bypass quota recording + circuit breaker) ────


@router.post("/admin/diag/quick-transcribe")
async def diag_quick_transcribe(
    request: Request,
    principal: GatewayPrincipal = Depends(require_principal),
    file: UploadFile = File(...),
    model: str = Form(...),
    language: str | None = Form(None),
    prompt: str | None = Form(None),
    route_id: str | None = Form(None),
) -> dict[str, Any]:
    """Send a tiny audio blob through the transcription dispatch path.
    Same diagnostic contract as quick-chat: bypasses ``record()`` and
    ``record_health=False`` so the operator's probe never pollutes the
    circuit breaker nor the usage stats. ``route_id`` (optional) pins
    the dispatch to one specific route, disabling failover."""
    _require_admin(principal)

    if not model.strip():
        raise HTTPException(400, detail="missing_field: model")

    from digitorn_gateway.audio_dispatch import (
        AudioDispatchTrace,
        AudioModelNotConfigured,
        dispatch_transcription,
    )
    from digitorn_gateway.llm_call import classify_upstream_error

    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(400, detail="empty_audio_file")
    if len(audio_bytes) > 25 * 1024 * 1024:
        raise HTTPException(413, detail="audio_too_large_diag_cap_25mb")

    # If a route is pinned, override the alias with that route's
    # model_alias to keep dispatch internals consistent.
    effective_model = model.strip()
    if route_id:
        cache = get_cache()
        target = next(
            (r for r in cache.all_routes() if str(r.id) == str(route_id)),
            None,
        )
        if target is None:
            raise HTTPException(404, detail=f"route_not_found: {route_id}")
        effective_model = target.model_alias

    t0 = time.monotonic()
    trace = AudioDispatchTrace()
    try:
        resp, telemetry = await asyncio.wait_for(
            dispatch_transcription(
                audio_bytes=audio_bytes,
                filename=file.filename or "audio.webm",
                alias=effective_model,
                language=language,
                prompt=prompt,
                response_format="verbose_json",
                trace=trace,
                record_health=False,
                pin_route_id=route_id,
            ),
            timeout=60.0,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "ok": True,
            "model_alias": model,
            "latency_ms": latency_ms,
            "text": (resp.get("text") or "")[:4000],
            "duration_seconds": resp.get("duration"),
            "served_by": trace.served_by,
            "attempts": trace.attempts,
            "route_id": str(trace.route_id) if trace.route_id else None,
            "failover_trail": trace.trail,
            "raw_response": resp,
        }
    except AudioModelNotConfigured as exc:
        return {
            "ok": False,
            "model_alias": model,
            "error": str(exc),
            "error_class": "audio_model_not_configured",
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }
    except asyncio.TimeoutError:
        raise HTTPException(504, detail="quick_transcribe_timeout_60s")
    except Exception as exc:
        try:
            http_status, error_class, hint = classify_upstream_error(exc)
        except Exception:
            http_status, error_class, hint = 0, "unknown", str(exc)
        return {
            "ok": False,
            "model_alias": model,
            "error": f"{type(exc).__name__}: {exc}",
            "error_class": error_class,
            "error_hint": hint,
            "upstream_status": http_status,
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }


# ── Preview model (dry-run a provider+real_model_id combo) ─────────


class PreviewModelRequest(BaseModel):
    """Body for ``/admin/diag/providers/{slug}/preview-model``.

    Used by the Models form's Test button: before persisting a new
    model alias in the catalog, verify that the (provider, real_model_id)
    combo actually answers. Uses the provider's default credential
    (``provider_default_cred[slug]``) and never touches the catalog
    or the circuit breaker."""
    real_model_id: str
    is_custom: bool = False
    message: str = "ping"


@router.post("/admin/diag/providers/{slug}/preview-model")
async def diag_preview_model(
    slug: str,
    body: PreviewModelRequest,
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """Dry-run a (provider, real_model_id) combo BEFORE adding the alias
    to the catalog. Reuses the provider's default credential. No
    persistence, no quota recording, no circuit-breaker pollution."""
    _require_admin(principal)

    if not body.real_model_id.strip():
        raise HTTPException(400, detail="missing_field: real_model_id")

    cache = get_cache()
    provider = cache._providers.get(slug)  # type: ignore[attr-defined]
    if provider is None:
        raise HTTPException(404, detail=f"provider_not_found: {slug}")

    cred_id = cache._provider_default_cred.get(slug)  # type: ignore[attr-defined]
    if cred_id is None:
        return {
            "ok": False,
            "error_class": "no_active_credential",
            "error": f"No active credential for provider '{slug}'. Add one in Credentials first.",
            "latency_ms": 0,
        }
    cred = cache._credentials.get(cred_id)  # type: ignore[attr-defined]
    if cred is None:
        return {
            "ok": False,
            "error_class": "credential_unreadable",
            "error": (
                f"Default credential for '{slug}' is in DB but not decryptable. "
                "Likely master key mismatch — recreate the credential."
            ),
            "latency_ms": 0,
        }

    # Build the LiteLLM call manually (alias doesn't exist in catalog yet,
    # so resolve_dispatch can't help). Same kwargs shape as dispatch().
    from digitorn_gateway.auth_dispatchers import dispatch_auth
    from digitorn_gateway.llm_call import (
        _litellm_model_from_compat,
        classify_upstream_error,
    )
    auth = dispatch_auth(provider.auth_type, cred.secret_data or {})
    provider_headers = cache._provider_dispatch_headers(provider)  # type: ignore[attr-defined]

    if body.is_custom:
        return {
            "ok": False,
            "error_class": "preview_not_supported_for_custom",
            "error": (
                "is_custom models route via CustomRouter which has its own "
                "registry. Preview is only supported for standard LiteLLM dispatch."
            ),
            "latency_ms": 0,
        }

    litellm_model = _litellm_model_from_compat(
        provider.compat, slug, body.real_model_id.strip(),
    )
    kwargs: dict[str, Any] = {
        "model": litellm_model,
        "messages": [{"role": "user", "content": body.message}],
        "max_tokens": 30,
        "temperature": 0,
    }
    if auth.api_key:
        kwargs["api_key"] = auth.api_key
    if provider.base_url:
        kwargs["api_base"] = provider.base_url
    merged_headers = {**provider_headers, **auth.extra_headers}
    if merged_headers:
        kwargs["extra_headers"] = merged_headers

    t0 = time.monotonic()
    try:
        import litellm
        resp = await asyncio.wait_for(litellm.acompletion(**kwargs), timeout=30.0)
        latency_ms = int((time.monotonic() - t0) * 1000)
        d = resp.model_dump() if hasattr(resp, "model_dump") else (
            resp.dict() if hasattr(resp, "dict") else dict(resp)
        )
        text = ""
        try:
            text = d["choices"][0]["message"]["content"]
        except Exception:
            pass
        return {
            "ok": True,
            "provider": slug,
            "real_model_id": body.real_model_id.strip(),
            "credential_label": cred.label,
            "latency_ms": latency_ms,
            "response_preview": text[:500],
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error_class": "timeout",
            "error": "Upstream did not respond within 30s",
            "latency_ms": 30000,
        }
    except Exception as exc:
        try:
            http_status, error_class, hint = classify_upstream_error(exc)
        except Exception:
            http_status, error_class, hint = 0, "unknown", str(exc)
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "error_class": error_class,
            "error_hint": hint,
            "upstream_status": http_status,
            "latency_ms": int((time.monotonic() - t0) * 1000),
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
