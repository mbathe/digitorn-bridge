"""Runtime settings endpoints.

A small admin-only surface for the operational feature flags. Three
phases share this module:

  * Phase A (read-only): ``GET /admin/settings/runtime`` returns the
    current value of every flag the dashboard cares about, sourced
    from the live ``Settings`` singleton. The operator no longer has
    to SSH into the box to know whether caching is on.

  * Phase B (live override): ``PUT /admin/settings/runtime`` persists
    overrides to ``gateway_runtime_settings`` + mutates the live
    Settings instance. The dispatch path picks up the change on its
    next ``get_settings()`` call - no restart required.

  * Phase C (observability): not in this file - C extends
    ``gateway_usage_events`` and the ``/admin/usage`` aggregations.

Sensitive values (``cache_redis_url`` carrying credentials,
``database_url``, ``master_key``, ``auth_jwks_url``) are NEVER
returned over the wire. The endpoint surfaces only what the operator
can safely read in a browser.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from digitorn_gateway.auth import GatewayPrincipal, require_principal
from digitorn_gateway.config import get_settings
from digitorn_gateway.db import get_session_factory

logger = logging.getLogger(__name__)


router = APIRouter()


# Live override: the worker process keeps a single ``Settings`` instance
# in memory. The PUT endpoint mutates it in place AND persists to
# ``gateway_runtime_settings``; the loader at startup re-reads every
# row and reapplies before traffic begins.
LIVE_OVERRIDE_ENABLED = True


def _require_admin(principal: GatewayPrincipal) -> None:
    if not (principal.roles and (
        "admin" in principal.roles or "developer" in principal.roles
    )):
        raise HTTPException(403, detail="admin_role_required")


# Flags surfaced to the dashboard. Each entry: (key, group, label,
# description, type, kind). The kind drives the UI control: bool ->
# toggle, int -> number input, str -> text. ``group`` clusters fields
# in the page.
_SURFACED_FLAGS: list[dict[str, Any]] = [
    # Cache.
    {"key": "cache_enabled", "group": "cache", "kind": "bool",
     "label": "Response cache",
     "description": "Master switch. Off = zero overhead, no Redis call."},
    {"key": "cache_default_ttl_seconds", "group": "cache", "kind": "int",
     "label": "Cache TTL (s)", "min": 10, "max": 86400,
     "description": "How long every cached response stays fresh."},
    {"key": "cache_lookup_timeout_ms", "group": "cache", "kind": "int",
     "label": "Lookup timeout (ms)", "min": 1, "max": 200,
     "description": "Hard cap on the cache GET. Slow Redis = silent miss."},

    # Failover.
    {"key": "failover_enabled", "group": "failover", "kind": "bool",
     "label": "Cross-provider failover",
     "description": "Master switch. Off = single attempt, upstream error bubbles up."},
    {"key": "failover_max_attempts", "group": "failover", "kind": "int",
     "label": "Max failover attempts", "min": 1, "max": 16,
     "description": "Cap on routes the dispatch loop will try per request."},
    {"key": "failover_max_inflight_per_credential", "group": "failover", "kind": "int",
     "label": "Inflight cap per credential", "min": 0, "max": 1000,
     "description": "0 = unlimited. >0 enables load-aware spill to next route."},

    # Truncation.
    {"key": "truncate_enabled", "group": "truncate", "kind": "bool",
     "label": "Auto-truncation safety net",
     "description": "Master switch for Mode 1 (reject) + Mode 2 (head_drop on fallback)."},
    {"key": "truncate_default_max_output_tokens", "group": "truncate", "kind": "int",
     "label": "Default output reservation", "min": 1, "max": 131072,
     "description": "Reserved output budget when the request omits max_tokens."},

    # Quota.
    {"key": "quota_enabled", "group": "quota", "kind": "bool",
     "label": "Quota enforcement",
     "description": "Master switch. Off = no pre-call check, no post-call record."},
    {"key": "quota_flush_interval_seconds", "group": "quota", "kind": "int",
     "label": "Quota flush interval (s)", "min": 1, "max": 600,
     "description": "How often the in-memory counters are flushed to Postgres."},
]


# Sensitive flags surfaced WITHOUT their value (just a presence flag),
# so the operator can see "Redis URL is configured" without leaking it.
_SENSITIVE_FLAGS: list[dict[str, Any]] = [
    {"key": "cache_redis_url", "group": "cache",
     "label": "Cache Redis URL",
     "description": "Distinct DB from quota_redis_url. Empty = in-process LRU."},
    {"key": "quota_redis_url", "group": "quota",
     "label": "Quota Redis URL",
     "description": "Cross-worker quota coordination. Empty = single-process mode."},
    {"key": "database_url", "group": "core",
     "label": "Database URL",
     "description": "Async SQLAlchemy URL. Same Postgres as digitorn-auth."},
    {"key": "auth_jwks_url", "group": "core",
     "label": "Auth JWKS URL",
     "description": "Where the gateway pulls public keys for JWT verification."},
]


@router.get("/admin/settings/runtime")
async def get_runtime_settings(
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """Return the current value of every operator-tunable flag.

    Read-only in phase A: every flag still requires a gateway restart
    to take effect (env var change). Phase B will add a PUT endpoint
    that mutates the live ``Settings`` singleton.
    """
    _require_admin(principal)
    settings = get_settings()
    flags: list[dict[str, Any]] = []
    for spec in _SURFACED_FLAGS:
        value = getattr(settings, spec["key"], None)
        flags.append({**spec, "value": value, "editable": LIVE_OVERRIDE_ENABLED})
    sensitive: list[dict[str, Any]] = []
    for spec in _SENSITIVE_FLAGS:
        raw = getattr(settings, spec["key"], "") or ""
        sensitive.append({
            **spec,
            "configured": bool(raw),
            # Hint at the host so operators can confirm without leaking
            # the credential / path.
            "preview": _safe_preview(spec["key"], raw),
        })
    return {
        "flags": flags,
        "sensitive": sensitive,
        "live_override_enabled": LIVE_OVERRIDE_ENABLED,
    }


_KEY_INDEX: dict[str, dict[str, Any]] = {s["key"]: s for s in _SURFACED_FLAGS}


class RuntimeSettingUpdate(BaseModel):
    key: str
    value: bool | int | float | str | None


def _coerce_for_field(spec: dict[str, Any], raw: Any) -> Any:
    """Normalise the JSON payload into the type expected by the
    Pydantic Settings field. The PUT endpoint accepts ``true``/``"true"``,
    ``42``/``"42"``, etc, the way curl users tend to send them.
    Any range constraint declared on the spec (``min``/``max``) is
    enforced here so the dashboard cannot push a value the model would
    reject silently."""
    kind = spec["kind"]
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        if isinstance(raw, str):
            v = raw.strip().lower()
            if v in {"true", "1", "yes", "on"}:
                return True
            if v in {"false", "0", "no", "off"}:
                return False
        raise HTTPException(400, detail=f"value for {spec['key']} must be boolean")
    if kind == "int":
        try:
            n = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, detail=f"value for {spec['key']} must be int")
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and n < lo:
            raise HTTPException(400, detail=f"{spec['key']} must be >= {lo}")
        if hi is not None and n > hi:
            raise HTTPException(400, detail=f"{spec['key']} must be <= {hi}")
        return n
    if kind == "str":
        if raw is None:
            return ""
        return str(raw)
    return raw


@router.put("/admin/settings/runtime")
async def update_runtime_setting(
    body: RuntimeSettingUpdate,
    principal: GatewayPrincipal = Depends(require_principal),
) -> dict[str, Any]:
    """Persist a flag override to the DB AND mutate the live Settings
    singleton. The change takes effect on the next ``get_settings()``
    call - typically the very next request."""
    _require_admin(principal)
    spec = _KEY_INDEX.get(body.key)
    if spec is None:
        raise HTTPException(
            404,
            detail=f"unknown setting '{body.key}' "
                   f"(allowed: {sorted(_KEY_INDEX.keys())})",
        )
    value = _coerce_for_field(spec, body.value)

    # Mutate the live singleton FIRST. If the field is missing or the
    # field's own validator rejects, we surface a clear 400 BEFORE we
    # persist - the DB and the running process never disagree.
    settings = get_settings()
    if not hasattr(settings, body.key):
        raise HTTPException(500, detail=f"settings has no field '{body.key}'")
    try:
        setattr(settings, body.key, value)
    except Exception as exc:
        raise HTTPException(
            400, detail=f"settings rejected value: {exc}",
        ) from exc

    # Side-effects: features that need an init/reconfigure when their
    # flag changes (e.g. cache cap) get poked here so the change takes
    # effect without a restart.
    _apply_side_effects(body.key, value)

    # Persist. UPSERT so subsequent toggles overwrite the same row.
    try:
        factory = get_session_factory()
        async with factory() as db:
            await db.execute(
                text("""
                    INSERT INTO gateway_runtime_settings (key, value, updated_by)
                    VALUES (:k, CAST(:v AS JSONB), :who)
                    ON CONFLICT (key) DO UPDATE
                      SET value = EXCLUDED.value,
                          updated_at = now(),
                          updated_by = EXCLUDED.updated_by
                """),
                {
                    "k": body.key,
                    "v": json.dumps(value),
                    "who": principal.user_id or "admin",
                },
            )
            await db.commit()
    except Exception as exc:
        # The in-memory mutation already happened; if the DB write
        # fails, log loudly so the operator knows the change won't
        # survive a restart, but don't roll the in-memory mutation
        # back - that would leave the running process in a different
        # state from what the caller asked for.
        logger.error(
            "runtime_setting_persist_failed key=%s value=%s err=%s",
            body.key, value, exc,
        )
        raise HTTPException(
            500,
            detail=f"in-memory updated but DB persist failed: {exc}",
        ) from exc

    logger.info(
        "runtime_setting_updated key=%s value=%s by=%s",
        body.key, value, principal.user_id,
    )

    # Cross-worker propagation: every other worker in the cluster
    # listens on this channel and re-pulls the row + reapplies via
    # ``apply_runtime_override``. The local worker has already
    # mutated the singleton above, so it doesn't need the notify -
    # but receiving its own NOTIFY is harmless (idempotent reload).
    try:
        from digitorn_gateway.cluster_sync import (
            notify as _notify, CHANNEL_RUNTIME_SETTINGS,
        )
        await _notify(CHANNEL_RUNTIME_SETTINGS, body.key)
    except Exception as exc:
        logger.debug("runtime_setting_notify_failed: %s", exc)

    return {"ok": True, "key": body.key, "value": value}


def _apply_side_effects(key: str, value: Any) -> None:
    """Re-wire subsystems when their feature flag changes.

    Some flags only take effect via cached state somewhere else (e.g.
    the inflight cap lives on ``ConfigCache``). The PUT endpoint pokes
    those caches here so the next request sees the new value without
    a gateway restart.
    """
    try:
        if key == "failover_max_inflight_per_credential":
            from digitorn_gateway.config_cache import get_cache as _gc
            _gc().set_inflight_cap(int(value or 0))
        elif key == "cache_redis_url":
            # Re-init the cache backend with the new URL. Safe to call
            # even when cache_enabled=False.
            from digitorn_gateway.response_cache import init_cache as _ic
            _ic(str(value or ""))
    except Exception as exc:
        logger.warning(
            "runtime_setting_side_effect_failed key=%s err=%s", key, exc,
        )


async def apply_one_override(key: str) -> None:
    """Reload one row from ``gateway_runtime_settings`` and apply it
    to the live Settings singleton + side-effects. Called by the
    cross-worker listener when a peer updates a flag.

    Empty key (broadcast) reloads everything. Unknown key is logged
    and ignored. Missing row removes the override (resets to env
    default, or rather: the singleton keeps its current value since
    we can't know what the env default was without reconstructing
    Settings - acceptable, the operator clears overrides explicitly
    by setting them to the env default).
    """
    if not key:
        await load_runtime_overrides()
        return
    spec = _KEY_INDEX.get(key)
    if spec is None:
        logger.debug("runtime_settings: ignored notify for unknown key=%s", key)
        return
    try:
        factory = get_session_factory()
        async with factory() as db:
            row = (await db.execute(
                text(
                    "SELECT value FROM gateway_runtime_settings WHERE key = :k"
                ),
                {"k": key},
            )).first()
            if row is None:
                logger.debug(
                    "runtime_settings: notify for key=%s but no row", key,
                )
                return
            value = _coerce_for_field(spec, row[0])
            settings = get_settings()
            setattr(settings, key, value)
            _apply_side_effects(key, value)
            logger.info(
                "runtime_setting_applied_from_peer key=%s value=%s",
                key, value,
            )
    except Exception as exc:
        logger.warning(
            "runtime_setting_peer_apply_failed key=%s err=%s", key, exc,
        )


async def load_runtime_overrides() -> dict[str, Any]:
    """Read every persisted override and apply it to the live Settings.

    Called from ``main.lifespan`` BEFORE the gateway starts accepting
    traffic so the first request already sees the operator's intent.
    Failures are logged - if we can't read the table the gateway falls
    back to its env-var defaults, which is always safe.
    """
    settings = get_settings()
    applied: dict[str, Any] = {}
    try:
        factory = get_session_factory()
        async with factory() as db:
            rows = (await db.execute(
                text("SELECT key, value FROM gateway_runtime_settings")
            )).all()
            for r in rows:
                k = r[0]
                v = r[1]
                spec = _KEY_INDEX.get(k)
                if spec is None:
                    logger.warning(
                        "runtime_settings: skipping unknown key %s", k,
                    )
                    continue
                try:
                    coerced = _coerce_for_field(spec, v)
                    setattr(settings, k, coerced)
                    _apply_side_effects(k, coerced)
                    applied[k] = coerced
                except Exception as exc:
                    logger.warning(
                        "runtime_settings: reject key=%s value=%s err=%s",
                        k, v, exc,
                    )
    except Exception as exc:
        logger.warning(
            "runtime_settings_load_failed (%s); using env defaults", exc,
        )
    if applied:
        logger.info(
            "runtime_settings_applied: %s",
            ", ".join(f"{k}={v}" for k, v in applied.items()),
        )
    return applied


def _safe_preview(key: str, raw: str) -> str:
    """Return a redacted hint for sensitive values. Never the secret
    itself - only what's safe to render in a browser tab.

      * URLs: scheme + host (no userinfo, no path beyond /).
      * Anything else with length: ``****`` + last 4 chars.
    """
    if not raw:
        return ""
    if "://" in raw:
        try:
            from urllib.parse import urlsplit
            u = urlsplit(raw)
            host = u.hostname or ""
            return f"{u.scheme}://{host}"
        except Exception:
            return "****"
    if len(raw) <= 4:
        return "****"
    return f"****{raw[-4:]}"
