"""Daemon backend - SSE client that talks to a remote Digitorn daemon.

Architecture:
  - POST /sessions/{sid}/messages  → send message (async, fire-and-forget)
  - GET  /sessions/{sid}/events    → receive ALL events (persistent SSE)

The event listener runs in a background thread and dispatches events
as Textual Messages to the TUI.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import httpx

from digitorn_cli.tui.messages import (
    TokenReceived, StreamDone, OutTokenCount, InTokenCount,
    ToolStarted, ToolCompleted, ThinkingStarted, ThinkingDelta, ThinkingReceived,
    HookFired, TurnComplete, BackendReady, BackendError,
    MemoryUpdate, AgentEvent, ApprovalRequested,
    StatusUpdate, TerminalOutput,
    Notification, NotificationResult,
    HistoryLoaded,
)

logger = logging.getLogger(__name__)


class DaemonBackend:
    """Connects to a Digitorn daemon as an SSE client.

    Uses two endpoints:
      POST /sessions/{sid}/messages → send user message (202 async)
      GET  /sessions/{sid}/events   → persistent SSE for ALL session events
    """

    def __init__(
        self,
        daemon_url: str,
        app_id: str,
        *,
        session_id: str | None = None,
        app_path: Path | None = None,
        auth_headers: dict[str, str] | None = None,
    ) -> None:
        self._daemon_url = daemon_url.rstrip("/")
        self._app_id = app_id
        self._session_id = session_id or str(uuid.uuid4())
        self._app_path = app_path  # If set, auto-deploy YAML before connecting
        self._auth_headers = auth_headers or {}
        self._app_info: dict[str, Any] = {}
        # Persistent HTTP client - reuses connections (no leak)
        self._http = httpx.Client(timeout=30.0)
        # Token counters no longer needed - raw deltas passed through to TUI

    async def initialize(self, post: Callable[..., None]) -> dict[str, Any]:
        """Fetch app info from daemon, post BackendReady.

        Runs synchronously - called from TUI's @work(thread=True) so
        Textual stays responsive.
        """
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self._do_initialize(post))
        finally:
            loop.close()

    async def _do_initialize(self, post: Callable[..., None]) -> dict[str, Any]:
        """The actual initialization (runs on its own event loop in a thread)."""
        # Auto-deploy YAML if app_path was provided
        if self._app_path is not None:
            try:
                deploy_resp = self._request(
                    "post",
                    f"{self._daemon_url}/api/apps/deploy",
                    json={"yaml_path": str(self._app_path.resolve()), "force": True},
                    timeout=30.0,
                )
                deploy_data = deploy_resp.json()
                if not deploy_data.get("success"):
                    post(BackendError(
                        f"Deploy failed: {deploy_data.get('error', 'unknown')}"
                    ))
                    return {}
                self._app_info = deploy_data["data"]
                self._app_id = self._app_info["app_id"]
            except Exception as exc:
                post(BackendError(f"Deploy failed: {exc}"))
                return {}
        else:
            # Fetch existing app info
            try:
                resp = self._request(
                    "get",
                    f"{self._daemon_url}/api/apps/{self._app_id}",
                    timeout=10.0,
                )
                if resp.status_code == 404:
                    post(BackendError(
                        f"App '{self._app_id}' not deployed on daemon."
                    ))
                    return {}
                data = resp.json()
                if not data.get("success"):
                    post(BackendError(
                        data.get("error", "Failed to fetch app info")
                    ))
                    return {}
                self._app_info = data["data"]
            except Exception as exc:
                post(BackendError(f"Cannot reach daemon: {exc}"))
                return {}

        import os
        app_info = self._app_info
        agents = app_info.get("agents", [])

        ws_mode = app_info.get("workspace_mode", "auto")
        if ws_mode == "fixed":
            ws = app_info.get("workspace", "")
        elif ws_mode == "none":
            ws = ""
        else:
            ws = os.getcwd()

        info = {
            "app_name": app_info.get("name", self._app_id),
            "agent_id": agents[0] if agents else "main",
            "mode": "daemon",
            "total_tools": app_info.get("total_tools", 0),
            "model": app_info.get("model", "?"),
            "greeting": app_info.get("greeting", ""),
            "workspace": ws,
        }

        post(BackendReady(**info))

        # If resuming an existing session, load history to restore the UI
        if self._session_id:
            self._load_history(post)

        # Start persistent SSE listener for session events
        self.start_event_listener(post)

        return info

    def _load_history(self, post: Callable[..., None]) -> None:
        """Load session history and restore the TUI state.

        Calls GET /sessions/{sid}/history to get:
        - messages (user/assistant turns)
        - events (tool_call, tool_start, thinking, etc.)
        - memory_snapshot (goal, todos, facts)
        - session metadata (interrupted, title, etc.)

        Posts a HistoryLoaded message that the TUI handles to rebuild the chat.
        """
        try:
            url = (
                f"{self._daemon_url}/api/apps/{self._app_id}"
                f"/sessions/{self._session_id}/history"
            )
            resp = self._request("get", url, timeout=15.0)
            if resp.status_code == 404:
                # New session - no history to load
                return
            if resp.status_code != 200:
                logger.warning("History load failed: HTTP %d", resp.status_code)
                return

            data = resp.json().get("data", {})
            messages = data.get("messages", [])
            events = data.get("events", [])
            memory = data.get("memory_snapshot", {})
            session_info = {
                "session_id": data.get("session_id", self._session_id),
                "title": data.get("title", ""),
                "interrupted": data.get("interrupted", False),
                "turn_count": data.get("turn_count", 0),
                "message_count": data.get("message_count", 0),
            }

            if messages:
                post(HistoryLoaded(
                    messages=messages,
                    events=events,
                    memory=memory,
                    session_info=session_info,
                ))
                logger.info(
                    "History loaded: %d messages, %d events, interrupted=%s",
                    len(messages), len(events), session_info.get("interrupted"),
                )

                # If session was interrupted, auto-resume
                if session_info.get("interrupted"):
                    self._check_and_resume(post)

        except Exception as exc:
            logger.warning("History load error: %s", exc)

    def start_event_listener(self, post: Callable[..., None]) -> None:
        """Start a background thread that listens to GET /sessions/{sid}/events.

        This persistent SSE connection receives ALL events for the session:
        token, tool_call, tool_start, result, memory_update, agent_event, etc.
        Must be called after initialize() so session_id is set.
        """
        import threading

        if getattr(self, "_event_thread", None) is not None:
            return  # Already listening

        self._event_stop = threading.Event()

        def _listen() -> None:
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._event_loop(post))
            except Exception as exc:
                logger.warning("event_listener_stopped: %s", exc)
            finally:
                loop.close()

        self._event_thread = threading.Thread(
            target=_listen, daemon=True, name="daemon-events",
        )
        self._event_thread.start()

    async def _event_loop(self, post: Callable[..., None]) -> None:
        """Persistent SSE listener with auto-reconnect + event replay + resume.

        On disconnect:
        1. Reconnect SSE with ?since=N to replay missed events
        2. Check session state - if interrupted, POST /resume to auto-continue
        3. The user never notices the interruption
        """
        _had_tokens = False
        _event_count = 0  # Track how many events we've received (for replay)
        _reconnect_count = 0

        while not self._event_stop.is_set():
            url = (
                f"{self._daemon_url}/api/apps/{self._app_id}"
                f"/sessions/{self._session_id}/events"
            )
            # On reconnect, pass ?since=N to replay missed events
            if _event_count > 0:
                url += f"?since={_event_count}"

            try:
                with httpx.stream(
                    "GET", url, timeout=None,
                    headers=self._fresh_headers(),
                ) as resp:
                    if resp.status_code == 401:
                        post(BackendError("Session expired. Run: digitorn-cli login"))
                        return
                    if resp.status_code != 200:
                        logger.warning("events_stream_error: HTTP %d", resp.status_code)
                        import time; time.sleep(2)
                        continue

                    # Successfully connected - if this was a reconnect, check for resume
                    if _reconnect_count > 0:
                        logger.info("SSE reconnected (attempt %d, replaying from %d)",
                                    _reconnect_count, _event_count)
                        self._check_and_resume(post)

                    _reconnect_count = 0
                    event_type = ""
                    data_buf = ""
                    line_buf = ""

                    for chunk in resp.iter_bytes():
                        if self._event_stop.is_set():
                            return
                        line_buf += chunk.decode("utf-8", errors="replace")

                        while "\n" in line_buf:
                            line, line_buf = line_buf.split("\n", 1)
                            line = line.rstrip("\r")

                            if line.startswith("event: "):
                                event_type = line[7:]
                            elif line.startswith("data: "):
                                data_buf = line[6:]
                            elif line == "" and event_type and data_buf:
                                if data_buf == "[DONE]":
                                    event_type = ""
                                    data_buf = ""
                                    continue

                                try:
                                    ev_data = json.loads(data_buf)
                                except (json.JSONDecodeError, ValueError):
                                    ev_data = {}

                                # Skip connected/replay_done meta-events
                                if event_type not in ("connected", "replay_done"):
                                    _had_tokens = self._dispatch_event(
                                        event_type, ev_data, post, _had_tokens,
                                    )
                                    _event_count += 1

                                if event_type == "replay_done":
                                    replayed = ev_data.get("replayed", 0)
                                    if replayed > 0:
                                        logger.info("Replayed %d missed events", replayed)

                                event_type = ""
                                data_buf = ""

            except httpx.ConnectError:
                if not self._event_stop.is_set():
                    _reconnect_count += 1
                    logger.warning("SSE disconnected, reconnecting (attempt %d)...",
                                   _reconnect_count)
                    post(BackendError("Connection lost. Reconnecting..."))
                    import time
                    time.sleep(min(3 * _reconnect_count, 30))  # Exponential backoff
            except (httpx.ReadTimeout, httpx.TimeoutException):
                if not self._event_stop.is_set():
                    _reconnect_count += 1
                    logger.warning("SSE timeout, reconnecting...")
                    import time; time.sleep(2)
            except Exception as exc:
                if not self._event_stop.is_set():
                    _reconnect_count += 1
                    logger.warning("SSE error: %s", exc)
                    import time; time.sleep(3)

    def _check_and_resume(self, post: Callable[..., None]) -> None:
        """After reconnect, check if the session was interrupted and auto-resume."""
        try:
            url = (
                f"{self._daemon_url}/api/apps/{self._app_id}"
                f"/sessions/{self._session_id}"
            )
            resp = self._http.get(url, headers=self._fresh_headers(), timeout=10)
            if resp.status_code != 200:
                return

            data = resp.json().get("data", {})
            is_active = data.get("is_active", False)
            interrupted = data.get("interrupted", False)

            if is_active:
                # Turn still running - just reconnected SSE, events will flow
                logger.info("Session active - turn in progress, SSE reconnected")
                return

            if interrupted:
                # Session was interrupted - auto-resume
                logger.info("Session interrupted - sending POST /resume")
                resume_url = (
                    f"{self._daemon_url}/api/apps/{self._app_id}"
                    f"/sessions/{self._session_id}/resume"
                )
                r = self._http.post(resume_url, headers=self._fresh_headers(), timeout=30)
                if r.status_code == 200:
                    result = r.json().get("data", {})
                    if result.get("resumed"):
                        logger.info("Session resumed - agent continuing")
                        post(StatusUpdate("requesting", {"label": "Resuming..."}))
                    else:
                        logger.info("Resume not needed: %s", result.get("reason"))
                else:
                    logger.warning("Resume failed: HTTP %d", r.status_code)

            # Neither active nor interrupted - session is idle, nothing to do
        except Exception as exc:
            logger.warning("Resume check failed: %s", exc)

    async def send_message(self, text: str, post: Callable[..., None]) -> None:
        """POST to /sessions/{sid}/messages - fire-and-forget.

        Events arrive via the persistent SSE listener (start_event_listener).
        """
        import asyncio
        import os
        import concurrent.futures

        def _send_in_thread() -> None:
            url = (
                f"{self._daemon_url}/api/apps/{self._app_id}"
                f"/sessions/{self._session_id}/messages"
            )
            payload = {"message": text, "workspace": os.getcwd()}
            try:
                resp = self._http.post(
                    url, json=payload, timeout=10.0,
                    headers=self._fresh_headers(),
                )
                if resp.status_code == 401:
                    post(BackendError("Session expired. Run: digitorn-cli login"))
                    post(TurnComplete(content="", error="Session expired"))
                elif resp.status_code not in (200, 202):
                    error_text = resp.text[:200]
                    post(BackendError(f"HTTP {resp.status_code}: {error_text}"))
                    post(TurnComplete(content="", error=f"HTTP {resp.status_code}"))
            except httpx.ConnectError:
                post(BackendError(f"Cannot connect to daemon at {self._daemon_url}"))
                post(TurnComplete(content="", error="Connection failed"))
            except Exception as exc:
                logger.error("send_message_error: %s", exc, exc_info=True)
                post(TurnComplete(content="", error=str(exc)))

        # Run in thread to avoid blocking Textual's event loop
        import threading
        t = threading.Thread(target=_send_in_thread, daemon=True, name="send-msg")
        t.start()
        self._last_send_thread = t  # Keep ref for cleanup

    def _dispatch_event(
        self,
        event_type: str,
        data: dict[str, Any],
        post: Callable[..., None],
        had_tokens: bool,
    ) -> bool:
        """Map a single SSE event to Textual Messages. Returns updated had_tokens."""

        if event_type == "token":
            delta = data.get("delta", "")
            if delta:
                post(TokenReceived(delta))
            return True

        if event_type == "stream_done":
            # Daemon signals text streaming is over - tool calls or turn end next
            post(StreamDone())
            return False

        # Fallback: if we were streaming tokens and now get a non-token event
        # without explicit stream_done, signal it (backward compatibility)
        if had_tokens and event_type not in ("token", "out_token", "in_token", "stream_done"):
            post(StreamDone())
            had_tokens = False

        if event_type == "tool_start":
            post(ToolStarted(
                data.get("name", ""),
                data.get("params", {}),
            ))

        elif event_type == "tool_call":
            name = data.get("name", "")
            params = data.get("params", {})
            result = data.get("result") or {
                "success": data.get("success", True),
                "error": data.get("error", ""),
            }
            post(ToolCompleted(name, params, result))
            # memory_update and agent_event are now sent as separate events
            # by the daemon - no need to extract them from tool_call here.

        elif event_type == "status":
            post(StatusUpdate(data.get("phase", ""), data))

        elif event_type == "terminal_output":
            post(TerminalOutput(
                data.get("stdout", ""),
                data.get("stderr", ""),
            ))

        elif event_type == "thinking_started":
            post(ThinkingStarted())

        elif event_type == "thinking_delta":
            delta = data.get("delta", "")
            if delta:
                post(ThinkingDelta(delta))

        elif event_type == "thinking":
            text = data.get("text", "")
            if text:
                # Batch mode fallback - if server didn't emit progressive events
                post(ThinkingReceived(text))

        elif event_type == "approval_request":
            post(ApprovalRequested(
                request_id=data.get("request_id", ""),
                tool_name=data.get("tool_name", ""),
                tool_params=data.get("tool_params", {}),
                risk_level=data.get("risk_level", "medium"),
                description=data.get("description", ""),
            ))

        elif event_type == "hook":
            # TUI's on_hook_fired accesses event.action_type, event.phase, event.details
            hook_event = SimpleNamespace(
                hook_id=data.get("hook_id", ""),
                action_type=data.get("action_type", ""),
                phase=data.get("phase", ""),
                details=data.get("details", {}),
            )
            post(HookFired(hook_event))

        elif event_type == "result":
            content = data.get("content", "")
            error = data.get("error")
            # Update session_id if the daemon assigned one
            sid = data.get("session_id")
            if sid:
                self._session_id = sid
            post(TurnComplete(
                content=content,
                error=error,
                usage=data.get("usage"),
                turn_number=data.get("turn_number", 0),
                context=data.get("context"),
                workspace_status=data.get("workspace_status"),
            ))

        elif event_type == "error":
            post(TurnComplete(
                content="",
                error=data.get("error", "Unknown error"),
            ))

        elif event_type == "out_token":
            count = data.get("count", 0)
            if count:
                # Send raw delta (same as standalone) - accumulation done in _post()
                post(OutTokenCount(count))

        elif event_type == "in_token":
            count = data.get("count", 0)
            if count:
                # Send raw value - in_tokens = current context size
                post(InTokenCount(count))

        elif event_type == "memory_update":
            action = data.get("action", "")
            result = data.get("result", {})
            if action and isinstance(result, dict):
                post(MemoryUpdate(action, result))

        elif event_type == "agent_event":
            agent_id = data.get("agent_id", data.get("action", ""))
            if agent_id:
                post(AgentEvent(
                    agent_id=agent_id,
                    status=data.get("status", ""),
                    specialist=data.get("specialist", ""),
                    task=data.get("task", ""),
                    duration=data.get("duration_seconds", 0),
                    preview=data.get("preview", ""),
                ))

        elif event_type == "notification":
            post(Notification(
                source=data.get("source", data.get("type", "background")),
                message=data.get("message", data.get("text", "")),
                data=data,
            ))

        elif event_type == "notification_result":
            post(NotificationResult(
                content=data.get("content", ""),
                source=data.get("source", ""),
                error=data.get("error", ""),
            ))

        return had_tokens

    @staticmethod
    def _emit_sidebar_updates(
        name: str, params: dict, result: Any, post: Callable[..., None],
    ) -> None:
        """Emit MemoryUpdate/AgentEvent from tool_call data (mirrors StandaloneBackend)."""
        from digitorn_cli.tui.backends._event_helpers import is_memory_tool, is_agent_tool, extract_result_data

        # Unwrap meta-tool
        real_name = name
        if name in ("execute_tool", "execute"):
            real_name = params.get("tool_name") or params.get("name") or name

        action = real_name.rsplit(".", 1)[-1].rsplit("__", 1)[-1]

        if is_memory_tool(real_name):
            data = extract_result_data(result)
            if not data:
                # Fallback: try parsing serialized result string
                if isinstance(result, str):
                    try:
                        import json as _json
                        parsed = _json.loads(result)
                        data = parsed.get("data", parsed) if isinstance(parsed, dict) else None
                    except Exception:
                        pass
                elif isinstance(result, dict) and "result" in result:
                    # SSE format: {"name": "...", "result": {"data": {...}}}
                    inner = result["result"]
                    data = extract_result_data(inner)
            if data:
                post(MemoryUpdate(action, data))

        if is_agent_tool(name) or is_agent_tool(real_name):
            data = extract_result_data(result)
            if data and isinstance(data, dict):
                if "cancel" in action:
                    cancelled = data.get("cancelled", [])
                    if isinstance(cancelled, list):
                        for aid in cancelled:
                            post(AgentEvent(agent_id=aid, status="cancelled"))
                    elif data.get("agent_id"):
                        post(AgentEvent(
                            agent_id=data["agent_id"], status="cancelled",
                        ))
                elif "wait_all" in action:
                    # agent_wait_all returns multiple results - emit event for each
                    results_list = data.get("results", [])
                    for r in results_list:
                        if isinstance(r, dict) and r.get("agent_id"):
                            r_status = r.get("status", "completed")
                            if r_status in ("completed", "done"):
                                r_status = "completed"
                            elif r_status in ("failed", "timeout", "error"):
                                r_status = "failed"
                            post(AgentEvent(
                                agent_id=r["agent_id"],
                                status=r_status,
                                specialist=r.get("specialist", ""),
                                task=r.get("task", "")[:60],
                                duration=r.get("duration_seconds", 0),
                                preview=(r.get("content", "") or "")[:80],
                            ))
                else:
                    status = "spawned" if "spawn" in action else data.get("status", "completed")
                    post(AgentEvent(
                        agent_id=data.get("agent_id", params.get("agent_id", "")),
                        status=status,
                        specialist=data.get("specialist", params.get("specialist", "")),
                        task=data.get("task", params.get("task", "")),
                        duration=data.get("duration_seconds", 0),
                        preview=(data.get("content", "") or data.get("content_preview", ""))[:80],
                    ))

    def resolve_approval(self, request_id: str, approved: bool, message: str = "") -> None:
        """POST approval decision to daemon."""
        try:
            self._request(
                "post",
                f"{self._daemon_url}/api/apps/{self._app_id}/approve",
                json={
                    "request_id": request_id,
                    "approved": approved,
                    "message": message,
                },
                timeout=10.0,
            )
        except Exception as exc:
            logger.warning("approval_resolve_failed: %s", exc)

    def abort(self) -> None:
        """Abort the current agent turn via the daemon API."""
        try:
            session_id = self._session_id
            if session_id:
                url = f"{self._daemon_url}/api/apps/{self._app_id}/sessions/{session_id}/abort"
                self._request("POST", url)
        except Exception:
            pass  # Best effort - don't crash on abort failure

    # ── Session management ──────────────────────────────────

    @property
    def app_id(self) -> str:
        return self._app_id

    @property
    def session_id(self) -> str:
        return self._session_id

    def list_sessions(self, limit: int = 20) -> list[dict]:
        """List sessions for the current app."""
        try:
            resp = self._request(
                "GET",
                f"{self._daemon_url}/api/apps/{self._app_id}/sessions",
                params={"limit": limit},
            )
            data = resp.json()
            return data.get("data", data.get("sessions", []))
        except Exception:
            return []

    def get_session_history(self, session_id: str) -> dict | None:
        """Get full message history for a session."""
        try:
            resp = self._request(
                "GET",
                f"{self._daemon_url}/api/apps/{self._app_id}/sessions/{session_id}/history",
            )
            data = resp.json()
            return data.get("data", data)
        except Exception:
            return None

    def resume_session(self, session_id: str) -> bool:
        """Switch to an existing session. Messages will be loaded on next send."""
        self._session_id = session_id
        return True

    def fork_session(self, new_session_id: str | None = None) -> dict | None:
        """Fork the current session into a new one."""
        try:
            payload: dict = {}
            if new_session_id:
                payload["new_session_id"] = new_session_id
            resp = self._request(
                "POST",
                f"{self._daemon_url}/api/sessions/{self._session_id}/fork",
                json=payload,
            )
            data = resp.json()
            if data.get("success"):
                return data.get("data", data)
            return None
        except Exception:
            return None

    # ── MCP management ──────────────────────────────────────

    def list_mcp_servers(self) -> list[dict]:
        """List MCP servers configured for the current app."""
        try:
            resp = self._request(
                "GET",
                f"{self._daemon_url}/api/apps/{self._app_id}/mcp/servers",
            )
            data = resp.json()
            return data.get("data", data.get("servers", []))
        except Exception:
            return []

    def mcp_health(self) -> list[dict]:
        """Health check all MCP servers for the current app."""
        try:
            resp = self._request(
                "POST",
                f"{self._daemon_url}/api/apps/{self._app_id}/mcp/health",
            )
            data = resp.json()
            return data.get("data", data.get("servers", []))
        except Exception:
            return []

    def get_app_info(self) -> dict:
        """Get full app info from daemon (model, tools, agents)."""
        try:
            resp = self._request(
                "GET",
                f"{self._daemon_url}/api/apps/{self._app_id}",
            )
            data = resp.json()
            return data.get("data", data) if data.get("success") else self._app_info
        except Exception:
            return self._app_info

    def get_index_info(self) -> dict:
        """Get tool index info (context_window, tool_injection_mode)."""
        try:
            resp = self._request(
                "GET",
                f"{self._daemon_url}/api/apps/{self._app_id}/index",
            )
            data = resp.json()
            return data.get("data", data) if data.get("success") else {}
        except Exception:
            return {}

    def get_session_info(self) -> dict:
        """Get current session metadata (message_count, turns)."""
        try:
            resp = self._request(
                "GET",
                f"{self._daemon_url}/api/apps/{self._app_id}/sessions/{self._session_id}",
            )
            data = resp.json()
            return data.get("data", data) if data.get("success") else {}
        except Exception:
            return {}

    def undo_last(self) -> dict | None:
        """Undo the last file edit via daemon."""
        try:
            resp = self._request(
                "POST",
                f"{self._daemon_url}/api/apps/{self._app_id}/sessions/{self._session_id}/undo",
            )
            data = resp.json()
            return data.get("data") if data.get("success") else None
        except Exception:
            return None

    def compact(self) -> dict | None:
        """Trigger context compaction via daemon."""
        try:
            resp = self._request(
                "POST",
                f"{self._daemon_url}/api/apps/{self._app_id}/sessions/{self._session_id}/compact",
                timeout=30.0,
            )
            data = resp.json()
            return data.get("data") if data.get("success") else None
        except Exception:
            return None

    def diagnostics(self) -> list[tuple[str, bool, str]]:
        """Fetch diagnostics from daemon."""
        try:
            resp = self._request(
                "GET",
                f"{self._daemon_url}/api/apps/{self._app_id}/diagnostics",
            )
            data = resp.json()
            if data.get("success"):
                return [
                    (c.get("name", "?"), c.get("ok", False), c.get("detail", ""))
                    for c in data.get("data", {}).get("checks", [])
                ]
        except Exception:
            pass
        return [("Daemon", True, self._daemon_url)]

    @property
    def workspace_path(self) -> str:
        return self._app_info.get("workspace", "")

    async def shutdown(self) -> None:
        """Stop the event listener thread and close HTTP client."""
        stop = getattr(self, "_event_stop", None)
        if stop is not None:
            stop.set()
        thread = getattr(self, "_event_thread", None)
        if thread is not None:
            thread.join(timeout=3)
            self._event_thread = None
        # Close the persistent HTTP client
        try:
            self._http.close()
        except Exception:
            pass

    # ── HTTP helpers ──────────────────────────────────────

    def _fresh_headers(self) -> dict[str, str]:
        """Get fresh auth headers with silent refresh (no interactive prompt).

        Called from background threads - must NEVER block on user input.
        If token expired, tries silent refresh. If that fails, returns
        stale headers (caller handles the 401).
        """
        try:
            from digitorn_cli.auth import (
                _load_credentials, _refresh_token, _is_token_expired,
            )
            creds = _load_credentials()
            if creds is None:
                return dict(self._auth_headers)

            if _is_token_expired(creds):
                refreshed = _refresh_token(self._daemon_url, creds)
                if refreshed:
                    self._auth_headers = {
                        "Authorization": f"Bearer {refreshed['access_token']}"
                    }
                # If refresh fails, return stale - caller handles 401
                return dict(self._auth_headers)

            self._auth_headers = {
                "Authorization": f"Bearer {creds['access_token']}"
            }
            return dict(self._auth_headers)
        except Exception:
            return dict(self._auth_headers)

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Make an authenticated HTTP request to the daemon.

        Refreshes auth headers before each request.
        """
        headers = self._fresh_headers()
        if "headers" in kwargs:
            kwargs["headers"].update(headers)
        else:
            kwargs["headers"] = dict(headers)

        if "timeout" not in kwargs:
            kwargs["timeout"] = 10.0

        return getattr(self._http, method)(url, **kwargs)
