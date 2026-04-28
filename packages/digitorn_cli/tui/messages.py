"""Textual Message subclasses - the callback bridge.

Runtime callbacks (on_token, on_tool_call, etc.) post these Messages
to the TUI's event loop. Widget handlers update the display.
"""

from __future__ import annotations

from typing import Any

from textual.message import Message


class TokenReceived(Message):
    """A streaming text token from the LLM."""

    def __init__(self, delta: str) -> None:
        super().__init__()
        self.delta = delta


class StreamDone(Message):
    """Text streaming completed. Finalize as Markdown."""


class OutTokenCount(Message):
    """Output token counter update."""

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count


class InTokenCount(Message):
    """Input token counter update."""

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count


class ToolStarted(Message):
    """A tool has started executing."""

    def __init__(self, name: str, params: dict[str, Any]) -> None:
        super().__init__()
        self.name = name
        self.params = params


class ToolCompleted(Message):
    """A tool has finished (success or error)."""

    def __init__(self, name: str, params: dict[str, Any], result: Any) -> None:
        super().__init__()
        self.name = name
        self.params = params
        self.result = result


class ThinkingStarted(Message):
    """LLM entered a <think> block - spinner should show 'thinking'."""


class ThinkingDelta(Message):
    """Streaming chunk of thinking text (arrives progressively)."""

    def __init__(self, delta: str) -> None:
        super().__init__()
        self.delta = delta


class ThinkingReceived(Message):
    """Complete thinking block (when </think> found)."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class HookFired(Message):
    """A system hook event (compaction, injection, etc.)."""

    def __init__(self, event: Any) -> None:
        super().__init__()
        self.event = event


class TurnComplete(Message):
    """Agent turn finished - final response content with daemon-provided metrics."""

    def __init__(self, content: str, error: str | None = None,
                 usage: dict | None = None, turn_number: int = 0,
                 context: dict | None = None,
                 workspace_status: dict | None = None) -> None:
        super().__init__()
        self.content = content
        self.error = error
        self.usage = usage or {}
        self.turn_number = turn_number
        self.context = context or {}
        self.workspace_status = workspace_status or {}


class BackendReady(Message):
    """Backend finished initialization."""

    def __init__(self, app_name: str, agent_id: str, mode: str,
                 total_tools: int, model: str, greeting: str = "",
                 workspace: str = "") -> None:
        super().__init__()
        self.app_name = app_name
        self.agent_id = agent_id
        self.mode = mode
        self.total_tools = total_tools
        self.model = model
        self.greeting = greeting
        self.workspace = workspace


class BackendError(Message):
    """Backend encountered an error."""

    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error


class MemoryUpdate(Message):
    """Memory state changed (goal, todos, facts, etc.)."""

    def __init__(self, action: str, result: dict) -> None:
        super().__init__()
        self.action = action
        self.result = result


class ApprovalRequested(Message):
    """A tool call requires user approval before execution."""

    def __init__(self, request_id: str, tool_name: str, tool_params: dict,
                 risk_level: str, description: str) -> None:
        super().__init__()
        self.request_id = request_id
        self.tool_name = tool_name
        self.tool_params = tool_params
        self.risk_level = risk_level
        self.description = description


class AgentEvent(Message):
    """Sub-agent status update."""

    def __init__(self, agent_id: str, status: str, specialist: str = "",
                 task: str = "", duration: float = 0, preview: str = "") -> None:
        super().__init__()
        self.agent_id = agent_id
        self.status = status
        self.specialist = specialist
        self.task = task
        self.duration = duration
        self.preview = preview


class StatusUpdate(Message):
    """Daemon status phase update (turn_start, requesting, tool_use, turn_end)."""

    def __init__(self, phase: str, details: dict | None = None) -> None:
        super().__init__()
        self.phase = phase
        self.details = details or {}


class TerminalOutput(Message):
    """Shell command output (stdout/stderr)."""

    def __init__(self, stdout: str, stderr: str = "") -> None:
        super().__init__()
        self.stdout = stdout
        self.stderr = stderr


class Notification(Message):
    """Background task or watcher notification."""

    def __init__(self, source: str, message: str, data: dict | None = None) -> None:
        super().__init__()
        self.source = source      # "watcher" or "background"
        self.message = message
        self.data = data or {}


class NotificationResult(Message):
    """Auto-triggered agent turn result from a notification."""

    def __init__(self, content: str, source: str = "", error: str = "") -> None:
        super().__init__()
        self.content = content
        self.source = source
        self.error = error


class HistoryLoaded(Message):
    """Session history loaded - restore the chat UI.

    Contains messages, events, memory snapshot, and session metadata.
    """

    def __init__(self, messages: list, events: list,
                 memory: dict | None = None,
                 session_info: dict | None = None) -> None:
        super().__init__()
        self.messages = messages      # [{role, content, ...}]
        self.events = events          # [{type, data, ...}]
        self.memory = memory or {}    # {goal, todos, facts}
        self.session_info = session_info or {}
