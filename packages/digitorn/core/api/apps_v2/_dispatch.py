"""Single source of truth for dispatching a chat turn.

Three call sites previously each owned their own copy of the
"run a turn through manager.chat() + classify errors + emit
events + handle credentials" logic:

* ``messages.py::_run_turn`` (fast-path, POST /messages)
* ``_shared.py::_drain_queue_next::_run_next`` (chained drain)
* ``manager_v2/_queue.py::drain_session_queue`` (Socket.IO resume)

Each had drifted: divergent log levels, missing event-type
promotion for credential errors, no heartbeat in the drain paths,
inconsistent cancellation detection. This module collapses all
three to one async function so the contract is honoured uniformly.

The redesign rationale lives in
``.logs/QUEUE_DISPATCH_REDESIGN.md``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ._shared import _classify_error, _inc_agent_turns, _turn_event

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)


# ─── Public types ──────────────────────────────────────────────────


class TurnSource(str, Enum):
    """Where the dispatch was triggered from. Used as a log
    breadcrumb and to gate behaviour: ``RESUME`` skips the queue
    flip / chain because the resume drain loop manages its own
    state; ``FAST`` and ``DRAIN`` let dispatch_turn flip + chain
    internally.
    """

    FAST = "fast"
    DRAIN = "drain"
    RESUME = "resume"


class TurnStatus(str, Enum):
    """Outcome of a single dispatch_turn() call.

    PAUSED is non-terminal: the queue row stays alive, the daemon
    halts the chain, and an external signal (credential grant,
    abort) is required to advance. COMPLETED / FAILED / CANCELLED
    are terminal and the caller writes them to the queue row.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


@dataclass(frozen=True)
class TurnEntry:
    """Immutable description of a turn to dispatch. Built by each
    caller from its native input shape (POST body / queue row).
    """

    correlation_id: str
    message: str
    workspace: str | None = None
    image_refs: list[dict[str, Any]] | None = None
    client_message_id: str = ""
    queue_row_id: str = ""
    position: int = 0


@dataclass(frozen=True)
class TurnOutcome:
    """What dispatch_turn returned. Callers branch on ``status`` to
    decide whether the queue row needs additional work (PAUSED), or
    just to verify the chain advanced (the chain is now scheduled
    inside dispatch_turn itself - see step 6 of the redesign).
    """

    status: TurnStatus
    error_code: str = ""
    error_data: dict[str, Any] | None = None
    paused_reason: str = ""
    interrupted: bool = False
    extras: dict[str, Any] = field(default_factory=dict)
    next_entry: Any = None


# ─── Internals ─────────────────────────────────────────────────────


_CREDENTIAL_CODES: frozenset[str] = frozenset({
    "credential_required",
    "credential_auth_required",
})

_HEARTBEAT_INTERVAL_S = 30

# Strong refs to fire-and-forget queue-chain dispatch tasks. The asyncio
# event loop only keeps weak refs to tasks (CPython doc), so a task that
# isn't held elsewhere can be GC'd mid-execution. Same pattern as
# `_BG_PERSIST_TASKS` / `_BG_TITLE_TASKS` in agent_loop.py and
# `_active_turn_tasks` in apps_v2/__init__.py. Without it, a chained
# drain after a turn end could disappear under GC pressure and the
# next queued message would sit until the orphan-queue watchdog
# eventually rescued it on the next user POST.
_CHAIN_TASKS: set[asyncio.Task] = set()


def _log_level_for(code: str) -> int:
    """Pick a log level for a given error code. Credential gates are
    routine UX, not crashes - log at INFO so prod dashboards stay
    clean. Anything else gets the full ERROR + traceback treatment.
    """

    if code in _CREDENTIAL_CODES:
        return logging.INFO
    return logging.ERROR


async def _start_heartbeat(queue_row_id: str) -> asyncio.Task | None:
    """Pulse `_mq.heartbeat(row_id)` every ``_HEARTBEAT_INTERVAL_S`` to
    keep the lease alive. No-op when there is no queue row (pure
    fast-path with no DB persistence). Returns the task so the caller
    can cancel it in a finally.
    """

    if not queue_row_id:
        return None
    from digitorn.core.app import message_queue as _mq

    async def _loop() -> None:
        while True:
            try:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
                await _mq.heartbeat(queue_row_id)
            except asyncio.CancelledError:
                return
            except Exception:
                # Heartbeat misses are non-fatal; lease will be reaped
                # by ``reap_expired_leases`` on the next sweep.
                continue

    return asyncio.create_task(_loop())


