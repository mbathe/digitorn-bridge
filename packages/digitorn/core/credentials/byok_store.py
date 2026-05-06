"""Per-user, per-app BYOK toggle store (LOCAL mode only).

Reads / writes the ``user_app_byok`` table. Three operations:

  * :func:`is_byok_enabled` - quick boolean lookup, swallows DB errors
    by returning ``False`` (the gateway path is the safe default).
  * :func:`get_byok` - returns the row dict, or ``None`` when absent.
  * :func:`set_byok` - upserts the toggle, returns the resulting state.

The store also exposes :func:`build_byok_overrides_for_app`, which is
called once per turn at the dispatch layer. Given a compiled app and
the current user, it returns a mapping ``{agent_id: ref_dict}`` that
the credential injector treats as a synthetic ``brain.credential``
block. The mapping is empty (no-op) when:

  * the daemon is in cloud mode (BYOK is meaningless there),
  * the user has no row in ``user_app_byok`` for this app,
  * or the row's ``enabled`` flag is False.

The override is **never persisted** anywhere - it lives only for the
duration of the turn, in the dict passed alongside the call. This
keeps the shared ``CompiledApp`` object untouched (no cross-user
mutation race).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

logger = logging.getLogger(__name__)

# A reserved slug convention. When BYOK is on for app X and brain
# provider P, the synthetic ref points at ``byok_<provider>`` with
# scope ``per_app_per_user``. The credential picker will fill that
# exact slug when the user submits the form.
_BYOK_REF_PREFIX = "byok_"


def _byok_ref_for_provider(provider: str) -> dict[str, str]:
    """Build the synthetic ``credential:`` ref dict for a brain.

    Returns a dict in the same shape ``parse_credential_ref`` accepts
    (``ref`` + ``scope``). The provider name is also included as a
    lock so the picker resolves to the matching template.
    """
    return {
        "ref": f"{_BYOK_REF_PREFIX}{provider.lower()}",
        "scope": "per_app_per_user",
        "provider": provider.lower(),
    }


# ── Single-row helpers ────────────────────────────────────────────


async def get_byok(user_id: str, app_id: str) -> dict[str, Any] | None:
    """Return the BYOK row for (user, app), or ``None`` when missing."""
    if not user_id or not app_id:
        return None
    try:
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import UserAppByok
    except Exception:
        return None
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


async def is_byok_enabled(user_id: str, app_id: str) -> bool:
    """Cheap boolean lookup. Returns ``False`` on any error.

    Used on the hot path - we deliberately fall back to the safe
    gateway-routed default when the DB is unreachable.
    """
    try:
        row = await get_byok(user_id, app_id)
    except Exception as exc:
        # Promoted from debug to warning: a DB hiccup here silently
        # routes the user's traffic through the gateway (billing the
        # Digitorn account) even when they had BYOK on. We keep the
        # safe-default behavior (don't block the request), but make
        # sure ops sees the failure.
        logger.warning("byok_lookup_failed user=%s app=%s: %s", user_id, app_id, exc)
        return False
    return bool(row and row.get("enabled"))


async def set_byok(
    *, user_id: str, app_id: str, enabled: bool,
) -> dict[str, Any]:
    """Upsert the BYOK toggle. Returns the resulting row as a dict."""
    if not user_id or not app_id:
        raise ValueError("user_id and app_id are required")
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import UserAppByok

    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(UserAppByok, (user_id, app_id))
        if row is None:
            row = UserAppByok(user_id=user_id, app_id=app_id, enabled=enabled)
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


async def list_byok_for_user(user_id: str) -> list[dict[str, Any]]:
    """Return every BYOK row for the user (for the Settings screen)."""
    if not user_id:
        return []
    try:
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import UserAppByok
    except Exception:
        return []
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


# ── Per-turn override builder ─────────────────────────────────────


async def build_byok_overrides_for_app(
    *,
    compiled: Any,
    user_id: str,
) -> dict[str, dict[str, str]]:
    """Compute the ``{agent_id: ref_dict}`` override map for one turn.

    Returns ``{}`` (no-op) when:

      * daemon is in cloud mode,
      * user is anonymous / system / local pseudo-id,
      * no row exists for (user, app), or row is disabled,
      * none of the brains have a known provider.

    Otherwise returns a synthetic ``credential:`` ref for every agent
    brain that does NOT already declare its own credential block.
    The behavior brain (when present) is keyed under the literal
    ``"behavior"``.
    """
    overrides: dict[str, dict[str, str]] = {}
    if compiled is None or not user_id:
        return overrides

    # Cloud mode: BYOK is never honored - all traffic is gateway-only.
    try:
        from digitorn.core.config import get_settings
        if get_settings().mode == "cloud":
            return overrides
    except Exception:
        pass

    # Anonymous / pseudo users have no per-user credentials.
    norm = user_id.strip().lower()
    if norm in {"", "local", "anonymous", "system", "admin"}:
        return overrides

    app_id = getattr(compiled, "app_id", "") or ""
    if not app_id:
        return overrides

    if not await is_byok_enabled(norm or user_id, app_id):
        return overrides

    # Brains without a YAML-declared credential get a synthetic one.
    # Brains that already declare ``credential:`` are left alone -
    # the YAML wins, exactly like the user-managed mode.
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
    """Extract the canonical provider name (deepseek, anthropic, …)
    from any brain shape - raw ``AgentBrain`` (has ``provider``
    directly) or ``CompiledBrain`` (provider is inside
    ``inline_config`` for inline brains, or implied by
    ``provider_id`` for named brains).
    """
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
