"""Per-user, per-app BYOK toggle store (LOCAL mode only)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

logger = logging.getLogger(__name__)

_BYOK_REF_PREFIX = "byok_"


def _byok_ref_for_provider(provider: str) -> dict[str, str]:
    """Build the synthetic `credential:` ref dict for a brain."""
    return {
        "ref": f"{_BYOK_REF_PREFIX}{provider.lower()}",
        "scope": "per_app_per_user",
        "provider": provider.lower(),
    }


async def _get_byok_inner(user_id: str, app_id: str) -> dict[str, Any] | None:
    """Inner coroutine: runs on the persist_worker loop where"""
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import UserAppByok
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(UserAppByok, (user_id, app_id))
        if row is None:
            return None
        return {
            "user_id": row.user_id,
            "app_id": row.app_id,
            "enabled": bool(row.enabled),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }


async def get_byok(user_id: str, app_id: str) -> dict[str, Any] | None:
    """Return the BYOK row for (user, app), or `None` when missing."""
    if not user_id or not app_id:
        return None
    try:
        from digitorn.core.runtime.persist_worker import get_default_worker
        return await get_default_worker().run_async(
            _get_byok_inner, user_id, app_id,
        )
    except Exception as exc:
        logger.warning(
            "byok_get_routed_failed user=%s app=%s: %s",
            user_id, app_id, exc,
        )
        return None


async def is_byok_enabled(user_id: str, app_id: str) -> bool:
    """Cheap boolean lookup"""
    try:
        row = await get_byok(user_id, app_id)
    except Exception as exc:
        logger.warning("byok_lookup_failed user=%s app=%s: %s", user_id, app_id, exc)
        return False
    return bool(row and row.get("enabled"))


async def _set_byok_inner(
    user_id: str, app_id: str, enabled: bool,
) -> dict[str, Any]:
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import UserAppByok
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(UserAppByok, (user_id, app_id))
        if row is None:
            row = UserAppByok(
                user_id=user_id, app_id=app_id, enabled=enabled,
            )
            db.add(row)
        else:
            row.enabled = enabled
        await db.commit()
        await db.refresh(row)
        return {
            "user_id": row.user_id,
            "app_id": row.app_id,
            "enabled": bool(row.enabled),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }


async def set_byok(
    *, user_id: str, app_id: str, enabled: bool,
) -> dict[str, Any]:
    """Upsert the BYOK toggle"""
    if not user_id or not app_id:
        raise ValueError("user_id and app_id are required")
    from digitorn.core.runtime.persist_worker import get_default_worker
    result = await get_default_worker().run_async(
        _set_byok_inner, user_id, app_id, enabled,
    )
    try:
        from digitorn.core.api.apps_v2._dispatch import invalidate_byok_cache
        invalidate_byok_cache(user_id=user_id, app_id=app_id)
    except Exception:
        pass
    return result


async def _list_byok_inner(user_id: str) -> list[dict[str, Any]]:
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import UserAppByok
    factory = get_session_factory()
    async with factory() as db:
        result = await db.execute(
            select(UserAppByok).where(UserAppByok.user_id == user_id)
        )
        rows = result.scalars().all()
        return [
            {
                "user_id": r.user_id,
                "app_id": r.app_id,
                "enabled": bool(r.enabled),
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]


async def list_byok_for_user(user_id: str) -> list[dict[str, Any]]:
    """Return every BYOK row for the user (for the Settings screen)."""
    if not user_id:
        return []
    try:
        from digitorn.core.runtime.persist_worker import get_default_worker
        return await get_default_worker().run_async(
            _list_byok_inner, user_id,
        )
    except Exception as exc:
        logger.warning(
            "byok_list_routed_failed user=%s: %s", user_id, exc,
        )
        return []


async def build_byok_overrides_for_app(
    *,
    compiled: Any,
    user_id: str,
) -> dict[str, dict[str, str]]:
    """Compute the `{agent_id: ref_dict}` override map for one turn."""
    overrides: dict[str, dict[str, str]] = {}
    if compiled is None or not user_id:
        return overrides

    # Cloud mode: BYOK is never honored - all traffic is gateway-only.
    try:
        from digitorn.core.config import get_settings
        if get_settings().mode == "cloud":
            return overrides
    except Exception as exc:
        logger.debug("byok_store best-effort block failed: %s", exc)

    # Anonymous / pseudo users have no per-user credentials.
    norm = user_id.strip().lower()
    if norm in {"", "local", "anonymous", "system", "admin"}:
        return overrides

    app_id = getattr(compiled, "app_id", "") or ""
    if not app_id:
        return overrides

    if not await is_byok_enabled(norm or user_id, app_id):
        return overrides

    for agent in getattr(compiled, "agents", []) or []:
        agent_id = getattr(agent, "agent_id", "") or "agent"
        brain = getattr(agent, "brain", None)
        if brain is None:
            continue
        if getattr(brain, "credential", None) is not None:
            continue
        provider = _provider_name_from_brain(brain)
        if not provider:
            continue
        overrides[agent_id] = _byok_ref_for_provider(provider)

    behavior = getattr(compiled, "behavior", None)
    if behavior is not None:
        bbrain = getattr(behavior, "brain", None)
        if bbrain is not None and getattr(bbrain, "credential", None) is None:
            provider = _provider_name_from_brain(bbrain)
            if provider:
                overrides["behavior"] = _byok_ref_for_provider(provider)

    return overrides


def _provider_name_from_brain(brain: Any) -> str:
    """Extract the canonical provider name (deepseek, anthropic, …)"""
    direct = (getattr(brain, "provider", "") or "").strip().lower()
    if direct:
        return direct
    inline = getattr(brain, "inline_config", None)
    if isinstance(inline, dict):
        for k in ("provider", "provider_hint"):
            v = inline.get(k)
            if v:
                return str(v).strip().lower()
    return (getattr(brain, "provider_id", "") or "").strip().lower()
