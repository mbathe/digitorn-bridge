"""Unified injection point for every runtime-emitted system directive."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from digitorn.core.events.envelope import (
    OpState,
    OpType,
    SessionEvent,
    gen_op_id,
)

logger = logging.getLogger(__name__)


DirectiveSource = Literal[
    "behavior_classifier",
    "behavior_pending",
    "delegation_check",
    "turn_limit_near",
    "nudge_empty_response",
    "nudge_unfinished_work",
    "hook_inject_message",
    "hook_shell_stdout",
    "hook_shell_blocked",
    "hook_shell_error",
    "hook_summary",
    "cron_reminder",
    "template_addendum",
    "compaction_summary",
    "other",
]


InjectPosition = Literal["append", "insert_before_last", "prepend"]


def _apply_local_mutation(
    messages: list[dict[str, Any]],
    content: str,
    position: InjectPosition,
) -> None:
    entry = {"role": "system", "content": content}
    if position == "prepend":
        messages.insert(0, entry)
    elif position == "insert_before_last":
        messages.insert(max(len(messages) - 1, 0), entry)
    else:
        messages.append(entry)


def _resolve_bus(ctx: Any, *, bus_override: Any | None = None) -> Any | None:
    if bus_override is not None:
        return bus_override
    if ctx is None:
        return None
    return (
        getattr(ctx, "event_bus", None)
        or getattr(ctx, "_event_bus", None)
    )


def _build_event(
    *,
    ctx: Any,
    content: str,
    source: str,
    turn: int | None,
    metadata: dict[str, Any] | None,
    app_id_override: str | None = None,
    session_id_override: str | None = None,
    user_id_override: str | None = None,
) -> SessionEvent | None:
    if app_id_override is not None:
        app_id = str(app_id_override)
    else:
        app_id = str(getattr(ctx, "app_id", "") or "") if ctx is not None else ""
    if session_id_override is not None:
        session_id = str(session_id_override)
    else:
        session_id = str(getattr(ctx, "session_id", "") or "") if ctx is not None else ""
    if user_id_override is not None:
        user_id = str(user_id_override)
    else:
        user_id = (
            str(getattr(ctx, "user_id", "") or "local")
            if ctx is not None else "local"
        )
    if not app_id or not session_id:
        return None

    payload: dict[str, Any] = {
        "content": content,
        "source": source,
    }
    if turn is not None:
        payload["turn"] = int(turn)
    if metadata:
        payload["metadata"] = dict(metadata)

    return SessionEvent.build(
        type="system_message",
        app_id=app_id,
        session_id=session_id,
        user_id=user_id,
        op_id=gen_op_id("sysmsg"),
        op_type=OpType.MESSAGE,
        op_state=OpState.COMPLETED,
        payload=payload,
    )


async def inject_system_directive(
    ctx: Any,
    *,
    content: str,
    source: DirectiveSource | str,
    messages: list[dict[str, Any]] | None = None,
    turn: int | None = None,
    metadata: dict[str, Any] | None = None,
    position: InjectPosition = "append",
    bus: Any | None = None,
    app_id: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
) -> int:
    """Persist a system directive and (optionally) inject it into the"""
    if not content:
        return 0

    if messages is not None:
        _apply_local_mutation(messages, content, position)

    resolved_bus = _resolve_bus(ctx, bus_override=bus)
    if resolved_bus is None:
        return 0

    event = _build_event(
        ctx=ctx, content=content, source=str(source),
        turn=turn, metadata=metadata,
        app_id_override=app_id,
        session_id_override=session_id,
        user_id_override=user_id,
    )
    if event is None:
        return 0

    try:
        return int(await resolved_bus.emit(event))
    except Exception as exc:
        logger.warning(
            "system_directive emit failed (source=%s): %s",
            source, exc,
        )
        return 0


def inject_system_directive_sync(
    ctx: Any,
    *,
    content: str,
    source: DirectiveSource | str,
    messages: list[dict[str, Any]] | None = None,
    turn: int | None = None,
    metadata: dict[str, Any] | None = None,
    position: InjectPosition = "append",
    bus: Any | None = None,
    app_id: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Sync wrapper: mutate `messages` immediately, schedule the"""
    if not content:
        return

    if messages is not None:
        _apply_local_mutation(messages, content, position)

    resolved_bus = _resolve_bus(ctx, bus_override=bus)
    if resolved_bus is None:
        return

    event = _build_event(
        ctx=ctx, content=content, source=str(source),
        turn=turn, metadata=metadata,
        app_id_override=app_id,
        session_id_override=session_id,
        user_id_override=user_id,
    )
    if event is None:
        return

    async def _emit() -> None:
        try:
            await resolved_bus.emit(event)
        except Exception as exc:
            logger.warning(
                "system_directive emit failed (source=%s): %s",
                source, exc,
            )

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_emit())
    except RuntimeError:
        logger.debug(
            "inject_system_directive_sync called without running loop "
            "(source=%s) - event dropped, local mutation kept",
            source,
        )


__all__ = [
    "DirectiveSource",
    "InjectPosition",
    "inject_system_directive",
    "inject_system_directive_sync",
]
