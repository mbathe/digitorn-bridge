"""SessionEvent — the universal event envelope every client-facing
event MUST use.

Problem this solves
-------------------
When a user disconnects mid-turn and reconnects, the client cannot
reconstruct the current state of in-flight operations:

    * Is the tool I saw running still running, or did it finish while
      I was offline?
    * Is the sub-agent I spawned still working, or did it crash?
    * Was the approval I was waiting for resolved?

The old ad-hoc ``{"type": ..., "data": {...}}`` dicts provided no
structural invariant for the client to group lifecycle events of a
single logical operation — callers had to reach into ``payload`` and
hope the shape was consistent.

Contract
--------
Every event carries:

  * ``event_id``      — globally unique, auto-generated.
  * ``seq``           — per-user monotonically increasing counter.
  * ``ts``            — server-side ISO-8601 UTC timestamp, µs precision.
  * ``type``          — fine-grained event type (``tool_start``,
                        ``agent_result``, …).
  * ``kind``          — high-level category (``session``, ``approval``,
                        ``error``, …) — auto-derived from ``type``.
  * ``app_id``, ``session_id``, ``user_id`` — **always required**.
  * ``correlation_id`` — the turn this event belongs to (same ``fp-…``
                         id across every event of a message).
  * ``op_id``         — the atomic operation this event belongs to. All
                        lifecycle events of one tool, one sub-agent,
                        one approval, one compaction, one turn share
                        the same ``op_id``.
  * ``op_type``       — enum: ``turn`` | ``tool`` | ``agent`` |
                        ``approval`` | ``compact`` | ``message``.
  * ``op_state``      — enum: ``pending`` | ``running`` |
                        ``waiting_approval`` | ``completed`` |
                        ``failed`` | ``cancelled`` | ``timeout``.
  * ``op_parent_id``  — ``op_id`` of the enclosing op (e.g. a tool
                        call inside a sub-agent points at that agent's
                        ``op_id``).
  * ``payload``       — type-specific fields.

Terminal states (``completed``, ``failed``, ``cancelled``, ``timeout``)
let the client apply a trivial rule on reconnect::

    ops = {}   # op_id -> latest event
    for ev in replay_since(last_seq):
        ops[ev.op_id] = ev
    for ev in ops.values():
        if ev.op_state in TERMINAL_STATES:
            show_final(ev)
        else:
            show_spinner(ev)

The constructor is fail-closed: missing ``app_id`` / ``session_id`` /
``user_id`` / ``op_id`` / ``op_type`` / ``op_state`` raises
``ValueError`` before the bus ever sees the event. A bug in dev
shouts immediately instead of leaking anonymous events in prod.

Backwards-compatibility
-----------------------
``SessionEventBus.publish(key, dict)`` keeps working — it wraps the
legacy dict into a ``SessionEvent`` using sensible defaults and logs
a deprecation warning the first time each call site fires. Every call
site is expected to migrate to ``bus.emit(SessionEvent(...))``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


def _now_iso_us() -> str:
    """UTC timestamp with microsecond precision, ISO-8601.

    ``seq`` is strictly monotonic per user so it is the primary
    ordering key; ``ts`` is there for human debugging and cross-user
    comparisons (where ``seq`` is meaningless).
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z",
    )


def _gen_event_id() -> str:
    return f"ev-{uuid.uuid4().hex[:12]}"


class OpType(str, Enum):
    """The kind of long-lived operation an event belongs to.

    These are the cycles the client renders as a single UI element
    (a tool chip, a sub-agent pill, an approval modal, …). Each cycle
    has a deterministic set of lifecycle events that all share one
    ``op_id``.
    """

    TURN = "turn"
    TOOL = "tool"
    AGENT = "agent"
    APPROVAL = "approval"
    COMPACT = "compact"
    MESSAGE = "message"
    # ``SYSTEM`` is reserved for daemon-level events (connect,
    # daemon_shutdown, …) that don't belong to any user-facing cycle.
    SYSTEM = "system"


class OpState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


TERMINAL_STATES: frozenset[OpState] = frozenset({
    OpState.COMPLETED,
    OpState.FAILED,
    OpState.CANCELLED,
    OpState.TIMEOUT,
})


