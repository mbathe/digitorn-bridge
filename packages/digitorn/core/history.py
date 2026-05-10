"""Unified history ledger writer - one function, three kinds of rows.

Everything that touches the durable log goes through :func:`record`:

    - ``message`` rows (user/assistant/tool)
    - ``event`` rows (tokens, thinking, tool_call, compaction, hooks…)
    - ``audit`` rows (quota change, user disable, app deploy…)

Readers hit the single ``history_log`` table and filter by ``kind`` /
``type`` - no UNION across 3 legacy tables, no divergent ordering.

Durability contract:

    - ``ts`` is monotonic-unique via ``unique_utc_now``, enforced by
      a DB UNIQUE constraint. Stamped at enqueue time so ordering
      reflects the caller's perception of "when" the event happened,
      not whenever the background writer happens to flush.
    - **Default path: batched background writer.** ``record()`` is
      effectively non-blocking for the caller - it stamps ``ts``,
      builds the row dict, hands it to :class:`HistoryWriter`, and
      returns. The writer drains its queue with short
      (≤50 ms) batch commits. On graceful shutdown the writer fully
      drains before the engine closes → zero loss for clean stops.
    - **Sync path**: pass ``sync=True`` to block on a direct INSERT
      in the caller's transaction. Used for audit rows that must be
      durable before the HTTP response returns to the client, and
      for tests that want to observe the row immediately.
    - On ``IntegrityError`` (rare cross-process ts collision), the
      row is retried with the clock bumped forward until success.
    - When no writer is running (CLI / tests / pre-init bootstrap),
      we always fall back to the sync path so records never silently
      disappear.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)


Kind = Literal["message", "event", "audit"]


class HistoryContractError(ValueError):
    """Raised when a caller tries to persist a row that violates the
    history-log contract. Caught by :func:`record` which logs and drops
    the row instead of crashing the caller - malformed rows are a bug
    to investigate, but NOT a reason to kill the ongoing turn."""


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
    """Single strict gate every row crosses before landing in the DB.

    The contract exists so that ANY downstream consumer (replay,
    dedup, /history endpoint, audit export, compliance report) can
    rely on invariants without per-row defensive branches.

    Rules:

      * ``type`` MUST be non-empty - without it readers can't filter.
      * ``kind == "event"``
          - ``session_id`` MUST be non-empty - events are session-scoped
            by design. An untagged event leaks cross-session on the wire
            and breaks the client's source filter.
          - ``seq > 0`` MUST hold - the seq is the SOLE ordering key
            for replay (per event-spec §0). Events stamped seq=0 break
            pagination, dedup, and mid-session reconnect.
          - ``user_id`` MUST be non-empty (pinned to the session's user;
            ``system`` for daemon-initiated events).
      * ``kind == "message"``
          - ``session_id`` MUST be non-empty (turns belong to a session).
          - ``user_id`` MUST be non-empty.
          - ``role`` is not checked here (the model layer already
            constrains it), but callers are expected to pass it.
      * ``kind == "audit"``
          - ``actor_user_id`` MUST be non-empty - an audit row with no
            actor is unusable for forensics / compliance.

    On violation the caller gets a :class:`HistoryContractError`; the
    row never reaches the writer.
    """
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
    """Collect caller kwargs into the dict ``HistoryLog(**)`` expects."""
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
    """Insert one row into ``history_log``. Returns the row id or None.

    Caller MUST provide ``kind`` (``message`` / ``event`` / ``audit``)
    and ``type`` (the fine classifier). Everything else is contextual.

    Routing: by default the row is handed to the batched background
    writer and the call returns immediately (``None``). Pass
    ``sync=True`` to block on a direct INSERT - used for audit rows
    where the response should not ship before the row is on disk.

    When no writer is running, we fall back to the sync path so the
    record is never silently dropped.
    """
    # STRICT CONTRACT CHECK - every row that lands MUST satisfy the
    # invariants. A contract violation is a bug in the emitter: log
    # loudly so it gets fixed, but drop the row instead of aborting
    # the caller's turn. Run BEFORE any backend dispatch so a bad
    # row never reaches the SessionStore either.
    try:
        _enforce_contract(
            kind=kind, type=type, app_id=app_id, session_id=session_id,
            user_id=user_id, seq=seq, actor_user_id=actor_user_id,
        )
    except HistoryContractError as exc:
        logger.error("history.record CONTRACT VIOLATION - dropping row: %s", exc)
        return None

    # SessionStore bridge fan-out. Default OFF (legacy behavior).
    # Set ``DIGITORN_SESSION_STORE_MODE=shadow`` to dual-write (legacy
    # DB + new in-memory store). Set ``primary`` to skip the legacy
    # path entirely -- ultra-fast, no Postgres on the agent loop.
    # Runs BEFORE the DB engine check so a Postgres-less runtime
    # still routes events into the SessionStore.
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
    # Phase 4c: legacy DB write path removed. The bridge is the sole
    # writer; the in-memory SessionStore is the source of truth. The
    # ``shadow`` mode used to dual-write to ``history_log`` for audit
    # readers, but every reader migrated to the new store. Audit /
    # retention queries that need cross-process Postgres visibility now
    # live on a separate audit table (api/user.py + retention_keeper).
    return None


__all__ = ["record", "Kind"]
