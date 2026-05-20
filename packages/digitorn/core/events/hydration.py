"""Reusable hydration helpers for `join_session`."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

async def compute_active_ops(
    *, app_id: str, session_id: str, user_id: str,
) -> dict[str, Any]:
    """Reconstruct the list of non-terminal operations."""
    try:
        from digitorn.core.events.envelope import TERMINAL_STATES
        from digitorn.core.events.envelope import (
            _LEGACY_OP_TYPE, _LEGACY_OP_STATE,
        )
        from digitorn.core.runtime.session_store.bridge import (
            get_default_bridge,
        )
    except Exception as exc:
        logger.debug("compute_active_ops imports failed: %s", exc)
        return {
            "app_id": app_id, "session_id": session_id,
            "active_ops": [], "count": 0, "scanned_events": 0,
            "terminal_states": [],
        }

    terminal_names = {s.value for s in TERMINAL_STATES}
    ops: dict[str, dict[str, Any]] = {}

    # Events come from the in-memory SessionStore already sorted by seq
    # via SeqAllocator (no need for ORDER BY).
    bridge = get_default_bridge()
    rows: list[Any] = []
    if bridge is not None:
        try:
            state = await bridge.store.open(
                session_id, app_id=app_id, user_id=user_id,
                create_if_missing=False, pin=False,
            )
            rows = [
                e for e in state.events
                if e.kind == "event" and (not user_id or (e.user_id or "") == user_id)
            ]
        except KeyError:
            rows = []
        except Exception as exc:
            logger.debug("compute_active_ops store read failed: %s", exc)
            rows = []

    for row in rows:
        payload = row.payload or {}
        op_id = payload.get("op_id") or row.correlation_id
        if not op_id:
            continue
        op_type = payload.get("op_type")
        op_state = payload.get("op_state")
        # Backward-compat: older rows without the explicit contract -
        # infer from type via the legacy map so historical sessions
        # still produce meaningful active_ops.
        if not op_type or not op_state:
            _ot = _LEGACY_OP_TYPE.get(row.type)
            _os = _LEGACY_OP_STATE.get(row.type)
            op_type = op_type or (_ot.value if _ot else "system")
            op_state = op_state or (_os.value if _os else "running")

        entry = ops.setdefault(op_id, {
            "op_id": op_id,
            "op_type": op_type,
            "op_state": op_state,
            "op_parent_id": payload.get("op_parent_id"),
            "first_seq": row.seq,
            "started_at": row.ts,
            "last_seq": row.seq,
            "last_ts": row.ts,
            "last_type": row.type,
            "correlation_id": row.correlation_id or None,
        })
        entry["op_state"] = op_state
        entry["last_seq"] = row.seq
        entry["last_ts"] = row.ts
        entry["last_type"] = row.type
        if payload.get("op_parent_id"):
            entry["op_parent_id"] = payload["op_parent_id"]
        # Surface anything the UI would want to render the chip
        # without having to re-query the payload.
        for hint in ("name", "tool_name", "specialist", "description"):
            if payload.get(hint) and hint not in entry:
                entry[hint] = payload[hint]

    active = [e for e in ops.values() if e["op_state"] not in terminal_names]
    active.sort(key=lambda e: (e.get("first_seq") or 0))

    return {
        "app_id": app_id,
        "session_id": session_id,
        "active_ops": active,
        "count": len(active),
        "scanned_events": len(rows),
        "terminal_states": sorted(terminal_names),
    }

async def compute_memory_snapshot(
    *, manager: Any, app_id: str, session_id: str, user_id: str,
) -> dict[str, Any] | None:
    """Return the current memory state for a session."""
    try:
        deployed = manager._get_deployed(app_id, user_id=user_id)
    except Exception:
        deployed = None
    if deployed is None:
        return None

    modules = getattr(deployed, "modules", {}) or {}
    memory_module = modules.get("memory")
    if memory_module is None:
        return None

    try:
        store = getattr(memory_module, "store", None)
        if store is None:
            return None
    except Exception:
        return None

    try:
        _ = store.episodic  # access to trigger lazy init if any
    except Exception as exc:
        logger.debug("hydration best-effort block failed: %s", exc)

    goal = ""
    todos: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []

    try:
        working = getattr(store, "working", None)
        if working is not None:
            goal = getattr(working, "goal", "") or ""
            todos = [
                {
                    "id": getattr(t, "id", ""),
                    "content": getattr(t, "content", ""),
                    "status": getattr(t, "status", "pending"),
                    "priority": getattr(t, "priority", "normal"),
                }
                for t in (getattr(working, "todos", []) or [])
            ]
    except Exception as exc:
        logger.debug("memory_snapshot: working read failed: %s", exc)

    try:
        semantic = getattr(store, "semantic", None)
        if semantic is not None:
            raw_facts = list(getattr(semantic, "facts", []) or [])
            # Keep the most recent 30 (ring-style); the client can
            # paginate for older ones via the memory HTTP route.
            for f in raw_facts[-30:]:
                if getattr(f, "superseded_by", None):
                    continue
                facts.append({
                    "id": getattr(f, "id", ""),
                    "content": getattr(f, "content", ""),
                    "category": getattr(f, "category", "") or None,
                    "importance": getattr(f, "importance", 1.0),
                })
    except Exception as exc:
        logger.debug("memory_snapshot: semantic read failed: %s", exc)

    return {
        "app_id": app_id,
        "session_id": session_id,
        "goal": goal,
        "todos": todos,
        "facts": facts,
    }

async def compute_session_snapshot(
    *, manager: Any, app_id: str, session_id: str, user_id: str,
) -> dict[str, Any]:
    """Session metadata + live usage summary for the sidebar."""
    out: dict[str, Any] = {
        "app_id": app_id,
        "session_id": session_id,
        "title": None,
        "created_at": None,
        "last_active": None,
        "message_count": 0,
        "tokens": {"prompt": 0, "completion": 0, "total": 0},
        "turn_running": False,
        "interrupted": False,
    }
    try:
        session = await manager.get_session(app_id, session_id, user_id=user_id)
    except Exception:
        session = None
    if session is None:
        return out

    out["title"] = getattr(session, "title", None)
    out["created_at"] = getattr(session, "created_at", None)
    out["last_active"] = getattr(session, "last_active", None)
    try:
        out["message_count"] = len(session.messages or [])
    except Exception as exc:
        logger.debug("hydration best-effort block failed: %s", exc)
    out["interrupted"] = bool(getattr(session, "interrupted", False))

    # Turn running? `_session_tasks` holds the active asyncio Task.
    try:
        active_key = f"{app_id}:{session_id}"
        task = manager._session_tasks.get(active_key)
        out["turn_running"] = bool(task is not None and not task.done())
    except Exception as exc:
        logger.debug("hydration best-effort block failed: %s", exc)

    # Token / cost totals are owned by the digitorn gateway, not the
    # daemon. Snapshots return 0 for these fields; clients that need
    # accurate values query the gateway directly.

    return out

async def compute_approvals_snapshot(
    *, manager: Any, app_id: str, session_id: str, user_id: str,
) -> dict[str, Any]:
    """Pending approvals visible to this user for this session."""
    pending: list[dict[str, Any]] = []
    try:
        deployed = manager._get_deployed(app_id, user_id=user_id)
    except Exception:
        deployed = None
    if deployed is None:
        return {"app_id": app_id, "session_id": session_id, "pending": [], "count": 0}
    aq = getattr(deployed, "approval_queue", None)
    if aq is None:
        return {"app_id": app_id, "session_id": session_id, "pending": [], "count": 0}

    try:
        raw = aq.list_pending_for_user(user_id) if user_id else aq.list_pending()
        for r in raw:
            if r.get("session_id") and r.get("session_id") != session_id:
                continue
            pending.append(r)
    except Exception as exc:
        logger.debug("approvals_snapshot: list_pending failed: %s", exc)

    return {
        "app_id": app_id,
        "session_id": session_id,
        "pending": pending,
        "count": len(pending),
    }
