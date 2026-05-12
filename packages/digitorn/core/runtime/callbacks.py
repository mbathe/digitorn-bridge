"""Typed callback protocols for the agent loop.

Every callback the runtime accepts is defined here as a Protocol.
AgentTurnCallbacks bundles them into a single object, replacing
the 10+ keyword arguments previously threaded through agent_turn → _loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class OnToken(Protocol):
    def __call__(self, delta: str, count: int = 0) -> None: ...


@runtime_checkable
class OnStreamDone(Protocol):
    def __call__(self) -> None: ...


@runtime_checkable
class OnOutToken(Protocol):
    def __call__(self, count: int) -> None: ...


@runtime_checkable
class OnInToken(Protocol):
    def __call__(self, count: int) -> None: ...


@runtime_checkable
class OnToolStart(Protocol):
    async def __call__(self, name: str, params: dict[str, Any], call_id: str) -> None: ...


@runtime_checkable
class OnToolCall(Protocol):
    async def __call__(
        self, name: str, params: dict[str, Any], result: Any, call_id: str,
    ) -> None: ...


@runtime_checkable
class OnToolCallStreaming(Protocol):
    """Fired while the LLM is still composing a tool call's args JSON
    - BEFORE execution. Lets the UI show a live placeholder ("Write
    · 47 tokens") so the user knows the agent is working even on a
    long write where args take seconds to generate.

    Fires once when the tool name first appears (count=0), then every
    ~250ms with the litellm-tokenized count of accumulated args.
    Stops when the tool call is finalized - execution then takes over
    via the existing ``OnToolStart`` / ``OnToolCall`` callbacks.

    ``intent`` is the verb phrase extracted from the partial args buffer
    as soon as the schema's first property (the injected ``intent``
    field) closes its string literal. Empty until then; sticky once
    captured so subsequent ticks don't overwrite it. Only populated
    when the app has ``ui.tool_calls.inject_intent: true``.
    """
    def __call__(self, call_id: str, name: str, count: int, intent: str = "") -> None: ...


@runtime_checkable
class OnThinking(Protocol):
    def __call__(self, text: str, count: int = 0) -> None: ...


@runtime_checkable
class OnThinkingStarted(Protocol):
    def __call__(self) -> None: ...


@runtime_checkable
class OnThinkingDelta(Protocol):
    def __call__(self, delta: str, count: int = 0) -> None: ...


@runtime_checkable
class OnStatus(Protocol):
    """Lifecycle status callback - emitted at every phase transition.

    Phases: turn_start, requesting, generating, tool_executing,
    turn_end, error, waiting, heartbeat.
    """
    def __call__(self, phase: str, details: dict[str, Any] | None = None) -> None: ...


@dataclass
class AgentTurnCallbacks:
    """All optional callbacks for a single agent turn.

    Pass one instance to agent_turn() instead of 10 keyword arguments.
    Every field defaults to None (disabled).
    """

    on_token: OnToken | None = None
    on_stream_done: OnStreamDone | None = None
    on_out_token: OnOutToken | None = None
    on_in_token: OnInToken | None = None
    on_tool_start: OnToolStart | None = None
    on_tool_call: OnToolCall | None = None
    on_tool_call_streaming: OnToolCallStreaming | None = None
    on_thinking: OnThinking | None = None
    on_thinking_started: OnThinkingStarted | None = None
    on_thinking_delta: OnThinkingDelta | None = None
    on_status: OnStatus | None = None
    hook_runner: Any = None

    @classmethod
    def none(cls) -> AgentTurnCallbacks:
        """Return callbacks with everything disabled."""
        return cls()