def _emit(event_bus: Any, type_: str, *, app_id: str, session_id: str,
          user_id: str, correlation_id: str, op_state: Any,
          payload: dict[str, Any] | None = None) -> Any:
    """Thin wrapper around ``event_bus.emit(_turn_event(...))`` that
    swallows publish exceptions. The bus's own retry / fallback policy
    is the right layer to handle delivery failure - dispatch_turn
    must not abort a turn because an event couldn't be published.
    """

    return event_bus.emit(_turn_event(
        type_,
        app_id=app_id,
        session_id=session_id,
        user_id=user_id,
        correlation_id=correlation_id,
        op_state=op_state,
        payload=payload,
    ))


# ─── Public dispatch_turn ──────────────────────────────────────────


async def dispatch_turn(
    request: "Request | None",
    app_id: str,
    session_id: str,
    *,
    entry: TurnEntry,
    user_id: str = "local",
    source: TurnSource = TurnSource.FAST,
    manager: Any | None = None,
    deployed: Any | None = None,
    credential_store: Any | None = None,
) -> TurnOutcome:
    """Run one chat turn end-to-end and return its outcome.

    Two call shapes are supported:

    1. **HTTP-driven** (POST /messages, drain chain) — pass ``request``;
       ``manager``, ``deployed``, ``credential_store`` are auto-resolved
       from ``request.app.state``.
    2. **Background** (Socket.IO resume drain) — pass ``request=None``
       and the resolved ``manager`` (and optionally ``deployed`` /
       ``credential_store``) directly. ``_inc_agent_turns`` is skipped
       since it's tied to the HTTP request lifecycle.

    Responsibilities (single source of truth for all three call sites):

    1. Emit ``message_started`` to anchor the turn on the client.
    2. Start a heartbeat for the queue row, if any.
    3. Pre-flight credential check; promote ``CredentialMissing`` /
       ``CredentialAuthRequired`` to ``credential_required`` /
       ``credential_auth_required`` events. Return ``PAUSED`` so the
       caller halts the queue chain and waits for a grant.
    4. Invoke ``manager.chat()``.
    5. Detect mid-turn cancellation (``session.interrupted``).
    6. On normal exit, emit ``message_done``; on failure, classify the
       exception, promote credential codes to the matching event type,
       emit, and return ``FAILED`` with the structured error payload.

    What this function does NOT own (callers handle):

    * Writing the queue row's terminal status (caller decides whether
      it has a row, and uses ``_mq.finish_and_drain`` to chain).
    * Re-reserving the session for the next entry in a chain.
    * The decision to chain vs stop on PAUSED.
    * ``release_session`` / ``_active_sessions`` bookkeeping (caller
      orchestrates this around the dispatch to close the
      "queued right after turn ends" race - see §11 of the design doc).
    """

    if request is not None:
        from ._shared import _get_manager, _get_deployed
        if manager is None:
            manager = _get_manager(request)
        if deployed is None:
            deployed = _get_deployed(request, app_id)
        if credential_store is None:
            credential_store = getattr(
                request.app.state, "credential_store", None,
            )
    if manager is None:
        raise RuntimeError(
            "dispatch_turn: `manager` is required when `request` is None",
        )
    bus = manager.event_bus

    op_state_module = _import_op_state()
    _OS = op_state_module

    if request is not None:
        await _inc_agent_turns(request)
    heartbeat_task = await _start_heartbeat(entry.queue_row_id)

    interrupted = False
    try:
        # 1. message_started.
        #    `fast_path` is preserved for clients (web + Flutter) that
        #    branched on it before the redesign; `source` is the new
        #    breadcrumb that distinguishes drain from resume on top of
        #    the binary fast/slow split.
        try:
            await _emit(
                bus, "message_started",
                app_id=app_id, session_id=session_id, user_id=user_id,
                correlation_id=entry.correlation_id,
                op_state=_OS.RUNNING,
                payload={
                    "correlation_id": entry.correlation_id,
                    "session_id": session_id,
                    "position": entry.position,
                    "fast_path": source == TurnSource.FAST,
                    "source": source.value,
                },
            )
        except Exception:
            pass

        # 2. Pre-flight credential resolution.
        try:
            from digitorn.core.credentials import (
                ensure_user_credentials_for_app,
            )
            if deployed is not None:
                await ensure_user_credentials_for_app(
                    deployed_app=deployed,
                    user_id=user_id,
                    credential_store=credential_store,
                )
        except asyncio.CancelledError:
            raise
        except Exception as cred_exc:
            outcome = await _on_dispatch_exception(
                cred_exc, bus,
                app_id=app_id, session_id=session_id, user_id=user_id,
                entry=entry, _OS=_OS, pre_chat=True,
            )
            if outcome.status == TurnStatus.PAUSED:
                # No queue flip - caller marks the row failed (Step 5).
                return outcome
            # FAILED: flip queue + emit message_done (BUG-039) in the
            # right order so the client never sees a "done" before the
            # daemon is idle.
            return await _finalize_failed(
                outcome, bus, app_id=app_id, session_id=session_id,
                user_id=user_id, entry=entry, _OS=_OS, source=source,
                request=request, manager=manager,
                deployed=deployed, credential_store=credential_store,
            )

        # 3. Run the turn.
        try:
            await manager.chat(
                app_id, session_id, entry.message,
                user_id=user_id,
                workspace=entry.workspace,
                image_refs=entry.image_refs or None,
                correlation_id=entry.correlation_id or None,
                client_message_id=entry.client_message_id or None,
            )
        except asyncio.CancelledError:
            interrupted = True
            raise
        except Exception as exc:
            outcome = await _on_dispatch_exception(
                exc, bus,
                app_id=app_id, session_id=session_id, user_id=user_id,
                entry=entry, _OS=_OS, pre_chat=False,
            )
            if outcome.status == TurnStatus.PAUSED:
                return outcome
            return await _finalize_failed(
                outcome, bus, app_id=app_id, session_id=session_id,
                user_id=user_id, entry=entry, _OS=_OS, source=source,
                request=request, manager=manager,
                deployed=deployed, credential_store=credential_store,
            )

        # 4. Mid-turn cancellation flag (set by abort handler).
        try:
            sess = await manager.get_session(
                app_id, session_id, user_id=user_id,
            )
            if sess is not None and getattr(sess, "interrupted", False):
                interrupted = True
        except Exception:
            pass

        if interrupted:
            return await _finalize_terminal(
                bus, app_id=app_id, session_id=session_id,
                user_id=user_id, entry=entry, _OS=_OS, source=source,
                status=TurnStatus.CANCELLED,
                terminal_event="message_cancelled",
                terminal_op_state=_OS.CANCELLED,
                queue_terminal="cancelled",
                queue_error_code="turn_cancelled",
                interrupted=True,
                request=request, manager=manager,
                deployed=deployed, credential_store=credential_store,
            )

        # 5. Success.
        return await _finalize_terminal(
            bus, app_id=app_id, session_id=session_id,
            user_id=user_id, entry=entry, _OS=_OS, source=source,
            status=TurnStatus.COMPLETED,
            terminal_event="message_done",
            terminal_op_state=_OS.COMPLETED,
            queue_terminal="completed",
            queue_error_code="",
            request=request, manager=manager,
            deployed=deployed, credential_store=credential_store,
        )

    except asyncio.CancelledError:
        # Abort propagated. Emit message_cancelled before re-raising
        # so the client closes the bubble cleanly even if the caller
        # doesn't get a chance to do it.
        try:
            await _emit(
                bus, "message_cancelled",
                app_id=app_id, session_id=session_id, user_id=user_id,
                correlation_id=entry.correlation_id,
                op_state=_OS.CANCELLED,
                payload={
                    "correlation_id": entry.correlation_id,
                    "session_id": session_id,
                    "reason": "task_cancelled",
                },
            )
        except Exception:
            pass
        raise
    finally:
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
        if request is not None:
            try:
                await _inc_agent_turns(request, -1)
            except Exception:
                pass