# Auto-derive ``kind`` (coarse category) from ``type`` (fine). Kept as
# a single map so adding a new event type is one line to update.
_KIND_MAP: dict[str, str] = {
    # Turn
    "user_message": "session",
    "message_queued": "session",
    "message_merged": "session",
    "message_replaced": "session",
    "message_started": "session",
    "message_done": "session",
    "message_cancelled": "session",
    "queue_full": "session",
    "result": "session",
    "turn_complete": "session",
    "stream_done": "session",
    # LLM streaming
    "token": "session",
    "token_usage": "session",
    "thinking": "session",
    "thinking_started": "session",
    "thinking_delta": "session",
    "in_token": "session",
    "out_token": "session",
    "assistant_stream_snapshot": "session",
    # Tools
    "tool_start": "session",
    "tool_call": "session",
    "tool_end": "session",
    # Memory
    "memory_update": "session",
    # Sub-agents
    "agent_event": "session",
    "agent_spawn": "session",
    "agent_progress": "session",
    "agent_result": "session",
    "agent_cancel": "session",
    # Hooks
    "hook": "session",
    "hook_notification": "session",
    # Abort + background
    "abort": "session",
    "bg_task_update": "session",
    "terminal_output": "session",
    # Preview
    "preview:state_changed": "session",
    "preview:state_patched": "session",
    "preview:cleared": "session",
    "preview:resource_set": "session",
    "preview:resource_patched": "session",
    "preview:resource_deleted": "session",
    "preview:resource_bulk_set": "session",
    "preview:channel_cleared": "session",
    "preview:snapshot": "session",
    "preview:delta": "session",
    # Widget
    "widget:render": "session",
    "widget:update": "session",
    "widget:close": "session",
    "widget:error": "session",
    "widget:state": "session",
    "widget:cleared": "session",
    "widget:snapshot": "session",
    # Compact
    "compact_started": "session",
    "compact_done": "session",
    # Durable snapshot emitted by _do_truncate / _do_summarize. Carries
    # the full reconstruction payload (summary_text, memory, tools,
    # kept_range, …) so a restarted daemon can resume exactly from the
    # last compaction without replaying the compacted messages.
    "compaction": "session",
    # Credentials
    "credential_required": "session",
    "credential_auth_required": "session",
    # Approvals
    "approval_request": "approval",
    "approval_resolved": "approval",
    "approval_progress": "approval",
    # System
    "connected": "system",
    "status": "status",
    # Errors
    "error": "error",
    # Backgrounds / cron
    "notification": "background_activation",
    "notification_result": "background_activation",
    # Replay snapshots (hydration)
    "queue:snapshot": "session",
    # Authoritative session state envelope — client's source of truth
    # for UI (turn active, queue depth, compaction, seq). See
    # ``AppManager.build_state_envelope`` for the payload shape.
    "state:snapshot": "session",
    # Turn liveness heartbeat, emitted every ~3s while a turn is
    # running. Lets the client watchdog distinguish "still thinking"
    # from "server stuck" without polling.
    "turn:heartbeat": "session",
}


# Which types SHOULD carry which op_type, used to heuristically fix
# up legacy ``publish(key, dict)`` calls that don't carry the contract
# yet. Migrated call sites set ``op_type`` explicitly.
_LEGACY_OP_TYPE: dict[str, OpType] = {
    "user_message": OpType.TURN,
    "message_queued": OpType.TURN,
    "message_merged": OpType.TURN,
    "message_replaced": OpType.TURN,
    "message_started": OpType.TURN,
    "message_done": OpType.TURN,
    "message_cancelled": OpType.TURN,
    "result": OpType.TURN,
    "turn_complete": OpType.TURN,
    "stream_done": OpType.TURN,
    "token": OpType.TURN,
    "token_usage": OpType.TURN,
    "thinking": OpType.TURN,
    "thinking_started": OpType.TURN,
    "thinking_delta": OpType.TURN,
    "in_token": OpType.TURN,
    "out_token": OpType.TURN,
    "assistant_stream_snapshot": OpType.TURN,
    "tool_start": OpType.TOOL,
    "tool_call": OpType.TOOL,
    "tool_end": OpType.TOOL,
    "agent_event": OpType.AGENT,
    "agent_spawn": OpType.AGENT,
    "agent_progress": OpType.AGENT,
    "agent_result": OpType.AGENT,
    "agent_cancel": OpType.AGENT,
    "approval_request": OpType.APPROVAL,
    "approval_resolved": OpType.APPROVAL,
    "approval_progress": OpType.APPROVAL,
    "compact_started": OpType.COMPACT,
    "compact_done": OpType.COMPACT,
    "compaction": OpType.COMPACT,
    "state:snapshot": OpType.SYSTEM,
    "turn:heartbeat": OpType.TURN,
    "connected": OpType.SYSTEM,
    "status": OpType.SYSTEM,
    "error": OpType.SYSTEM,
}


# Default ``op_state`` for a given event type — used when a legacy
# dict doesn't specify one. Migrated code passes ``op_state``
# explicitly, so this map is only a last-resort fallback.
_LEGACY_OP_STATE: dict[str, OpState] = {
    "user_message": OpState.PENDING,
    "message_queued": OpState.PENDING,
    "message_started": OpState.RUNNING,
    "message_done": OpState.COMPLETED,
    "message_cancelled": OpState.CANCELLED,
    "result": OpState.COMPLETED,
    "turn_complete": OpState.COMPLETED,
    "stream_done": OpState.COMPLETED,
    "token": OpState.RUNNING,
    "thinking": OpState.RUNNING,
    "thinking_started": OpState.RUNNING,
    "thinking_delta": OpState.RUNNING,
    "tool_start": OpState.RUNNING,
    "tool_call": OpState.COMPLETED,  # overridden to FAILED if error
    "tool_end": OpState.COMPLETED,
    "agent_spawn": OpState.RUNNING,
    "agent_progress": OpState.RUNNING,
    "agent_result": OpState.COMPLETED,  # overridden to FAILED if error
    "agent_cancel": OpState.CANCELLED,
    "approval_request": OpState.WAITING_APPROVAL,
    "approval_resolved": OpState.COMPLETED,
    "approval_progress": OpState.WAITING_APPROVAL,
    "compact_started": OpState.RUNNING,
    "compact_done": OpState.COMPLETED,
    "compaction": OpState.COMPLETED,
    "state:snapshot": OpState.COMPLETED,
    "turn:heartbeat": OpState.RUNNING,
    "connected": OpState.COMPLETED,
    "error": OpState.FAILED,
    "abort": OpState.CANCELLED,
}


