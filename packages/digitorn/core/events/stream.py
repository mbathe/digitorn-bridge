"""Digitorn — Execution stream for real-time agent feedback.

Bridges a running module action to the event bus, allowing modules
to emit progress, partial results, logs, and warnings that agents
and clients receive in real-time via Socket.IO.

Every event emitted through the stream is a child of the original
action_started event, so agents can correlate all events from a
single execution via correlation_id.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from digitorn.core.events.bus import EventBus
    from digitorn.core.events.models import UniversalEvent


class ExecutionStream:
    """Real-time feedback channel injected into ExecutionContext.stream.

    Modules use this to push live updates to agents during execution.
    All emitted events are children of the parent_event (action_started),
    inheriting its correlation_id, session_id, and source.
    """

    def __init__(
        self,
        event_bus: "EventBus",
        parent_event: "UniversalEvent",
        module_id: str,
        action: str,
    ) -> None:
        self._bus = event_bus
        self._parent = parent_event
        self._module_id = module_id
        self._action = action
        self._topic_prefix = f"digitorn.module.{module_id}"

    async def progress(
        self, step: int, total: int, message: str = ""
    ) -> None:
        """Report execution progress (e.g. step 3/10)."""
        await self._emit(
            "action_progress",
            {"step": step, "total": total, "message": message},
        )

    async def partial_result(self, data: Any) -> None:
        """Push an intermediate result the agent can use immediately."""
        await self._emit("action_partial_result", {"result": data})

    async def log(
        self, level: str, message: str, data: dict[str, Any] | None = None
    ) -> None:
        """Emit a log entry (debug, info, warning, error)."""
        payload: dict[str, Any] = {"level": level, "message": message}
        if data:
            payload["details"] = data
        await self._emit("action_log", payload)

    async def warning(self, message: str, data: dict[str, Any] | None = None) -> None:
        """Signal a non-blocking issue to the agent."""
        await self.log("warning", message, data)

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit a child event through the bus."""
        data["action"] = self._action
        event = self._parent.child(
            topic=f"{self._topic_prefix}.{event_type}",
            event_type=event_type,
            data=data,
        )
        try:
            await self._bus.publish(event)
        except Exception as exc:
            logger.warning("Event publish failed: %s", exc)
