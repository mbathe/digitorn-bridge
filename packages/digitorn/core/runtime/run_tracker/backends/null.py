"""No-op backend.

Selected when an operator wants the runtime to advertise its events
to nothing - e.g., during benchmarks, in test harnesses, or when
running the runtime in a transient sandbox where persistence has no
value. Every method returns immediately.
"""

from __future__ import annotations

from typing import Any, Optional


class NullBackend:
    def __init__(self, **_: Any) -> None:
        # Accept (and ignore) backend config so ``select_backend("null",
        # {"path": ...})`` works the same as the other backends.
        pass

    async def setup(self) -> None:
        pass

    async def teardown(self) -> None:
        pass

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
        pass

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
        pass

    async def emit_event(
        self,
        *,
        run_id: str,
        event_type: str,
        data: dict[str, Any],
        sequence: int,
        emitted_at_iso: str,
    ) -> None:
        pass

    async def increment_turns(self, *, run_id: str) -> None:
        pass

    async def increment_sub_agents(self, *, run_id: str) -> None:
        pass