# ─── Error path helpers ────────────────────────────────────────────


async def _on_dispatch_exception(
    exc: Exception,
    bus: Any,
    *,
    app_id: str,
    session_id: str,
    user_id: str,
    entry: TurnEntry,
    _OS: Any,
    pre_chat: bool,
) -> TurnOutcome:
    """Classify an exception thrown during dispatch_turn, log it at
    the appropriate level, emit the matching event (with credential
    promotion), and return the structured outcome.

    ``pre_chat=True`` means the exception came from the pre-flight
    credential resolver (so we know the chat() never started). That
    information surfaces in the outcome so the caller can decide
    whether to retry the same entry on resume vs treat it as a real
    failure - today the only difference is the ``paused_reason``
    string, kept so future call sites can branch on it cleanly.
    """

    error_data = _classify_error(exc)
    code = error_data.get("code") or ""
    log_level = _log_level_for(code)

    if log_level == logging.INFO:
        logger.info(
            "turn_paused app=%s session=%s code=%s pre_chat=%s",
            app_id, session_id, code, pre_chat,
        )
    else:
        logger.log(
            log_level,
            "turn_failed app=%s session=%s code=%s pre_chat=%s: %s",
            app_id, session_id, code, pre_chat, exc,
            exc_info=(log_level >= logging.ERROR),
        )

    if code in _CREDENTIAL_CODES:
        evt_type = code
        try:
            await _emit(
                bus, evt_type,
                app_id=app_id, session_id=session_id, user_id=user_id,
                correlation_id=entry.correlation_id,
                op_state=_OS.WAITING_APPROVAL,
                payload={
                    **error_data,
                    "correlation_id": entry.correlation_id,
                },
            )
        except Exception:
            pass
        return TurnOutcome(
            status=TurnStatus.PAUSED,
            error_code=code,
            error_data=error_data,
            paused_reason="credential_missing"
            if code == "credential_required"
            else "credential_auth_required",
        )

    # Generic failure path.
    try:
        await _emit(
            bus, "error",
            app_id=app_id, session_id=session_id, user_id=user_id,
            correlation_id=entry.correlation_id,
            op_state=_OS.FAILED,
            payload={
                **error_data,
                "correlation_id": entry.correlation_id,
            },
        )
    except Exception:
        pass
    return TurnOutcome(
        status=TurnStatus.FAILED,
        error_code=code or "internal",
        error_data=error_data,
    )


