"""Digitorn — Universal event envelope.

Every event flowing through the system is wrapped in a UniversalEvent.
This provides causality tracking, session binding, priority routing,
and a consistent structure for all backends.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, Field


def _gen_event_id() -> str:
    """Event IDs use a stable ``ev-<12hex>`` prefix so they're easy to
    distinguish from ``fp-<12hex>`` correlation ids in log lines —
    two flows that used to collide when child() fell back to
    event_id as the correlation_id (BUG-010)."""
    return f"ev-{uuid.uuid4().hex[:12]}"


class UniversalEvent(BaseModel):
    """Causality-aware event envelope."""

    event_id: str = Field(default_factory=_gen_event_id)
    topic: str = Field(..., description="Dot-separated topic (e.g. 'digitorn.module.cli.action_started').")
    timestamp: float = Field(default_factory=time.time)

    correlation_id: str | None = Field(
        default=None,
        description="Groups related events across the entire workflow.",
    )
    causation_id: str | None = Field(
        default=None,
        description="ID of the event that directly caused this one.",
    )
    parent_id: str | None = Field(
        default=None,
        description="ID of the logical parent event.",
    )

    app_id: str | None = Field(
        default=None,
        description="Application this event belongs to. Events are isolated per app.",
    )
    session_id: str | None = Field(
        default=None,
        description="User session this event belongs to.",
    )
    agent_id: str | None = Field(
        default=None,
        description="Agent that triggered or should receive this event.",
    )

    priority: int = Field(
        default=0,
        ge=0,
        le=10,
        description="0 = normal, 10 = highest priority.",
    )

    source: str = Field(default="", description="Component that emitted the event.")
    event_type: str = Field(default="info", description="Event category (info, error, progress, result, …).")
    data: dict[str, Any] = Field(default_factory=dict)

    def child(self, topic: str, **kwargs: Any) -> UniversalEvent:
        """Create a child event inheriting the full scope and causality chain."""
        return UniversalEvent(
            topic=topic,
            correlation_id=self.correlation_id or self.event_id,
            causation_id=self.event_id,
            parent_id=self.event_id,
            app_id=self.app_id,
            session_id=self.session_id,
            agent_id=self.agent_id,
            source=self.source,
            **kwargs,
        )

    def sibling(self, topic: str, **kwargs: Any) -> UniversalEvent:
        """Create a sibling event sharing the same parent and scope."""
        return UniversalEvent(
            topic=topic,
            correlation_id=self.correlation_id,
            causation_id=self.causation_id,
            parent_id=self.parent_id,
            app_id=self.app_id,
            session_id=self.session_id,
            agent_id=self.agent_id,
            source=self.source,
            **kwargs,
        )
