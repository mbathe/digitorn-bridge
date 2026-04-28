"""DevClient - the main testing interface.

Like a human using the Flutter client, but programmable.
Handles: deploy, credentials, sessions, multi-turn chat,
auto-approval, history inspection, abort/resume.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from digitorn.testing.models import AppHandle, LiveEvent, OnLiveEvent, SessionHandle, ToolCall, TurnResult

logger = logging.getLogger(__name__)


def _resolve_default_daemon_url() -> str:
    """Daemon base URL - env override first, then live settings, else 8000.

    Lets a dev/test daemon on a non-default port wire its own internal
    DevClient self-calls to itself instead of assuming localhost:8000.
    """
    env = os.environ.get("DIGITORN_DEV_DAEMON_URL") or os.environ.get("DIGITORN_DAEMON_URL")
    if env:
        return env.rstrip("/")
    try:
        from digitorn.core.config import get_settings
        s = get_settings()
        host = getattr(s.server, "host", "127.0.0.1") or "127.0.0.1"
        port = int(getattr(s.server, "port", 8000) or 8000)
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{port}"
    except Exception:
        return "http://127.0.0.1:8000"


_DEFAULT_DAEMON = _resolve_default_daemon_url()
_DEFAULT_TIMEOUT = 3600.0
_POLL_INTERVAL = 1.0


class DevClientError(Exception):
    """Raised when an API call fails."""
    pass


class DevClient:
    """HTTP client that talks to the Digitorn daemon like a human.

    Usage:
        client = DevClient()
        app = client.deploy("path/to/app.yaml")
        session = app.chat("hello", workspace="/path/to/project")
        print(session.last.text)
    """

    def __init__(
        self,
        daemon_url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        auto_approve: bool = True,
        on_event: OnLiveEvent | None = None,
        token: str | None = None,
        refresh_token: str | None = None,
    ) -> None:
        if daemon_url is None:
            daemon_url = _resolve_default_daemon_url()
        self.daemon_url = daemon_url.rstrip("/")
        self.timeout = timeout
        self.auto_approve = auto_approve
        self.on_event = on_event
        self._token = token
        self._refresh_token = refresh_token
        self._ensure_connected()

    @classmethod
    def with_token(cls, token: str, daemon_url: str = _DEFAULT_DAEMON, **kwargs: Any) -> "DevClient":
        return cls(daemon_url=daemon_url, token=token, **kwargs)

    @classmethod
    def with_user(
        cls,
        email: str,
        password: str,
        daemon_url: str = _DEFAULT_DAEMON,
        register_if_missing: bool = False,
        **kwargs: Any,
    ) -> "DevClient":
        """Return a client authenticated as this user (login, or register+login)."""
        anon = cls(daemon_url=daemon_url, **kwargs)
        try:
            tokens = anon.login(email, password)
        except DevClientError:
            if not register_if_missing:
                raise
            tokens = anon.register(email, password)
        return cls(
            daemon_url=daemon_url,
            token=tokens.get("access_token"),
            refresh_token=tokens.get("refresh_token"),
            **kwargs,
        )

    def _ensure_connected(self) -> None:
        try:
            r = self._get("/api/apps")
            if r.status_code != 200:
                raise DevClientError(f"Daemon returned {r.status_code}")
        except httpx.ConnectError:
            raise DevClientError(
                f"Cannot connect to daemon at {self.daemon_url}. "
                f"Start it with: py -3.12 -m digitorn serve"
            )

    def _headers(self) -> dict[str, str]:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def _http(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.daemon_url}{path}"
        kwargs.setdefault("timeout", self.timeout)
        if self._token is not None:
            headers = kwargs.pop("headers", {}) or {}
            headers.update(self._headers())
            kwargs["headers"] = headers
            try:
                with httpx.Client(timeout=kwargs.pop("timeout")) as c:
                    return c.request(method.upper(), url, **kwargs)
            except httpx.ConnectError as e:
                raise DevClientError(f"Daemon unreachable: {method.upper()} {path}") from e
        try:
            from digitorn.core.cli.auth_helpers import daemon_request
            return daemon_request(method, url, **kwargs)
        except SystemExit:
            raise DevClientError(f"Daemon unreachable: {method.upper()} {path}")

    def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._http("get", path, **kwargs)

    def _post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._http("post", path, **kwargs)

    def _put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._http("put", path, **kwargs)

    def _patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._http("patch", path, **kwargs)

    def _delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._http("delete", path, **kwargs)

    # ── Auth ────────────────────────────────────────────────────

    def login(self, email: str, password: str) -> dict[str, Any]:
        r = self._post("/auth/login", json={"email": email, "password": password})
        if r.status_code != 200:
            raise DevClientError(f"login failed: {r.status_code} {r.text[:200]}")
        data = r.json()
        self._token = data.get("access_token")
        self._refresh_token = data.get("refresh_token")
        return data

    def register(self, email: str, password: str, name: str | None = None,
                 username: str | None = None) -> dict[str, Any]:
        # Endpoint requires `username`. We derive it from email when the
        # caller didn't pass one explicitly (keeps the old API shape).
        uname = username or name or email.split("@", 1)[0]
        body: dict[str, Any] = {
            "username": uname,
            "email": email,
            "password": password,
        }
        if name:
            body["name"] = name
        r = self._post("/auth/register", json=body)
        if r.status_code != 200:
            raise DevClientError(f"register failed: {r.status_code} {r.text[:200]}")
        data = r.json()
        self._token = data.get("access_token")
        self._refresh_token = data.get("refresh_token")
        return data

    def logout(self) -> bool:
        r = self._post("/auth/logout")
        self._token = None
        self._refresh_token = None
        return r.status_code == 200

    def refresh_auth(self) -> dict[str, Any]:
        if not self._refresh_token:
            raise DevClientError("no refresh_token available")
        r = self._post("/auth/refresh", json={"refresh_token": self._refresh_token})
        if r.status_code != 200:
            raise DevClientError(f"refresh failed: {r.status_code}")
        data = r.json()
        self._token = data.get("access_token") or self._token
        self._refresh_token = data.get("refresh_token") or self._refresh_token
        return data

    def get_me(self) -> dict[str, Any]:
        r = self._get("/auth/me")
        return r.json() if r.status_code == 200 else {}

    @property
    def token(self) -> str | None:
        return self._token

    # ── Deploy ──────────────────────────────────────────────────

    def deploy(
        self,
        yaml_path: str | Path,
        force: bool = True,
        wait: float = 10.0,
    ) -> AppHandle:
        """Deploy an app from a YAML file.

        Args:
            yaml_path: Path to app.yaml (absolute or relative to cwd).
            force: Redeploy if already deployed.
            wait: Seconds to wait for deploy to complete.

        Returns:
            AppHandle with app info.
        """
        abs_path = str(Path(yaml_path).resolve())
        r = self._post("/api/apps/deploy", json={
            "yaml_path": abs_path,
            "force": force,
        })
        data = r.json()
        if not data.get("success"):
            raise DevClientError(f"Deploy failed: {data.get('error', data)}")

        app_id = data.get("data", {}).get("app_id", "")
        if not app_id:
            raise DevClientError(f"Deploy returned no app_id: {data}")

        # Wait for deploy to complete
        time.sleep(min(wait, 3.0))

        return self.get_app(app_id)

    def get_app(self, app_id: str) -> AppHandle:
        """Get info about a deployed app."""
        r = self._get(f"/api/apps/{app_id}")
        data = r.json().get("data", {})
        return AppHandle(
            app_id=app_id,
            daemon_url=self.daemon_url,
            status=data.get("status", "deployed"),
            mode=data.get("mode", ""),
            agents=data.get("agents", []),
            total_tools=data.get("total_tools", 0),
        )

    def undeploy(self, app_id: str) -> bool:
        """Undeploy an app."""
        r = self._post(f"/api/apps/{app_id}/undeploy")
        return r.json().get("success", False)

    def list_apps(self) -> list[dict[str, Any]]:
        """List all deployed apps."""
        r = self._get("/api/apps")
        return r.json().get("data", [])

    # ── Credentials ─────────────────────────────────────────────

    def set_credential(self, app_id: str, key: str, value: str) -> bool:
        """Set a credential for an app (e.g. API key)."""
        r = self._post(f"/api/credentials/{app_id}", json={
            "key": key,
            "value": value,
        })
        return r.json().get("success", False)

    def get_required_secrets(self, app_id: str) -> list[dict[str, Any]]:
        """Get required secrets for an app."""
        r = self._get(f"/api/apps/{app_id}/required-secrets")
        return r.json().get("data", {}).get("required", [])

    # ── Sessions ────────────────────────────────────────────────

    def create_session(
        self,
        app_id: str,
        workspace: str = "",
        session_id: str | None = None,
    ) -> SessionHandle:
        """Create a new conversation session."""
        sid = session_id or f"test-{uuid.uuid4().hex[:8]}"
        return SessionHandle(
            session_id=sid,
            app_id=app_id,
            daemon_url=self.daemon_url,
            workspace=workspace or str(Path.cwd()),
        )

    def chat(
        self,
        app_id: str,
        message: str,
        workspace: str = "",
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> SessionHandle:
        """One-shot: create a session, send a message, return the session.

        This is the simplest way to test an app:
            session = client.chat("my-app", "hello", workspace="/path")
            print(session.last.text)
        """
        session = self.create_session(app_id, workspace, session_id)
        self.send(session, message, timeout=timeout)
        return session

    def send(
        self,
        session: SessionHandle,
        message: str,
        timeout: float | None = None,
        on_event: OnLiveEvent | None = None,
    ) -> TurnResult:
        """Send a message to a session and wait for the response.

        Handles auto-approval, polling, live event streaming, and result extraction.

        Args:
            session: The session to send to.
            message: The message text.
            timeout: Max wait time in seconds.
            on_event: Optional callback for live events (tool calls, text, behavior).
                      If not set, uses the client-level on_event.

        Returns a TurnResult with the agent's response and all live events.
        """
        if session._closed:
            raise DevClientError("Session is closed")

        timeout = timeout or self.timeout
        callback = on_event or self.on_event
        t0 = time.monotonic()
        live_events: list[LiveEvent] = []

        def _emit(evt: LiveEvent) -> None:
            live_events.append(evt)
            if callback:
                try:
                    callback(evt)
                except Exception:
                    pass

        # Send message
        r = self._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/messages",
            json={"message": message, "workspace": session.workspace},
        )
        if r.status_code not in (200, 202):
            return TurnResult(
                success=False,
                error=f"POST failed: {r.status_code} {r.json().get('error', '')}",
            )

        # Track what we've already seen (to emit only new events)
        initial_msg_count = self._get_message_count(session)
        seen_msg_count = initial_msg_count
        seen_tool_ids: set[str] = set()

        while time.monotonic() - t0 < timeout:
            # Auto-approve pending requests
            if self.auto_approve:
                approved = self._auto_approve(session.app_id)
                if approved > 0:
                    _emit(LiveEvent(type="approval", data={"count": approved}))

            # Check for new messages (live progress)
            try:
                rh = self._get(
                    f"/api/apps/{session.app_id}/sessions/{session.session_id}/history"
                )
                if rh.status_code == 200:
                    msgs = rh.json().get("data", {}).get("messages", [])
                    # Emit events for new messages we haven't seen
                    for m in msgs[seen_msg_count:]:
                        role = m.get("role", "")
                        content = str(m.get("content", ""))
                        tcs = m.get("tool_calls", [])

                        if role == "assistant":
                            # Emit tool calls
                            for tc in tcs:
                                tc_id = tc.get("id", "")
                                if tc_id and tc_id not in seen_tool_ids:
                                    seen_tool_ids.add(tc_id)
                                    fn = tc.get("function", {})
                                    _emit(LiveEvent(type="tool_call", data={
                                        "name": fn.get("name", "?"),
                                        "arguments": fn.get("arguments", {}),
                                    }))
                            # Emit text
                            if content and content.strip():
                                _emit(LiveEvent(type="text", data={"content": content[:500]}))

                        elif role == "tool":
                            _emit(LiveEvent(type="tool_result", data={
                                "content": content[:200],
                                "tool_call_id": m.get("tool_call_id", ""),
                            }))

                        elif role == "system" and "BEHAVIOR" in content:
                            _emit(LiveEvent(type="behavior", data={"content": content[:300]}))

                    seen_msg_count = len(msgs)
            except Exception:
                pass

            # Check session status (is the turn done?)
            try:
                r = self._get(f"/api/apps/{session.app_id}/sessions/{session.session_id}")
                if r.status_code == 404:
                    # Session doesn't exist server-side - stop polling
                    return TurnResult(
                        success=False,
                        error=f"Session {session.session_id} not found (404)",
                        duration_seconds=time.monotonic() - t0,
                        live_events=live_events,
                    )
                if r.status_code == 200:
                    data = r.json().get("data", {})
                    if data.get("is_active") is False and data.get("turn_count", 0) > 0:
                        break
            except Exception:
                pass

            time.sleep(_POLL_INTERVAL)
        else:
            return TurnResult(
                success=False,
                error=f"Timeout after {timeout}s",
                duration_seconds=time.monotonic() - t0,
                live_events=live_events,
            )

        duration = time.monotonic() - t0

        # Extract result from history (only new messages from this turn)
        result = self._extract_turn_result(session, initial_msg_count, duration)
        result.live_events = live_events
        session.turns.append(result)
        return result

    def abort(self, session: SessionHandle) -> dict[str, Any]:
        """Abort the current agent turn."""
        r = self._post(f"/api/apps/{session.app_id}/sessions/{session.session_id}/abort")
        return r.json().get("data", {})

    def resume(self, session: SessionHandle) -> TurnResult:
        """Resume an interrupted session."""
        r = self._post(f"/api/apps/{session.app_id}/sessions/{session.session_id}/resume")
        if r.json().get("data", {}).get("resumed"):
            return self.send(session, "")  # Empty message triggers the resume turn
        return TurnResult(success=True, text="(nothing to resume)")

    def close_session(self, session: SessionHandle) -> None:
        """Close a session (cleanup)."""
        session._closed = True

    def delete_session(self, session: SessionHandle) -> bool:
        """Delete a session permanently.

        Uses `_delete` (the SDK's standard authenticated DELETE helper)
        so the user's token and header scheme stay consistent with the
        other verbs. The previous implementation did a POST-with-
        `_method: DELETE` pseudo-tunnel and fell through to a second
        attempt via `daemon_request` whose auth cache didn't match the
        caller's token - failures were swallowed silently.
        """
        try:
            r = self._delete(
                f"/api/apps/{session.app_id}/sessions/{session.session_id}",
            )
        except DevClientError:
            raise
        except Exception:
            return False
        ok = False
        try:
            data = r.json()
            ok = bool(
                r.status_code in (200, 204)
                and (data.get("success") is not False)
            )
        except Exception:
            ok = r.status_code in (200, 204)
        if ok:
            session._closed = True
        return ok

    def get_history(self, session: SessionHandle, include_system: bool = True) -> list[dict[str, Any]]:
        """Get full message history for a session.

        Args:
            include_system: If True (default for dev SDK), includes system messages
                like behavior directives and violations. Flutter clients get False.
        """
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/history",
            params={"include_system": "true"} if include_system else {},
        )
        return r.json().get("data", {}).get("messages", [])

    def get_events(self, session: SessionHandle) -> list[dict[str, Any]]:
        """Get event log for a session."""
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/history"
        )
        return r.json().get("data", {}).get("events", [])

    # ── Approvals & Responses ──────────────────────────────────

    def get_pending(self, app_id: str) -> list[dict[str, Any]]:
        """Get all pending approval/ask_user requests."""
        r = self._get(f"/api/apps/{app_id}/approvals")
        return r.json().get("data", {}).get("pending", [])

    def approve(self, app_id: str, request_id: str, response: str = "") -> bool:
        """Approve a pending request (tool call or ask_user).

        For tool approvals: just approve (response can be empty).
        For ask_user: send the user's response/choice in the response field.
        """
        r = self._post(f"/api/apps/{app_id}/approve", json={
            "request_id": request_id,
            "approved": True,
            "message": response,
        })
        return r.json().get("success", False)

    def deny(self, app_id: str, request_id: str, reason: str = "") -> bool:
        """Deny a pending request."""
        r = self._post(f"/api/apps/{app_id}/approve", json={
            "request_id": request_id,
            "approved": False,
            "message": reason,
        })
        return r.json().get("success", False)

    def respond_to_ask(self, app_id: str, request_id: str, answer: str) -> bool:
        """Respond to an ask_user question. Alias for approve with a response."""
        return self.approve(app_id, request_id, response=answer)

    # ── Validate ────────────────────────────────────────────────

    def validate_yaml(self, yaml_path: str | Path) -> dict[str, Any]:
        """Validate an app YAML without deploying.

        Returns {"valid": True} or {"valid": False, "errors": [...]}.
        """
        abs_path = str(Path(yaml_path).resolve())
        r = self._post("/api/apps/validate", json={"yaml_path": abs_path})
        data = r.json()
        if data.get("success"):
            return {"valid": True, "data": data.get("data", {})}
        return {"valid": False, "errors": [data.get("error", "Unknown error")]}

    # ── Secrets ─────────────────────────────────────────────────

    def set_secret(self, app_id: str, key: str, value: str) -> bool:
        """Set a secret for an app."""
        r = self._post(f"/api/apps/{app_id}/secrets/{key}",
            json={"value": value})
        if r.status_code in (200, 201):
            return r.json().get("success", True)
        # Try PUT
        try:
            from digitorn.core.cli.auth_helpers import daemon_request
            r = daemon_request("put",
                f"{self.daemon_url}/api/apps/{app_id}/secrets/{key}",
                json={"value": value})
            return r.json().get("success", False)
        except Exception:
            return False

    def get_secrets(self, app_id: str) -> dict[str, Any]:
        """List all secrets for an app (values masked)."""
        r = self._get(f"/api/apps/{app_id}/secrets")
        return r.json().get("data", {})

    # ── Session Memory ──────────────────────────────────────────

    def get_memory(self, session: SessionHandle) -> dict[str, Any]:
        """Get the memory state for a session (goal, todos, facts)."""
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/memory"
        )
        return r.json().get("data", {})

    def get_tasks(self, session: SessionHandle) -> list[dict[str, Any]]:
        """Get the task list for a session."""
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/tasks"
        )
        return r.json().get("data", {}).get("tasks", [])

    # ── Tool Index ──────────────────────────────────────────────

    def get_tool_categories(self, app_id: str) -> list[dict[str, Any]]:
        """Get all tool categories for an app."""
        r = self._get(f"/api/apps/{app_id}/tools/categories")
        return r.json().get("data", {}).get("categories", [])

    def search_tools(self, app_id: str, query: str) -> list[dict[str, Any]]:
        """Search tools by name/description."""
        r = self._get(f"/api/apps/{app_id}/tools/search",
            params={"q": query})
        return r.json().get("data", {}).get("results", [])

    def get_tool(self, app_id: str, tool_name: str) -> dict[str, Any]:
        """Get full tool schema by name."""
        r = self._get(f"/api/apps/{app_id}/tools/{tool_name}")
        return r.json().get("data", {})

    # ── One-Shot Run ────────────────────────────────────────────

    def run_oneshot(
        self,
        app_id: str,
        input_text: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run a one-shot app and get the result.

        For apps with mode: one_shot. Blocks until complete.
        """
        timeout = timeout or self.timeout
        r = self._post(f"/api/apps/{app_id}/run",
            json={"input": input_text},
            timeout=timeout)
        return r.json().get("data", {})

    # ── Background Sessions ─────────────────────────────────────

    def create_background_session(
        self,
        app_id: str,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a background session (for background mode apps)."""
        body: dict[str, Any] = {}
        if message:
            body["message"] = message
        if payload:
            body["payload"] = payload
        r = self._post(f"/api/apps/{app_id}/background-sessions", json=body)
        return r.json().get("data", {})

    def list_background_sessions(self, app_id: str) -> list[dict[str, Any]]:
        """List background sessions."""
        r = self._get(f"/api/apps/{app_id}/background-sessions")
        return r.json().get("data", {}).get("sessions", [])

    def get_background_session(self, app_id: str, bg_session_id: str) -> dict[str, Any]:
        """Get a background session's status and history."""
        r = self._get(f"/api/apps/{app_id}/background-sessions/{bg_session_id}")
        return r.json().get("data", {})

    # ── Triggers ────────────────────────────────────────────────

    def get_triggers(self, app_id: str) -> list[dict[str, Any]]:
        """List triggers for a background app."""
        r = self._get(f"/api/apps/{app_id}/triggers")
        return r.json().get("data", {}).get("triggers", [])

    def fire_trigger(self, app_id: str, trigger_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Manually fire a trigger."""
        r = self._post(f"/api/apps/{app_id}/triggers/{trigger_id}/fire",
            json=payload or {})
        return r.json().get("data", {})

    def test_trigger(self, app_id: str, trigger_id: str) -> dict[str, Any]:
        """Test-fire a trigger (dry run)."""
        r = self._post(f"/api/apps/{app_id}/triggers/{trigger_id}/test")
        return r.json().get("data", {})

    # ── Workspace ───────────────────────────────────────────────

    def get_workspace(self, session: SessionHandle) -> dict[str, Any]:
        """Get workspace state for a session.

        Returns a normalized shape: the raw API nests files under
        `snapshot.resources.files`, which surprised every client. We
        promote `files` (a {path: content} dict) to the top level while
        preserving the full response under `raw` for advanced callers.
        """
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace"
        )
        data = r.json().get("data", {}) or {}
        resources = (data.get("snapshot") or {}).get("resources") or {}
        files = resources.get("files") or {}
        # Back-compat: flat files dict at top level
        flat: dict[str, Any] = {
            "files": files,
            "workspace": data.get("workspace", ""),
            "render_mode": data.get("render_mode"),
            "entry_file": data.get("entry_file"),
            "title": data.get("title"),
            "git": data.get("git"),
            "raw": data,
        }
        return flat

    def get_workspace_file(self, session: SessionHandle, file_path: str) -> dict[str, Any]:
        """Get a single file from the workspace."""
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/files/{file_path}"
        )
        return r.json().get("data", {})

    def get_preview(self, session: SessionHandle) -> dict[str, Any]:
        """Get the preview state for a session."""
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/preview"
        )
        return r.json().get("data", {})

    # ── Watchers ────────────────────────────────────────────────

    def list_watchers(self, app_id: str) -> list[dict[str, Any]]:
        """List active watchers for an app."""
        r = self._get(f"/api/apps/{app_id}/watchers")
        return r.json().get("data", {}).get("watchers", [])

    # ── Session management ──────────────────────────────────────

    def list_sessions(self, app_id: str) -> list[dict[str, Any]]:
        """List all sessions for an app."""
        r = self._get(f"/api/apps/{app_id}/sessions")
        return r.json().get("data", {}).get("sessions", [])

    def fork_session(self, session: SessionHandle) -> dict[str, Any]:
        """Fork a session (create a copy from current state)."""
        r = self._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/fork"
        )
        return r.json().get("data", {})

    def compact_session(self, session: SessionHandle) -> dict[str, Any]:
        """Force context compaction on a session."""
        r = self._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/compact"
        )
        return r.json().get("data", {})

    def export_session(self, session: SessionHandle) -> dict[str, Any]:
        """Export a session as a portable JSON."""
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/export"
        )
        return r.json().get("data", {})

    # ── Internal helpers ────────────────────────────────────────

    def _auto_approve(self, app_id: str) -> int:
        """Auto-approve pending TOOL approvals (not ask_user).

        ask_user requests are skipped - the agent must respond to
        those explicitly via Chat(session_id, respond='answer').
        Returns the number of approved requests.
        """
        try:
            r = self._get(f"/api/apps/{app_id}/approvals")
            pending = r.json().get("data", {}).get("pending", [])
            count = 0
            for req in pending:
                rid = req.get("request_id", "")
                req_type = req.get("type", "")
                # Skip ask_user - agent must respond manually
                if "ask_user" in req_type or "ask" in req.get("tool_name", ""):
                    continue
                if rid:
                    try:
                        self._post(f"/api/apps/{app_id}/approve",
                            json={"request_id": rid, "approved": True})
                        count += 1
                    except Exception:
                        pass
            return count
        except Exception:
            return 0

    def _get_message_count(self, session: SessionHandle) -> int:
        """Get current message count in the session."""
        try:
            r = self._get(
                f"/api/apps/{session.app_id}/sessions/{session.session_id}/history"
            )
            if r.status_code == 200:
                return len(r.json().get("data", {}).get("messages", []))
        except Exception:
            pass
        return 0

    def _extract_turn_result(
        self,
        session: SessionHandle,
        prev_msg_count: int,
        duration: float,
    ) -> TurnResult:
        """Extract a TurnResult from the session history."""
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/history"
        )
        if r.status_code != 200:
            return TurnResult(success=False, error=f"History fetch failed: {r.status_code}")

        messages = r.json().get("data", {}).get("messages", [])
        new_messages = messages[prev_msg_count:]

        # Find assistant text and tool calls
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw_messages: list[dict[str, Any]] = new_messages

        for m in new_messages:
            role = m.get("role", "")

            if role == "assistant":
                content = m.get("content", "")
                if content:
                    text_parts.append(content)
                for tc in m.get("tool_calls", []):
                    fn = tc.get("function", {})
                    name = fn.get("name", "?")
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {"raw": args}
                    tool_calls.append(ToolCall(
                        name=name,
                        arguments=args if isinstance(args, dict) else {},
                    ))

            elif role == "tool":
                # Match tool result to the last tool call
                content = m.get("content", "")
                tc_id = m.get("tool_call_id", "")
                ok = True
                if isinstance(content, str):
                    try:
                        parsed = json.loads(content)
                        if isinstance(parsed, dict) and parsed.get("success") is False:
                            ok = False
                    except Exception:
                        pass
                if tool_calls:
                    tool_calls[-1].result = str(content)[:500]
                    tool_calls[-1].success = ok

        # Enrich tool_calls from events (discovery mode hides them from history)
        if not tool_calls:
            try:
                events = self.get_events(session)
                # Only include events from the CURRENT turn
                current_turn = len(session.turns)
                for evt in events:
                    evt_turn = evt.get("turn", -1)
                    if evt_turn >= 0 and evt_turn != current_turn:
                        continue
                    if evt.get("type") == "tool_call":
                        d = evt.get("data", {})
                        name = d.get("name") or d.get("label") or "?"
                        params = d.get("params", {})
                        if isinstance(params, str):
                            try:
                                params = json.loads(params)
                            except Exception:
                                params = {}
                        tc = ToolCall(
                            name=name,
                            arguments=params if isinstance(params, dict) else {},
                            success=d.get("success", True),
                            result=str(d.get("result", ""))[:500] if d.get("result") else "",
                        )
                        tool_calls.append(tc)
            except Exception:
                pass

        return TurnResult(
            success=True,
            text="\n\n".join(text_parts),
            tool_calls=tool_calls,
            turn_number=len(session.turns) + 1,
            duration_seconds=round(duration, 1),
            raw_messages=raw_messages,
        )

    # ── Queue operations ────────────────────────────────────────

    def post_message_raw(
        self,
        session: SessionHandle,
        message: str,
        queue_mode: str | None = None,
        client_message_id: str | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "message": message,
            "workspace": session.workspace or "",
        }
        if queue_mode:
            body["queue_mode"] = queue_mode
        if client_message_id:
            body["client_message_id"] = client_message_id
        if images:
            body["images"] = images
        r = self._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/messages",
            json=body,
        )
        try:
            return {"status_code": r.status_code, "body": r.json()}
        except Exception:
            return {"status_code": r.status_code, "body": r.text[:500]}

    def get_queue(self, session: SessionHandle, include_finished: bool = False) -> list[dict[str, Any]]:
        params = {"include_finished": "true"} if include_finished else {}
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/queue",
            params=params,
        )
        if r.status_code != 200:
            return []
        return r.json().get("data", {}).get("entries", []) or []

    def clear_queue(self, session: SessionHandle) -> int:
        r = self._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/queue/clear"
        )
        return r.json().get("data", {}).get("cancelled", 0)

    def cancel_queue_entry(self, session: SessionHandle, entry_id: str) -> bool:
        from digitorn.core.cli.auth_helpers import daemon_request
        r = daemon_request(
            "delete",
            f"{self.daemon_url}/api/apps/{session.app_id}/sessions/{session.session_id}/queue/{entry_id}",
        )
        return r.status_code == 200 and r.json().get("success", False)

    def abort_session(self, session: SessionHandle, purge_queue: bool = False) -> dict[str, Any]:
        params = {"purge_queue": "true"} if purge_queue else {}
        r = self._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/abort",
            params=params,
        )
        return r.json().get("data", {})

    # ── User-level credentials ──────────────────────────────────

    def create_user_credential(
        self,
        provider_name: str,
        fields: dict[str, str],
        label: str = "default",
        scope: str = "per_user",
    ) -> dict[str, Any]:
        r = self._post(
            "/api/credentials",
            json={
                "provider_name": provider_name,
                "fields": fields,
                "label": label,
                "scope": scope,
            },
        )
        return r.json().get("data", {}) if r.status_code == 200 else {
            "error": r.text[:500], "status": r.status_code,
        }

    def list_user_credentials(self, provider: str | None = None) -> list[dict[str, Any]]:
        params = {"provider": provider} if provider else {}
        r = self._get("/api/credentials", params=params)
        return r.json().get("data", {}).get("credentials", []) or []

    def delete_user_credential(self, credential_id: str) -> bool:
        from digitorn.core.cli.auth_helpers import daemon_request
        r = daemon_request(
            "delete", f"{self.daemon_url}/api/credentials/{credential_id}",
        )
        return r.status_code == 200

    # ── Persistent event log (DB-backed) ────────────────────────

    def get_persistent_events(
        self,
        session: SessionHandle,
        since_seq: int = 0,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/events",
            params={"since_seq": since_seq, "limit": limit},
        )
        if r.status_code != 200:
            return []
        return r.json().get("data", {}).get("events", []) or []

    # ── Context breakdown ───────────────────────────────────────

    def get_context_breakdown(self, session: SessionHandle) -> dict[str, Any]:
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/context-breakdown"
        )
        if r.status_code != 200:
            return {}
        return r.json().get("data", {})

    # ── Preview / workspace snapshots ───────────────────────────

    def get_preview_snapshot(self, session: SessionHandle) -> dict[str, Any]:
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/preview-snapshot"
        )
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def get_code_snapshot(self, session: SessionHandle) -> dict[str, Any]:
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/code-snapshot"
        )
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def get_workspace_file_content(
        self, session: SessionHandle, file_path: str, include_baseline: bool = False,
    ) -> dict[str, Any]:
        params = {"include_baseline": "true"} if include_baseline else {}
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/files/{file_path}",
            params=params,
        )
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def approve_workspace_file(self, session: SessionHandle, file_path: str) -> bool:
        r = self._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/files/approve",
            json={"path": file_path},
        )
        return r.status_code == 200 and r.json().get("success", False)

    def reject_workspace_file(self, session: SessionHandle, file_path: str) -> bool:
        r = self._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/files/reject",
            json={"path": file_path},
        )
        return r.status_code == 200 and r.json().get("success", False)

    # ── Daemon level ────────────────────────────────────────────

    def get_health(self) -> dict[str, Any]:
        r = self._get("/health")
        return r.json() if r.status_code == 200 else {}

    def get_metrics(self) -> str:
        r = self._get("/metrics")
        return r.text if r.status_code == 200 else ""

    def list_modules(self) -> list[dict[str, Any]]:
        r = self._get("/api/discovery/modules")
        return r.json().get("data", {}).get("modules", []) or []

    def list_mcp_servers(self) -> list[dict[str, Any]]:
        r = self._get("/api/mcp/servers")
        return r.json().get("data", {}).get("servers", []) or []

    # ── Diagnostics ─────────────────────────────────────────────

    def get_app_diagnostics(self, app_id: str) -> dict[str, Any]:
        r = self._get(f"/api/apps/{app_id}/diagnostics")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def get_app_errors(self, app_id: str) -> list[dict[str, Any]]:
        r = self._get(f"/api/apps/{app_id}/errors")
        return r.json().get("data", {}).get("errors", []) or []

    def get_activations(self, app_id: str) -> list[dict[str, Any]]:
        r = self._get(f"/api/apps/{app_id}/activations")
        return r.json().get("data", {}).get("activations", []) or []

    # ── Live Socket.IO event streaming ──────────────────────────

    def _get_auth_token(self) -> str:
        if self._token:
            return self._token
        from digitorn.core.cli.auth_helpers import get_auth_headers
        hdr = get_auth_headers(self.daemon_url)
        auth = hdr.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return auth

    def wait_for_session(self, session: SessionHandle, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = self._get(
                f"/api/apps/{session.app_id}/sessions/{session.session_id}"
            )
            if r.status_code == 200:
                return True
            time.sleep(0.2)
        return False

    def open_event_stream(
        self, session: SessionHandle, start: bool = True,
        wait_for_session: bool = True,
    ) -> "LiveEventStream":
        from digitorn.testing.events import LiveEventStream
        if wait_for_session and not self.wait_for_session(session, timeout=5.0):
            raise DevClientError(
                f"Session {session.session_id} not found - POST a message first"
            )
        stream = LiveEventStream(
            daemon_url=self.daemon_url,
            token=self._get_auth_token(),
            app_id=session.app_id,
            session_id=session.session_id,
        )
        if start:
            stream.start()
        return stream

    def send_live(
        self,
        session: SessionHandle,
        message: str,
        *,
        queue_mode: str | None = None,
        client_message_id: str | None = None,
        idle_seconds: float = 3.0,
        total_timeout: float = 180.0,
        stream: "LiveEventStream | None" = None,
    ) -> "LiveEventStream":
        """POST a message AND open a live event stream for the session.

        Returns the live stream after waiting for `message_done` for the
        posted message's correlation_id (or idle fallback). Reuses the
        provided stream if given (caller stays responsible for stop())."""
        owned = stream is None
        post_result: dict[str, Any] = {}
        if stream is None:
            post_result = self.post_message_raw(
                session, message,
                queue_mode=queue_mode,
                client_message_id=client_message_id,
            )
            stream = self.open_event_stream(session)
        try:
            if not post_result:
                post_result = self.post_message_raw(
                    session, message,
                    queue_mode=queue_mode,
                    client_message_id=client_message_id,
                )
            correlation_id = (post_result.get("body") or {}).get("data", {}).get("correlation_id") or ""
            if correlation_id:
                done = stream.wait_for(
                    "message_done",
                    timeout=total_timeout,
                    predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == correlation_id,
                )
                if done is None:
                    stream.wait_until_idle(quiet_seconds=idle_seconds, total_timeout=5.0)
            else:
                stream.wait_until_idle(quiet_seconds=idle_seconds, total_timeout=total_timeout)
        except Exception:
            if owned:
                stream.stop()
            raise
        return stream

    # ── Images / attachments ────────────────────────────────────

    def encode_image(
        self,
        path_or_bytes: str | bytes | Path,
        mime: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        import base64
        if isinstance(path_or_bytes, (str, Path)):
            p = Path(path_or_bytes)
            raw = p.read_bytes()
            name = name or p.name
            if not mime:
                ext = p.suffix.lower()
                mime = {
                    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                }.get(ext, "application/octet-stream")
        else:
            raw = path_or_bytes
            name = name or "image"
            mime = mime or "image/png"
        return {
            "data": base64.b64encode(raw).decode("ascii"),
            "mime": mime,
            "name": name,
        }

    def get_session_image(self, session: SessionHandle, image_id: str) -> bytes:
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/images/{image_id}"
        )
        return r.content if r.status_code == 200 else b""

    # ── Inbox / notifications / devices (user-scope) ────────────

    def get_inbox(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        r = self._get("/api/users/me/inbox", params={"limit": limit, "offset": offset})
        return r.json().get("data", {}).get("items", []) or []

    def get_inbox_unread_count(self) -> int:
        r = self._get("/api/users/me/inbox/unread_count")
        return int(r.json().get("data", {}).get("count", 0))

    def mark_inbox_read(self, item_id: str) -> bool:
        r = self._post(f"/api/users/me/inbox/{item_id}/read")
        return r.status_code == 200

    def mark_all_inbox_read(self) -> int:
        r = self._post("/api/users/me/inbox/read_all")
        return int(r.json().get("data", {}).get("marked", 0))

    def delete_inbox_item(self, item_id: str) -> bool:
        r = self._delete(f"/api/users/me/inbox/{item_id}")
        return r.status_code == 200

    def list_user_sessions(self) -> list[dict[str, Any]]:
        r = self._get("/api/users/me/sessions")
        return r.json().get("data", {}).get("sessions", []) or []

    def list_user_approvals(self) -> list[dict[str, Any]]:
        r = self._get("/api/users/me/approvals")
        return r.json().get("data", {}).get("pending", []) or []

    def list_devices(self) -> list[dict[str, Any]]:
        r = self._get("/api/users/me/devices")
        return r.json().get("data", {}).get("devices", []) or []

    def register_device(self, device_id: str, name: str = "", platform: str = "test") -> bool:
        r = self._post(
            "/api/users/me/devices",
            json={"device_id": device_id, "name": name, "platform": platform},
        )
        return r.status_code in (200, 201)

    def delete_device(self, device_id: str) -> bool:
        r = self._delete(f"/api/users/me/devices/{device_id}")
        return r.status_code == 200

    def send_notification(self, app_id: str, body: dict[str, Any]) -> dict[str, Any]:
        r = self._post(f"/api/apps/{app_id}/notifications", json=body)
        return r.json() if r.status_code == 200 else {"error": r.text[:200]}

    def list_active_notifications(self, app_id: str) -> list[dict[str, Any]]:
        r = self._get(f"/api/apps/{app_id}/notifications/active")
        return r.json().get("data", {}).get("notifications", []) or []

    # ── Packages (install/update/upgrade built-in or remote apps) ──

    def list_packages(self) -> list[dict[str, Any]]:
        r = self._get("/api/packages")
        return r.json().get("data", {}).get("packages", []) or []

    def get_package(self, package_id: str) -> dict[str, Any]:
        r = self._get(f"/api/packages/{package_id}")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def check_package_update(self, package_id: str) -> dict[str, Any]:
        r = self._get(f"/api/packages/{package_id}/check-update")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def install_package(self, source: str, force: bool = False) -> dict[str, Any]:
        r = self._post("/api/packages/install", json={"source": source, "force": force})
        return r.json().get("data", {}) if r.status_code == 200 else {"error": r.text[:200]}

    def upgrade_package(self, package_id: str) -> dict[str, Any]:
        r = self._post(f"/api/packages/{package_id}/upgrade")
        return r.json().get("data", {}) if r.status_code == 200 else {"error": r.text[:200]}

    def uninstall_package(self, package_id: str) -> bool:
        r = self._post(f"/api/packages/{package_id}/uninstall")
        return r.status_code == 200

    def get_package_asset(self, package_id: str, asset_path: str) -> bytes:
        r = self._get(f"/api/packages/{package_id}/assets/{asset_path}")
        return r.content if r.status_code == 200 else b""

    # ── MCP (catalog + installed servers) ───────────────────────

    def mcp_catalog(self) -> list[dict[str, Any]]:
        r = self._get("/api/mcp/catalog")
        return r.json().get("data", {}).get("servers", []) or []

    def mcp_get_server_spec(self, server_id: str) -> dict[str, Any]:
        r = self._get(f"/api/mcp/catalog/{server_id}")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def mcp_search(self, query: str) -> list[dict[str, Any]]:
        r = self._get("/api/mcp/search", params={"q": query})
        return r.json().get("data", {}).get("results", []) or []

    def mcp_list_servers(self) -> list[dict[str, Any]]:
        r = self._get("/api/mcp/servers")
        return r.json().get("data", {}).get("servers", []) or []

    def mcp_get_server(self, server_id: str) -> dict[str, Any]:
        r = self._get(f"/api/mcp/servers/{server_id}")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def mcp_install_server(self, body: dict[str, Any]) -> dict[str, Any]:
        r = self._post("/api/mcp/servers", json=body)
        return r.json().get("data", {}) if r.status_code in (200, 201) else {"error": r.text[:200]}

    def mcp_delete_server(self, server_id: str) -> bool:
        r = self._delete(f"/api/mcp/servers/{server_id}")
        return r.status_code == 200

    def mcp_test_server(self, server_id: str) -> dict[str, Any]:
        r = self._post(f"/api/mcp/servers/{server_id}/test")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def mcp_update_server_config(self, server_id: str, config: dict[str, Any]) -> bool:
        r = self._put(f"/api/mcp/servers/{server_id}/config", json=config)
        return r.status_code == 200

    def mcp_pool(self) -> dict[str, Any]:
        r = self._get("/api/mcp/pool")
        return r.json().get("data", {})

    def mcp_pool_health(self) -> dict[str, Any]:
        r = self._get("/api/mcp/pool/health")
        return r.json().get("data", {})

    def mcp_pool_connect(self, server_id: str) -> bool:
        r = self._post(f"/api/mcp/pool/{server_id}/connect")
        return r.status_code == 200

    def mcp_pool_disconnect(self, server_id: str) -> bool:
        r = self._post(f"/api/mcp/pool/{server_id}/disconnect")
        return r.status_code == 200

    # ── Discovery / compile (for Builder) ───────────────────────

    def discovery_modules(self) -> list[dict[str, Any]]:
        r = self._get("/api/discovery/modules")
        return r.json().get("data", {}).get("modules", []) or []

    def discovery_module(self, module_id: str) -> dict[str, Any]:
        r = self._get(f"/api/discovery/modules/{module_id}")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def discovery_triggers(self) -> list[dict[str, Any]]:
        r = self._get("/api/discovery/triggers")
        return r.json().get("data", {}).get("triggers", []) or []

    def discovery_templates(self) -> list[dict[str, Any]]:
        r = self._get("/api/discovery/templates")
        return r.json().get("data", {}).get("templates", []) or []

    def discovery_template(self, template_id: str) -> dict[str, Any]:
        r = self._get(f"/api/discovery/templates/{template_id}")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def compile_yaml(self, yaml_content: str) -> dict[str, Any]:
        r = self._post("/api/discovery/compile", json={"yaml": yaml_content})
        return r.json().get("data", {}) if r.status_code == 200 else {"error": r.text[:500]}

    def prompt_preview(self, yaml_content: str, agent_id: str = "") -> dict[str, Any]:
        r = self._post(
            "/api/discovery/prompt-preview",
            json={"yaml": yaml_content, "agent_id": agent_id},
        )
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def generate_package_manifest(self, yaml_content: str) -> dict[str, Any]:
        r = self._post(
            "/api/discovery/generate-package-manifest",
            json={"yaml": yaml_content},
        )
        return r.json().get("data", {}) if r.status_code == 200 else {}

    # ── Builder drafts ──────────────────────────────────────────

    def list_drafts(self) -> list[dict[str, Any]]:
        r = self._get("/api/builder/drafts")
        return r.json().get("data", {}).get("drafts", []) or []

    def create_draft(self, yaml_content: str, name: str = "") -> dict[str, Any]:
        r = self._post("/api/builder/drafts", json={"yaml": yaml_content, "name": name})
        return r.json().get("data", {}) if r.status_code in (200, 201) else {"error": r.text[:200]}

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        r = self._get(f"/api/builder/drafts/{draft_id}")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def update_draft(self, draft_id: str, yaml_content: str, name: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"yaml": yaml_content}
        if name is not None:
            body["name"] = name
        r = self._patch(f"/api/builder/drafts/{draft_id}", json=body)
        return r.json().get("data", {}) if r.status_code == 200 else {"error": r.text[:200]}

    def delete_draft(self, draft_id: str) -> bool:
        r = self._delete(f"/api/builder/drafts/{draft_id}")
        return r.status_code == 200

    def deploy_draft(self, draft_id: str, force: bool = True) -> dict[str, Any]:
        r = self._post(
            f"/api/builder/drafts/{draft_id}/deploy",
            json={"force": force},
        )
        return r.json().get("data", {}) if r.status_code == 200 else {"error": r.text[:200]}

    def deploy_yaml_content(self, yaml_content: str, force: bool = True) -> AppHandle:
        """Deploy an app from inline YAML (no file on disk). Builder-friendly."""
        import tempfile
        import os as _os
        fd, tmp = tempfile.mkstemp(suffix=".yaml", prefix="qt-")
        try:
            _os.write(fd, yaml_content.encode("utf-8"))
            _os.close(fd)
            return self.deploy(tmp, force=force)
        finally:
            try:
                _os.remove(tmp)
            except Exception:
                pass

    # ── Security profiles ───────────────────────────────────────

    def get_security_profile(self, app_id: str) -> dict[str, Any]:
        r = self._get(f"/api/security/profiles/{app_id}")
        return r.json() if r.status_code == 200 else {}

    def create_security_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        r = self._post("/api/security/profiles", json=body)
        return r.json() if r.status_code in (200, 201) else {"error": r.text[:200]}

    def update_security_profile(self, app_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        r = self._patch(f"/api/security/profiles/{app_id}", json=patch)
        return r.json() if r.status_code == 200 else {"error": r.text[:200]}

    def delete_security_profile(self, app_id: str) -> bool:
        r = self._delete(f"/api/security/profiles/{app_id}")
        return r.status_code == 200

    def list_grants(self, app_id: str) -> list[dict[str, Any]]:
        r = self._get(f"/api/security/profiles/{app_id}/grants")
        return r.json().get("grants", []) if r.status_code == 200 else []

    def update_grant(self, app_id: str, module_id: str, grant: dict[str, Any]) -> bool:
        r = self._put(f"/api/security/profiles/{app_id}/grants/{module_id}", json=grant)
        return r.status_code == 200

    def delete_grant(self, app_id: str, module_id: str) -> bool:
        r = self._delete(f"/api/security/profiles/{app_id}/grants/{module_id}")
        return r.status_code == 200

    # ── Background tasks ────────────────────────────────────────

    def create_background_task(self, app_id: str, body: dict[str, Any]) -> dict[str, Any]:
        r = self._post(f"/api/apps/{app_id}/background-tasks", json=body)
        return r.json().get("data", {}) if r.status_code == 200 else {"error": r.text[:200]}

    def list_background_tasks(self, app_id: str) -> list[dict[str, Any]]:
        r = self._get(f"/api/apps/{app_id}/background-tasks")
        return r.json().get("data", {}).get("tasks", []) or []

    def get_background_task(self, app_id: str, task_id: str) -> dict[str, Any]:
        r = self._get(f"/api/apps/{app_id}/background-tasks/{task_id}")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def cancel_background_task(self, app_id: str, task_id: str) -> bool:
        r = self._delete(f"/api/apps/{app_id}/background-tasks/{task_id}")
        return r.status_code == 200

    def wait_background_task(self, app_id: str, task_id: str, timeout: float = 60.0) -> dict[str, Any]:
        r = self._post(
            f"/api/apps/{app_id}/background-tasks/{task_id}/wait",
            json={"timeout": timeout},
        )
        return r.json().get("data", {}) if r.status_code == 200 else {}

    # ── Background sessions - payload ops ───────────────────────

    def bg_session_get_payload(self, app_id: str, bg_session_id: str) -> dict[str, Any]:
        r = self._get(f"/api/apps/{app_id}/background-sessions/{bg_session_id}/payload")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def bg_session_set_payload(self, app_id: str, bg_session_id: str, payload: dict[str, Any]) -> bool:
        r = self._put(
            f"/api/apps/{app_id}/background-sessions/{bg_session_id}/payload",
            json=payload,
        )
        return r.status_code == 200

    def bg_session_delete_payload(self, app_id: str, bg_session_id: str) -> bool:
        r = self._delete(f"/api/apps/{app_id}/background-sessions/{bg_session_id}/payload")
        return r.status_code == 200

    def bg_session_pause(self, app_id: str, bg_session_id: str) -> bool:
        r = self._post(f"/api/apps/{app_id}/background-sessions/{bg_session_id}/pause")
        return r.status_code == 200

    def bg_session_resume(self, app_id: str, bg_session_id: str) -> bool:
        r = self._post(f"/api/apps/{app_id}/background-sessions/{bg_session_id}/resume")
        return r.status_code == 200

    def bg_session_delete(self, app_id: str, bg_session_id: str) -> bool:
        r = self._delete(f"/api/apps/{app_id}/background-sessions/{bg_session_id}")
        return r.status_code == 200

    def get_payload_schema(self, app_id: str) -> dict[str, Any]:
        r = self._get(f"/api/apps/{app_id}/payload-schema")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    # ── Watchers ────────────────────────────────────────────────

    def create_watcher(self, app_id: str, body: dict[str, Any]) -> dict[str, Any]:
        r = self._post(f"/api/apps/{app_id}/watchers", json=body)
        return r.json().get("data", {}) if r.status_code == 200 else {"error": r.text[:200]}

    def get_watcher(self, app_id: str, watcher_id: str) -> dict[str, Any]:
        r = self._get(f"/api/apps/{app_id}/watchers/{watcher_id}")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    # ── Activations / artifacts / assets ────────────────────────

    def get_activation(self, app_id: str, activation_id: str) -> dict[str, Any]:
        r = self._get(f"/api/apps/{app_id}/activations/{activation_id}")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def get_activations_stats(self, app_id: str) -> dict[str, Any]:
        r = self._get(f"/api/apps/{app_id}/activations/stats")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def get_channels_health(self, app_id: str) -> dict[str, Any]:
        r = self._get(f"/api/apps/{app_id}/channels/health")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def get_app_asset(self, app_id: str, asset_path: str) -> bytes:
        r = self._get(f"/api/apps/{app_id}/assets/{asset_path}")
        return r.content if r.status_code == 200 else b""

    def download_artifact(self, app_id: str, event_id: str) -> bytes:
        r = self._get(f"/api/apps/{app_id}/artifacts/{event_id}/download")
        return r.content if r.status_code == 200 else b""

    def get_app_icon(self, app_id: str) -> bytes:
        r = self._get(f"/api/apps/{app_id}/icon")
        return r.content if r.status_code == 200 else b""

    def get_app_status(self, app_id: str) -> dict[str, Any]:
        r = self._get(f"/api/apps/{app_id}/status")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def list_app_files(self, app_id: str) -> list[dict[str, Any]]:
        r = self._get(f"/api/apps/{app_id}/files")
        return r.json().get("data", {}).get("files", []) or []

    # ── Workspace advanced ops ──────────────────────────────────

    def export_workspace(self, session: SessionHandle) -> dict[str, Any]:
        r = self._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/export"
        )
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def import_workspace(self, session: SessionHandle, body: dict[str, Any]) -> dict[str, Any]:
        r = self._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/import",
            json=body,
        )
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def fork_workspace(self, session: SessionHandle, new_session_id: str | None = None) -> dict[str, Any]:
        body = {"new_session_id": new_session_id} if new_session_id else {}
        r = self._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/fork",
            json=body,
        )
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def workspace_git_status(self, session: SessionHandle) -> dict[str, Any]:
        r = self._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/git-status"
        )
        return r.json().get("data", {}) if r.status_code == 200 else {}

    # ── Sessions search / undo / misc ───────────────────────────

    def search_sessions(self, app_id: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
        r = self._get(
            f"/api/apps/{app_id}/sessions/search",
            params={"q": query, "limit": limit},
        )
        return r.json().get("data", {}).get("sessions", []) or []

    def undo_session(self, session: SessionHandle) -> dict[str, Any]:
        r = self._post(f"/api/apps/{session.app_id}/sessions/{session.session_id}/undo")
        return r.json().get("data", {}) if r.status_code == 200 else {}

    def cancel_task(self, session: SessionHandle, task_id: str) -> bool:
        r = self._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/tasks/{task_id}/cancel"
        )
        return r.status_code == 200

    # ── Daemon-level config (/api/config) ───────────────────────

    def get_daemon_config(self) -> dict[str, Any]:
        r = self._get("/api/config")
        return r.json() if r.status_code == 200 else {}

    def patch_daemon_config(self, changes: dict[str, Any]) -> dict[str, Any]:
        r = self._patch("/api/config", json=changes)
        return r.json() if r.status_code == 200 else {"error": r.text[:200]}

    def browse_config(self, path: str = "") -> dict[str, Any]:
        r = self._get("/api/config/browse", params={"path": path} if path else {})
        return r.json() if r.status_code == 200 else {}

    # ── Modules runtime (execute actions directly) ──────────────

    def list_runtime_modules(self) -> list[dict[str, Any]]:
        r = self._get("/api/modules")
        return r.json() if r.status_code == 200 else []

    def get_runtime_module(self, module_id: str) -> dict[str, Any]:
        r = self._get(f"/api/modules/{module_id}")
        return r.json() if r.status_code == 200 else {}

    def execute_module_action(self, module_id: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
        r = self._post(
            f"/api/modules/{module_id}/execute",
            json={"action": action, "params": params},
        )
        return r.json() if r.status_code == 200 else {"error": r.text[:200]}

    def module_health(self, module_id: str) -> dict[str, Any]:
        r = self._get(f"/api/modules/{module_id}/health")
        return r.json() if r.status_code == 200 else {}

    # ── System requires (Python deps, etc) ──────────────────────

    def list_requires(self) -> list[dict[str, Any]]:
        r = self._get("/api/requires")
        return r.json() if r.status_code == 200 else []

    def install_require(self, body: dict[str, Any]) -> dict[str, Any]:
        r = self._post("/api/requires/install", json=body)
        return r.json() if r.status_code == 200 else {"error": r.text[:200]}

    def install_all_requires(self) -> dict[str, Any]:
        r = self._post("/api/requires/install-all")
        return r.json() if r.status_code == 200 else {"error": r.text[:200]}

    # ── Pipeline / one-shot with explicit payload ───────────────

    def run_pipeline(self, app_id: str, input_data: Any, timeout: float = 300.0) -> dict[str, Any]:
        r = self._post(
            f"/api/apps/{app_id}/pipeline",
            json={"input": input_data},
            timeout=timeout,
        )
        return r.json().get("data", {}) if r.status_code == 200 else {"error": r.text[:500]}

    # ── Transcribe (audio → text) ──────────────────────────────

    def transcribe_audio(self, audio_bytes: bytes, mime: str = "audio/webm") -> str:
        import base64
        r = self._post(
            "/api/transcribe",
            json={"audio": base64.b64encode(audio_bytes).decode("ascii"), "mime": mime},
        )
        return r.json().get("text", "") if r.status_code == 200 else ""

    # ── Live stream with multi-room support ─────────────────────

    def open_multi_stream(
        self,
        session: SessionHandle | None = None,
        also_app: str | None = None,
        also_user: bool = False,
        since_seq: int = 0,
    ) -> "LiveEventStream":
        """Open a stream that joins session + app + user rooms at once."""
        from digitorn.testing.events import LiveEventStream
        if session is not None and not self.wait_for_session(session, timeout=5.0):
            raise DevClientError(f"Session {session.session_id} not found")
        app_id = session.app_id if session else (also_app or "")
        sid = session.session_id if session else "_global_"
        stream = LiveEventStream(
            daemon_url=self.daemon_url,
            token=self._get_auth_token(),
            app_id=app_id,
            session_id=sid,
            since_seq=since_seq,
            also_join_app=bool(also_app),
            also_join_user=also_user,
        )
        stream.start()
        return stream