async def _flip_queue_row(
    *,
    session_id: str,
    entry: TurnEntry,
    source: TurnSource,
    terminal_status: str,
    error_code: str,
) -> Any:
    """Atomically transition the row to its terminal state and pop the
    next queued row for the session. Returns the next entry (or None).

    Skipped when:

    * ``source == RESUME`` - the resume drain loop owns its own queue
      flips via ``mark_done`` / ``mark_failed``; if dispatch_turn also
      flipped, the loop's next ``next_queued`` would skip every other
      entry.
    * ``entry.queue_row_id`` is empty - pure fast-path with no DB row
      to flip (no queue infrastructure involved at all).
    """

    if source == TurnSource.RESUME:
        return None
    if not entry.queue_row_id:
        return None
    from digitorn.core.app import message_queue as _mq
    try:
        return await _mq.finish_and_drain(
            session_id, entry.queue_row_id,
            terminal_status=terminal_status,
            error_code=error_code,
        )
    except Exception as exc:
        logger.warning(
            "queue_flip_failed app=%s sid=%s row=%s: %s",
            "<unknown>", session_id, entry.queue_row_id, exc,
        )
        return None


async def _resolve_awaiter(
    correlation_id: str, status: TurnStatus, error_code: str,
) -> None:
    """Unblock any ``mode=wait`` POST caller that's awaiting this
    correlation_id. Resolved on COMPLETED / CANCELLED, failed on
    FAILED, no-op on PAUSED (caller still has work to do).
    """

    if not correlation_id:
        return
    from digitorn.core.app import message_queue as _mq
    try:
        if status == TurnStatus.COMPLETED:
            _mq.resolve_awaiter(correlation_id, {"status": "completed"})
        elif status == TurnStatus.CANCELLED:
            _mq.resolve_awaiter(correlation_id, {"status": "cancelled"})
        elif status == TurnStatus.FAILED:
            _mq.fail_awaiter(
                correlation_id,
                RuntimeError(error_code or "internal"),
            )
    except Exception:
        pass


def _schedule_chain(
    request: "Request | None",
    app_id: str,
    session_id: str,
    *,
    next_entry: Any,
    user_id: str,
    manager: Any,
    deployed: Any,
    credential_store: Any,
) -> None:
    """If a next queued row was popped by the atomic flip, schedule the
    next ``dispatch_turn`` as a fire-and-forget task so the chain runs
    concurrently with the current turn's caller returning. ``source``
    flips to ``DRAIN`` on the chained call - the new dispatch will own
    its own ``message_started`` emit, queue flip, and terminal events.
    """

    if next_entry is None:
        return
    next_dispatch_entry = TurnEntry(
        correlation_id=getattr(next_entry, "correlation_id", "") or "",
        message=getattr(next_entry, "message", "") or "",
        image_refs=getattr(next_entry, "image_refs", None) or None,
        queue_row_id=getattr(next_entry, "id", "") or "",
        position=getattr(next_entry, "position", 0) or 0,
    )

    async def _run_chained() -> None:
        try:
            await dispatch_turn(
                request, app_id, session_id,
                entry=next_dispatch_entry,
                user_id=user_id,
                source=TurnSource.DRAIN,
                manager=manager,
                deployed=deployed,
                credential_store=credential_store,
            )
        except Exception as exc:
            logger.warning(
                "chain_dispatch_failed app=%s sid=%s: %s",
                app_id, session_id, exc,
            )

    _chain_task = asyncio.create_task(_run_chained())
    _CHAIN_TASKS.add(_chain_task)
    _chain_task.add_done_callback(_CHAIN_TASKS.discard)


