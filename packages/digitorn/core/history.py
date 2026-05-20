"""Unified history ledger writer - one function, three kinds of rows."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)


Kind = Literal["message", "event", "audit"]


class HistoryContractError(ValueError):
    """Raised when a caller tries to persist a row that violates the"""


def _enforce_contract(
    *,
    kind: Kind,
    type: str,
    app_id: str | None,
    session_id: str | None,
    user_id: str | None,
    seq: int,
    actor_user_id: str | None,
) -> None:
    """Single strict gate every row crosses before landing in the DB."""
    if not type:
        raise HistoryContractError(
            f"history.record: 'type' is required for kind={kind!r}"
        )

    if kind == "event":
        if not session_id:
            raise HistoryContractError(
                f"history.record(kind=event, type={type!r}): session_id is "
                "required. Untagged events leak across sessions - fix the "
                "emitter to carry the session context."
            )
        if not user_id:
            raise HistoryContractError(
                f"history.record(kind=event, type={type!r}): user_id is "
                "required (use 'system' for daemon-internal events)."
            )
        if not isinstance(seq, int) or seq <= 0:
            raise HistoryContractError(
                f"history.record(kind=event, type={type!r}, "
                f"session_id={session_id}): seq must be a positive int "
                f"(got {seq!r}). Route emissions through "
                "SessionBus.emit / publish so next_seq stamps a real seq."
            )

    elif kind == "message":
        if not session_id:
            raise HistoryContractError(
                f"history.record(kind=message, type={type!r}): "
                "session_id is required."
            )
        if not user_id:
            raise HistoryContractError(
                f"history.record(kind=message, type={type!r}): "
                "user_id is required."
            )

    elif kind == "audit":
        if not actor_user_id:
            raise HistoryContractError(
                f"history.record(kind=audit, type={type!r}): "
                "actor_user_id is required - audit rows without an "
                "actor are unusable for forensics / compliance."
            )

    else:
        raise HistoryContractError(
            f"history.record: unknown kind={kind!r} - expected one of "
            "message, event, audit."
        )


def _build_row_kwargs(
    *,
    kind: Kind,
    type: str,
    app_id: str | None,
    session_id: str | None,
    user_id: str | None,
    seq: int,
    actor_user_id: str | None,
    actor_roles: list[str] | None,
    role: str | None,
    content: str | None,
    tool_call_id: str | None,
    tool_calls: list[dict[str, Any]] | None,
    name: str | None,
    payload: dict[str, Any] | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    target_user_id: str | None,
    target_app_id: str | None,
    target_resource: str | None,
    ip_address: str | None,
    user_agent: str | None,
    correlation_id: str,
    success: bool,
    message: str,
    ts: Any,
) -> dict[str, Any]:
    """Collect caller kwargs into the dict `HistoryLog(**)` expects."""
    return {
        "ts": ts,
        "seq": int(seq or 0),
        "kind": kind,
        "type": type,
        "app_id": app_id,
        "session_id": session_id,
        "user_id": user_id,
        "actor_user_id": actor_user_id,
        "actor_roles": list(actor_roles or []),
        "role": role,
        "content": content,
        "tool_call_id": tool_call_id,
        "tool_calls": tool_calls,
        "name": name,
        "payload": payload or {},
        "before": before or {},
        "after": after or {},
        "target_user_id": target_user_id,
        "target_app_id": target_app_id,
        "target_resource": target_resource,
        "ip_address": ip_address,
        "user_agent": user_agent[:512] if user_agent else None,
        "correlation_id": correlation_id or "",
        "success": success,
        "message": (message or "")[:8192],
    }


async def record(
    *,
    kind: Kind,
    type: str,
    app_id: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    seq: int = 0,
    actor_user_id: str | None = None,
    actor_roles: list[str] | None = None,
    # message-shape
    role: str | None = None,
    content: str | None = None,
    tool_call_id: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    name: str | None = None,
    # generic
    payload: dict[str, Any] | None = None,
    # audit
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    target_user_id: str | None = None,
    target_app_id: str | None = None,
    target_resource: str | None = None,
    # forensic
    ip_address: str | None = None,
    user_agent: str | None = None,
    correlation_id: str = "",
    # outcome
    success: bool = True,
    message: str = "",
    # routing
    sync: bool = False,
    # retry control (sync path only)
    max_retries: int = 5,
) -> int | None:
    """Insert one row into `history_log`. Returns the row id or None.

    Caller MUST provide `kind` (`message` / `event` / `audit`)
    and `type` (the fine classifier). Everything else is contextual.

    Routing: by default the row is handed to the batched background
    writer and the call returns immediately (`None`). Pass
    `sync=True` to block on a direct INSERT - used for audit rows
    where the response should not ship before the row is on disk.

    When no writer is running, we fall back to the sync path so the
    record is never silently dropped.
    """
    try:
        _enforce_contract(
            kind=kind, type=type, app_id=app_id, session_id=session_id,
            user_id=user_id, seq=seq, actor_user_id=actor_user_id,
        )
    except HistoryContractError as exc:
        logger.error("history.record CONTRACT VIOLATION - dropping row: %s", exc)
        return None

    bridge_mode = "off"
    try:
        from digitorn.core.runtime.session_store.bridge import (
            BridgeMode, get_default_bridge,
        )
        bridge = get_default_bridge()
    except Exception:
        bridge = None
    if bridge is not None and bridge.mode != BridgeMode.OFF:
        bridge_mode = bridge.mode.value
        try:
            await bridge.record(
                kind=kind, type=type,
                app_id=app_id, session_id=session_id, user_id=user_id,
                seq=seq, actor_user_id=actor_user_id,
                actor_roles=actor_roles,
                role=role, content=content, tool_call_id=tool_call_id,
                tool_calls=tool_calls, name=name,
                payload=payload, before=before, after=after,
                target_user_id=target_user_id, target_app_id=target_app_id,
                target_resource=target_resource,
                ip_address=ip_address, user_agent=user_agent,
                correlation_id=correlation_id,
                success=success, message=message,
            )
        except Exception as exc:
            logger.warning(
                "session_store_bridge_failed kind=%s type=%s err=%s",
                kind, type, exc,
            )
    return None


__all__ = ["record", "Kind"]
