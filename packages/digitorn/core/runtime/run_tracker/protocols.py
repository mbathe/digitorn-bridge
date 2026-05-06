"""Protocol for agent-run tracking backends.

The runtime hot path does NOT call any backend method directly. It
hands events to the worker queue, which drains them into whatever
backend the daemon was configured with at boot. Backends therefore
have an async API even when their implementation is synchronous
(file write, in-memory dict): the worker awaits each call so a slow
backend can't starve the queue.

A backend MUST be safe to run concurrently with itself - the worker
serialises calls per-event-type, but several events for different
runs can interleave. A backend that needs a write lock should hold
it internally.

Pluggable purpose: the daemon ships with a Postgres backend (cloud
mode) and a JSON-file backend (local mode). Operators can plug in
their own (SQLite, LMDB, Kafka, …) by implementing this Protocol
and registering the class in ``BACKEND_REGISTRY`` before
``start_worker`` is called.

Failure semantics: any exception a backend raises is caught by the
worker and logged at WARNING. The agent run continues uninterrupted -
tracking is observability, not durability of the chat.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class TrackerBackend(Protocol):
    """One persisted agent_runs / agent_run_events store."""

    # ── lifecycle ────────────────────────────────────────────────

    async def setup(self) -> None:
        """Called once at worker start. Open connections, prepare
        statements, create directories. Must be idempotent."""
        ...

    async def teardown(self) -> None:
        """Called once at worker stop. Flush, close, release."""
        ...

    # ── writes (called by the worker for each enqueued event) ───

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
        """Insert a new agent_runs row in ``status='active'``.

        ``ctx_snapshot`` is a plain dict captured at enqueue time so
        the backend never holds a reference to the live ``ctx`` object
        (the runtime may mutate or garbage-collect it). Required keys:

            user_id, app_id, session_id, agent_id

        Optional keys:

            agent_name, workspace, provider, model
        """
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
        """Close a run.

        ``status`` is one of: completed | failed | cancelled | timeout |
        paused. The backend updates the row, materialises duration if it
        wants to, and (for the Postgres backend) stamps
        ``user_sessions.last_completed_run_id`` on success.
        """
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
        """Append one row to agent_run_events.

        ``sequence`` is allocated by the worker (per-run monotonic),
        so the backend doesn't need to compute it. ``emitted_at_iso``
        is the wall clock at enqueue time - the backend uses it as
        ``created_at`` and computes ``elapsed_ms`` against the run's
        ``started_at`` if it has it.
        """
        ...

    async def increment_turns(self, *, run_id: str) -> None: ...

    async def increment_sub_agents(self, *, run_id: str) -> None: ...