async def _finalize_failed(
    outcome: TurnOutcome,
    bus: Any,
    *,
    app_id: str,
    session_id: str,
    user_id: str,
    entry: TurnEntry,
    _OS: Any,
    source: TurnSource,
    request: "Request | None" = None,
    manager: Any = None,
    deployed: Any = None,
    credential_store: Any = None,
) -> TurnOutcome:
    """Order matters here. The race "queued just after turn ends"
    (Bug B) is closed by doing the queue terminal flip FIRST, then
    emitting the spinner-stop event. Otherwise the client receives
    ``message_done``, immediately POSTs a new message, and the daemon's
    ``is_turn_running`` is still True because the row hasn't flipped
    yet → the new message gets queued and the user sees the QUEUED
    flicker.
    """

    next_entry = await _flip_queue_row(
        session_id=session_id, entry=entry, source=source,
        terminal_status="failed",
        error_code=outcome.error_code or "internal",
    )
    await _resolve_awaiter(
        entry.correlation_id, TurnStatus.FAILED, outcome.error_code,
    )
    try:
        await _emit(
            bus, "message_done",
            app_id=app_id, session_id=session_id, user_id=user_id,
            correlation_id=entry.correlation_id,
            op_state=_OS.COMPLETED,
            payload={
                "correlation_id": entry.correlation_id,
                "session_id": session_id,
            },
        )
    except Exception:
        pass
    # Schedule the chain BEFORE returning. Fire-and-forget asyncio task
    # so the caller returns immediately - the next dispatch runs
    # concurrently with this turn's caller wrapping up.
    _schedule_chain(
        request, app_id, session_id,
        next_entry=next_entry, user_id=user_id,
        manager=manager, deployed=deployed,
        credential_store=credential_store,
    )
    return TurnOutcome(
        status=outcome.status,
        error_code=outcome.error_code,
        error_data=outcome.error_data,
        paused_reason=outcome.paused_reason,
        interrupted=outcome.interrupted,
        extras=outcome.extras,
        next_entry=next_entry,
    )


async def _finalize_terminal(
    bus: Any,
    *,
    app_id: str,
    session_id: str,
    user_id: str,
    entry: TurnEntry,
    _OS: Any,
    source: TurnSource,
    status: TurnStatus,
    terminal_event: str,
    terminal_op_state: Any,
    queue_terminal: str,
    queue_error_code: str,
    interrupted: bool = False,
    request: "Request | None" = None,
    manager: Any = None,
    deployed: Any = None,
    credential_store: Any = None,
) -> TurnOutcome:
    """Same race-fix shape as `_finalize_failed`, generalised for
    COMPLETED and CANCELLED outcomes.
    """

    next_entry = await _flip_queue_row(
        session_id=session_id, entry=entry, source=source,
        terminal_status=queue_terminal,
        error_code=queue_error_code,
    )
    await _resolve_awaiter(entry.correlation_id, status, queue_error_code)
    try:
        payload = {
            "correlation_id": entry.correlation_id,
            "session_id": session_id,
        }
        await _emit(
            bus, terminal_event,
            app_id=app_id, session_id=session_id, user_id=user_id,
            correlation_id=entry.correlation_id,
            op_state=terminal_op_state,
            payload=payload,
        )
    except Exception:
        pass
    _schedule_chain(
        request, app_id, session_id,
        next_entry=next_entry, user_id=user_id,
        manager=manager, deployed=deployed,
        credential_store=credential_store,
    )
    return TurnOutcome(
        status=status,
        interrupted=interrupted,
        next_entry=next_entry,
    )


def _import_op_state() -> Any:
    """Lazy import of ``OpState`` so this module's top-level imports
    stay tiny - the events package pulls in the pydantic model graph
    which we don't want to drag in at app boot before this function
    is actually called.
    """

    from digitorn.core.events.envelope import OpState
    return OpState
