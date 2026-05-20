"""TUIBackend protocol - the contract between the TUI and ANY backend."""

from __future__ import annotations

from typing import Any, Callable, Protocol


class TUIBackend(Protocol):
    """Backend that bridges a Digitorn daemon to Textual Messages."""


    async def initialize(self, post: Callable[..., None]) -> dict[str, Any]:
        """Bootstrap: fetch app info, post BackendReady."""
        ...

    async def send_message(self, text: str, post: Callable[..., None]) -> None:
        """Send a user message."""
        ...

    async def shutdown(self) -> None:
        """Clean up resources (close HTTP connections, etc.)."""
        ...


    def abort(self) -> None:
        """Abort the current agent turn."""
        ...

    def resolve_approval(self, request_id: str, approved: bool,
                         message: str = "") -> None:
        """Approve or deny a pending tool approval request."""
        ...


    @property
    def session_id(self) -> str:
        """Current session ID."""
        ...

    @property
    def app_id(self) -> str:
        """Current app ID."""
        ...

    @property
    def workspace_path(self) -> str:
        """Workspace directory path."""
        ...

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """List available sessions."""
        ...

    def resume_session(self, session_id: str) -> bool:
        """Switch to an existing session."""
        ...

    def fork_session(self, new_session_id: str | None = None) -> dict | None:
        """Fork current session into a new one."""
        ...

    def get_session_history(self, session_id: str) -> dict | None:
        """Get full message history for a session."""
        ...


    def get_app_info(self) -> dict:
        """Get full app info (model, tools, agents, capabilities)."""
        ...

    def get_session_info(self) -> dict:
        """Get current session metadata (message_count, turns)."""
        ...

    def get_index_info(self) -> dict:
        """Get tool index info (context_window, tool_injection_mode)."""
        ...

    def list_mcp_servers(self) -> list[dict]:
        """List MCP servers configured for the app."""
        ...

    def mcp_health(self) -> list[dict]:
        """Health check all MCP servers."""
        ...


    def compact(self) -> dict | None:
        """Trigger context compaction."""
        ...

    def undo_last(self) -> dict | None:
        """Undo the last file edit."""
        ...

    def diagnostics(self) -> list[tuple[str, bool, str]]:
        """Run system diagnostics."""
        ...
