"""Protocol for agent-run tracking backends."""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class TrackerBackend(Protocol):
    """One persisted agent_runs / agent_run_events store."""


    async def setup(self) -> None:
        """Called once at worker start. Open connections, prepare"""
        ...

    async def teardown(self) -> None:
        """Called once at worker stop. Flush, close, release."""
        ...


    async def start_run(
        self,
        *,
        run_id: str,
        ctx_snapshot: dict[str, Any],
        max_turns: Optional[int],
        parent_run_id: Optional[str],
        task_summary: Optional[str],
        queued_at_iso: str,
        started_at_iso: str,
    ) -> None:
        """Insert a new agent_runs row in `status='active'`."""
        ...

    async def complete_run(
        self,
        *,
        run_id: str,
        status: str,
        prompt_tokens: int,
        completion_tokens: int,
        turns_used: int,
        status_reason: Optional[str],
        completed_at_iso: str,
    ) -> None:
        """Close a run."""
        ...

    async def emit_event(
        self,
        *,
        run_id: str,
        event_type: str,
        data: dict[str, Any],
        sequence: int,
        emitted_at_iso: str,
    ) -> None:
        """Append one row to agent_run_events."""
        ...

    async def increment_turns(self, *, run_id: str) -> None: ...

    async def increment_sub_agents(self, *, run_id: str) -> None: ...