def kind_for(event_type: str) -> str:
    """Public: look up the coarse ``kind`` for an event type."""
    return _KIND_MAP.get(event_type, "session")


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """Immutable envelope for every client-bound event.

    Use ``SessionEvent.build(...)`` for the common path — it fills in
    defaults (event_id, ts, kind). Use the bare constructor only when
    you need full control (tests).
    """

    type: str
    app_id: str
    session_id: str
    user_id: str
    op_id: str
    op_type: OpType
    op_state: OpState

    # Optional / defaulted
    event_id: str = field(default_factory=_gen_event_id)
    ts: str = field(default_factory=_now_iso_us)
    kind: str = ""
    correlation_id: str = ""
    op_parent_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    # Filled by the bus at publish time — do NOT set manually.
    seq: int = 0

    def __post_init__(self) -> None:
        # Fail-closed validation. Every field that is legally required
        # gets a clear error before the event goes anywhere.
        missing: list[str] = []
        if not self.type:
            missing.append("type")
        if not self.app_id:
            missing.append("app_id")
        if not self.session_id:
            missing.append("session_id")
        if not self.user_id:
            missing.append("user_id")
        if not self.op_id:
            missing.append("op_id")
        if missing:
            raise ValueError(
                f"SessionEvent: missing required field(s) {missing!r}. "
                "Every client-bound event must carry scope "
                "(app_id/session_id/user_id) and operation identity "
                "(op_id)."
            )
        if not isinstance(self.op_type, OpType):
            raise ValueError(
                f"SessionEvent.op_type must be an OpType enum "
                f"(got {type(self.op_type).__name__}={self.op_type!r})"
            )
        if not isinstance(self.op_state, OpState):
            raise ValueError(
                f"SessionEvent.op_state must be an OpState enum "
                f"(got {type(self.op_state).__name__}={self.op_state!r})"
            )
        if not self.kind:
            object.__setattr__(self, "kind", kind_for(self.type))

    # ── Factory ────────────────────────────────────────────────────

    @classmethod
    def build(
        cls,
        *,
        type: str,
        app_id: str,
        session_id: str,
        user_id: str,
        op_id: str,
        op_type: OpType,
        op_state: OpState,
        correlation_id: str = "",
        op_parent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> "SessionEvent":
        """Preferred construction path. Keyword-only so new fields can
        be added later without breaking positional callers."""
        return cls(
            type=type,
            app_id=app_id,
            session_id=session_id,
            user_id=user_id,
            op_id=op_id,
            op_type=op_type,
            op_state=op_state,
            correlation_id=correlation_id or "",
            op_parent_id=op_parent_id,
            payload=dict(payload) if payload else {},
        )

    # ── Serialisation ─────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Wire format for Socket.IO emission and DB persistence."""
        return {
            "event_id": self.event_id,
            "type": self.type,
            "kind": self.kind,
            "seq": self.seq,
            "ts": self.ts,
            "app_id": self.app_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "correlation_id": self.correlation_id or None,
            "op_id": self.op_id,
            "op_type": self.op_type.value,
            "op_state": self.op_state.value,
            "op_parent_id": self.op_parent_id,
            "payload": dict(self.payload),
        }

    def with_seq(self, seq: int) -> "SessionEvent":
        """Return a copy with the server-assigned ``seq`` filled in.

        ``SessionEvent`` is frozen, so the bus can't mutate it. This
        helper hands back a sealed copy with the attribution written.
        """
        object_copy = SessionEvent(
            type=self.type,
            app_id=self.app_id,
            session_id=self.session_id,
            user_id=self.user_id,
            op_id=self.op_id,
            op_type=self.op_type,
            op_state=self.op_state,
            event_id=self.event_id,
            ts=self.ts,
            kind=self.kind,
            correlation_id=self.correlation_id,
            op_parent_id=self.op_parent_id,
            payload=dict(self.payload),
            seq=seq,
        )
        return object_copy

    # ── Convenience ───────────────────────────────────────────────

    def is_terminal(self) -> bool:
        """True when this event marks the op as finished for good."""
        return self.op_state in TERMINAL_STATES


def gen_op_id(prefix: str) -> str:
    """Generate a prefixed, globally-unique op_id.

    The prefix is a short hint of the operation type (``turn``,
    ``tool``, ``agent``, ``approval``, ``compact``) so op_ids are
    self-describing in logs and tracebacks.
    """
    return f"op-{prefix}-{uuid.uuid4().hex[:12]}"
