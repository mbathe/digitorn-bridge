"""Programmatic multi-turn chat against the real daemon.

Mirrors what `digitorn dev chat <app>` does interactively but exposes
it as a callable so test scenarios can:
  - send a message
  - wait for the agent to finish (poll session status)
  - auto-approve any tool-call gates
  - read the assistant's response text
  - send the next message in the same session

This is what makes the e2e tests "real conversations" - the same code
path the human + Flutter client + web client all use.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field


def _bearer() -> str | None:
    """Get a valid Bearer token. Priority:
      1. ``~/.digitorn/credentials.json`` access_token - this is the
         user-issued JWT with ``perms`` populated (works for all
         protected routes including ``apps:deploy``). Refreshed via
         ``POST https://auth.digitorn.ai/auth/login``.
      2. ``LocalDeviceAuth`` device_token - daemon-pair token with
         empty perms; only useful as a last-resort fallback.
    """
    p = os.path.expanduser("~/.digitorn/credentials.json")
    try:
        with open(p) as f:
            tok = json.load(f).get("access_token")
        if tok:
            return tok
    except Exception:
        pass
    try:
        from digitorn.core.auth.local_device import LocalDeviceAuth
        auth = LocalDeviceAuth.load()
        return auth.device_token
    except Exception:
        return None


@dataclass
class TurnResult:
    """One turn of a multi-turn conversation."""
    user_message: str
    assistant_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    duration_s: float = 0.0
    error: str | None = None


@dataclass
class Conversation:
    """The full transcript + last session state."""
    app_id: str
    session_id: str
    daemon: str
    turns: list[TurnResult] = field(default_factory=list)

    @property
    def assistant_texts(self) -> list[str]:
        return [t.assistant_text for t in self.turns]

    @property
    def transcript(self) -> str:
        out: list[str] = []
        for t in self.turns:
            out.append(f">>> USER: {t.user_message}")
            out.append(f"<<< AGENT: {t.assistant_text}")
            if t.tool_calls:
                for tc in t.tool_calls:
                    out.append(
                        f"      [tool] {tc.get('name', '?')} "
                        f"params={json.dumps(tc.get('params', {}))[:120]}"
                    )
        return "\n".join(out)


def _http(
    method: str, url: str, body: dict | None = None,
    *, daemon: str = "", auth: bool = True, timeout: int = 30,
) -> tuple[int, dict]:
    if not url.startswith("http"):
        url = f"{daemon}{url}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if auth:
        tok = _bearer()
        if tok:
            req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except Exception as e:
        return 0, {"_error": str(e)}


def _auto_approve(daemon: str, app_id: str) -> int:
    """Approve every pending tool call. Returns count approved."""
    s, d = _http("GET", f"/api/apps/{app_id}/approvals", daemon=daemon)
    if s != 200:
        return 0
    pending = d.get("data", {}).get("pending", [])
    n = 0
    for p in pending:
        rid = p.get("id")
        if not rid:
            continue
        s, _ = _http(
            "POST", f"/api/apps/{app_id}/approvals/{rid}/approve",
            daemon=daemon,
        )
        if s == 200:
            n += 1
    return n


def start_conversation(
    app_id: str, *, daemon: str = "http://127.0.0.1:8765",
    session_id: str | None = None,
) -> Conversation:
    """Open a new chat session for the app."""
    sid = session_id or f"e2e-{uuid.uuid4().hex[:8]}"
    return Conversation(app_id=app_id, session_id=sid, daemon=daemon)


def send_turn(
    conv: Conversation, message: str, *,
    timeout_s: int = 90, poll_interval: float = 0.5,
) -> TurnResult:
    """Send one user message, wait for the agent's reply.

    First turn creates the session via ``POST /sessions`` (atomic
    create + first message). Subsequent turns hit
    ``POST /sessions/{sid}/messages``.

    Loop: poll /sessions/{sid} for completion + auto-approve tool
    gates + read history once finished.
    """
    t0 = time.time()
    turn = TurnResult(user_message=message)

    # First turn: create the session WITH the first message.
    if not conv.turns:
        s, d = _http(
            "POST",
            f"/api/apps/{conv.app_id}/sessions",
            body={"message": message},
            daemon=conv.daemon,
        )
        # The daemon assigns its own session_id. Capture it.
        if s in (200, 201, 202):
            sid = (
                d.get("data", {}).get("session_id")
                or d.get("data", {}).get("session", {}).get("id")
                or conv.session_id
            )
            conv.session_id = sid
        else:
            turn.error = f"create session failed: {s} {str(d)[:200]}"
            turn.duration_s = time.time() - t0
            conv.turns.append(turn)
            return turn
    else:
        s, d = _http(
            "POST",
            f"/api/apps/{conv.app_id}/sessions/{conv.session_id}/messages",
            body={"message": message},
            daemon=conv.daemon,
        )
        if s not in (200, 201, 202):
            turn.error = f"send failed: {s} {str(d)[:200]}"
            turn.duration_s = time.time() - t0
            conv.turns.append(turn)
            return turn

    # Poll for completion. Match the dev CLI semantics:
    #   is_active == False AND turn_count incremented => turn finished.
    deadline = t0 + timeout_s
    target_turn = len(conv.turns)  # turn count expected after this send
    finished = False
    while time.time() < deadline:
        _auto_approve(conv.daemon, conv.app_id)
        s, st = _http(
            "GET",
            f"/api/apps/{conv.app_id}/sessions/{conv.session_id}",
            daemon=conv.daemon,
        )
        if s == 200:
            data = st.get("data", {})
            active = data.get("is_active", False)
            turn_count = data.get("turn_count", 0)
            if not active and turn_count > target_turn:
                finished = True
                break
        time.sleep(poll_interval)

    if not finished:
        turn.error = "timeout waiting for assistant"
    else:
        # Pull the last assistant message from history.
        s, h = _http(
            "GET",
            f"/api/apps/{conv.app_id}/sessions/{conv.session_id}/history",
            daemon=conv.daemon,
        )
        msgs = h.get("data", {}).get("messages", [])
        for m in reversed(msgs):
            if m.get("role") == "assistant":
                turn.assistant_text = m.get("content", "") or ""
                tc = m.get("tool_calls") or []
                for t_ in tc:
                    fn = t_.get("function", {})
                    turn.tool_calls.append({
                        "name": t_.get("name") or fn.get("name", ""),
                        "params": t_.get("params") or fn.get("arguments", {}),
                    })
                break

    turn.duration_s = time.time() - t0
    conv.turns.append(turn)
    return turn


def chat(
    app_id: str, messages: list[str], *,
    daemon: str = "http://127.0.0.1:8765",
    session_id: str | None = None,
    timeout_per_turn: int = 90,
) -> Conversation:
    """Convenience: run a full multi-turn conversation."""
    conv = start_conversation(app_id, daemon=daemon, session_id=session_id)
    for msg in messages:
        send_turn(conv, msg, timeout_s=timeout_per_turn)
    return conv
