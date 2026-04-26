"""Unified history ledger writer — one function, three kinds of rows.

Everything that touches the durable log goes through :func:`record`:

    - ``message`` rows (user/assistant/tool)
    - ``event`` rows (tokens, thinking, tool_call, compaction, hooks…)
    - ``audit`` rows (quota change, user disable, app deploy…)

Readers hit the single ``history_log`` table and filter by ``kind`` /
``type`` — no UNION across 3 legacy tables, no divergent ordering.

Durability contract:

    - ``ts`` is monotonic-unique via ``unique_utc_now``, enforced by
      a DB UNIQUE constraint. Stamped at enqueue time so ordering
      reflects the caller's perception of "when" the event happened,
      not whenever the background writer happens to flush.
    - **Default path: batched background writer.** ``record()`` is
      effectively non-blocking for the caller — it stamps ``ts``,
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
    the row instead of crashing the caller — malformed rows are a bug
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

      * ``type`` MUST be non-empty — without it readers can't filter.
      * ``kind == "event"``
          - ``session_id`` MUST be non-empty — events are session-scoped
            by design. An untagged event leaks cross-session on the wire
            and breaks the client's source filter.
          - ``seq > 0`` MUST hold — the seq is the SOLE ordering key
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
          - ``actor_user_id`` MUST be non-empty — an audit row with no
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
                "required. Untagged events leak across sessions — fix the "
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
                "actor_user_id is required — audit rows without an "
                "actor are unusable for forensics / compliance."
            )

    else:
        raise HistoryContractError(
            f"history.record: unknown kind={kind!r} — expected one of "
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
    ``sync=True`` to block on a direct INSERT — used for audit rows
    where the response should not ship before the row is on disk.

    When no writer is running, we fall back to the sync path so the
    record is never silently dropped.
    """
    try:
        from digitorn.core.database import _engine
        if _engine is None:
            return None
        from digitorn.core.unique_clock import unique_utc_now
    except Exception as exc:
        logger.debug("history.record setup failed: %s", exc)
        return None

    # STRICT CONTRACT CHECK — every row that lands in history_log MUST
    # satisfy the invariants. A contract violation is a bug in the
    # emitter: log loudly so it gets fixed, but drop the row instead
    # of aborting the caller's turn. We'd rather lose one malformed
    # event than crash an agent mid-response.
    try:
        _enforce_contract(
            kind=kind, type=type, app_id=app_id, session_id=session_id,
            user_id=user_id, seq=seq, actor_user_id=actor_user_id,
        )
    except HistoryContractError as exc:
        logger.error("history.record CONTRACT VIOLATION — dropping row: %s", exc)
        return None

    # Stamp the timestamp EAGERLY so ordering reflects the caller's
    # "now" — not the writer's later flush point.
    ts = unique_utc_now()
    row_kwargs = _build_row_kwargs(
        kind=kind, type=type, app_id=app_id, session_id=session_id,
        user_id=user_id, seq=seq,
        actor_user_id=actor_user_id, actor_roles=actor_roles,
        role=role, content=content, tool_call_id=tool_call_id,
        tool_calls=tool_calls, name=name,
        payload=payload, before=before, after=after,
        target_user_id=target_user_id, target_app_id=target_app_id,
        target_resource=target_resource,
        ip_address=ip_address, user_agent=user_agent,
        correlation_id=correlation_id,
        success=success, message=message, ts=ts,
    )

    # Fast path: hand to the batched writer.
    if not sync:
        try:
            from digitorn.core.history_writer import get_writer
            writer = get_writer()
        except Exception:
            writer = None
        if writer is not None and writer.running:
            ok = writer.enqueue(row_kwargs)
            if ok:
                return None
            # Queue overflow — fall through to sync insert so the row
            # is never silently dropped.
            logger.warning(
                "history.record writer_queue_full — falling back to sync "
                "kind=%s type=%s",
                kind, type,
            )

    return await _insert_sync(row_kwargs, max_retries=max_retries)


async def _insert_sync(
    row_kwargs: dict[str, Any], *, max_retries: int = 5,
) -> int | None:
    """Synchronous INSERT with retry on ts collision.

    Used for sync=True callers AND as the overflow/no-writer fallback.
    """
    try:
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import HistoryLog
        from digitorn.core.unique_clock import unique_utc_now
    except Exception as exc:
        logger.debug("history._insert_sync setup failed: %s", exc)
        return None

    from sqlalchemy.exc import IntegrityError

    for attempt in range(max_retries + 1):
        try:
            async with get_session_factory()() as db:
                row = HistoryLog(**row_kwargs)
                db.add(row)
                await db.commit()
                await db.refresh(row)
                return row.id
        except IntegrityError as exc:
            if attempt >= max_retries:
                logger.error(
                    "history._insert_sync exhausted retries kind=%s "
                    "type=%s: %s",
                    row_kwargs.get("kind"), row_kwargs.get("type"), exc,
                )
                return None
            # Bump the clock and retry with a fresh ts.
            row_kwargs["ts"] = unique_utc_now()
        except Exception as exc:
            logger.error(
                "history._insert_sync failed kind=%s type=%s: %s",
                row_kwargs.get("kind"), row_kwargs.get("type"), exc,
                exc_info=True,
            )
            return None
    return None


__all__ = ["record", "Kind"]
