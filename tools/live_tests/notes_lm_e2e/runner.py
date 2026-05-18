"""Shared helpers for the notes-lm phase tests."""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Make digitorn importable when running from repo root.
ROOT = Path(__file__).resolve().parents[3]
PKG = ROOT / "packages"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

from digitorn.testing import DevClient  # noqa: E402
from digitorn.testing.models import SessionHandle  # noqa: E402

logger = logging.getLogger("notes_lm_e2e")

DAEMON = "http://127.0.0.1:8000"
APP_ID = "notes-lm"


def make_client(timeout: float = 180.0) -> DevClient:
    creds_path = Path.home() / ".digitorn" / "credentials.json"
    if creds_path.exists():
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
        tok = creds.get("access_token") or creds.get("token") or ""
        if tok:
            return DevClient.with_token(tok, daemon_url=DAEMON, timeout=timeout)
    raise SystemExit(
        "No credentials.json found - log into the daemon first via the CLI."
    )


def make_session(client: DevClient, label: str = "p") -> SessionHandle:
    """Build a SessionHandle AND seed a real session on the daemon.

    The daemon enforces "no empty sessions" - the only way to create
    a session is ``POST /sessions`` with a first user message. Direct
    PUTs to ``/workspace/files/...`` before that POST return 404.

    We seed the session with a no-op "ping" message and wait briefly
    for the first turn to land so subsequent PUTs / messages are
    targeting a fully-initialized session. We do NOT wait for the
    assistant to finish - that would cost an LLM call per phase. We
    just need the session row + workspace mount to exist on disk.

    The 404-tolerant fallback path GETs the history endpoint, which
    in some daemon builds lazy-creates the session.
    """
    sid = f"e2e-{label}-{uuid.uuid4().hex[:8]}"
    workspace = str(Path.home() / ".digitorn" / "workspaces" / APP_ID / sid)
    Path(workspace).mkdir(parents=True, exist_ok=True)
    handle = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace=workspace,
    )

    created = False
    try:
        r = client._post(
            f"/api/apps/{APP_ID}/sessions",
            json={
                "session_id": sid,
                "message": ".",  # smallest valid kickoff (min_length=1)
                "workspace_path": workspace,
            },
        )
        if r.status_code in (200, 201, 202):
            created = True
        elif r.status_code == 409:
            # Already exists (unlikely with a fresh uuid). Treat as ok.
            created = True
        else:
            logger.debug(
                "POST /sessions returned %s body=%s",
                r.status_code, r.text[:200],
            )
    except Exception as exc:
        logger.debug("POST /sessions raised: %s", exc)

    if not created:
        # Last-resort: GET history. Some builds materialize the row on
        # this path. If it 404s the caller will surface its own error.
        try:
            client._get(f"/api/apps/{APP_ID}/sessions/{sid}/history")
        except Exception:
            pass

    # Give the daemon a moment to flush the session row to disk before
    # we hit it with workspace PUTs.
    time.sleep(0.4)
    return handle


def send_and_wait(
    client: DevClient,
    session: SessionHandle,
    message: str,
    *,
    system_addendum: str | None = None,
    timeout: float = 240.0,
    require_assistant_text: bool = True,
) -> dict[str, Any]:
    """Send a message + poll until the assistant has produced something
    (or timeout). Returns a dict with::

        {
          "ok": bool,
          "messages_before": int,
          "messages_after": int,
          "assistant_text": str,
          "tool_calls": list[dict],
          "elapsed_s": float,
          "error": str | None,
        }
    """
    t0 = time.monotonic()
    # 1. snapshot pre-state
    try:
        rh = client._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/history",
        )
        msgs_before = rh.json().get("data", {}).get("messages", []) if rh.status_code == 200 else []
    except Exception:
        msgs_before = []
    initial_count = len(msgs_before)

    # 2. POST the message (with optional system_addendum)
    body: dict[str, Any] = {"message": message, "workspace": session.workspace}
    if system_addendum is not None:
        body["system_addendum"] = system_addendum
    try:
        r = client._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/messages",
            json=body,
        )
        if r.status_code not in (200, 202):
            return {
                "ok": False, "messages_before": initial_count,
                "messages_after": initial_count, "assistant_text": "",
                "tool_calls": [], "elapsed_s": time.monotonic() - t0,
                "error": f"POST {r.status_code}: {r.text[:300]}",
            }
    except Exception as exc:
        return {
            "ok": False, "messages_before": initial_count,
            "messages_after": initial_count, "assistant_text": "",
            "tool_calls": [], "elapsed_s": time.monotonic() - t0,
            "error": f"POST exception: {exc!r}",
        }

    # 3. poll history until the agent turn is FULLY done.
    # An assistant message WITH tool_calls is intermediate - the agent
    # will produce a follow-up message after the tool result lands.
    # We're done when the LAST message is an assistant with non-empty
    # text AND no pending tool_calls AND the count has been stable
    # for at least 2 polls.
    last_msgs: list[dict[str, Any]] = []
    stable_count = 0
    last_seen_count = initial_count
    while time.monotonic() - t0 < timeout:
        try:
            rh = client._get(
                f"/api/apps/{session.app_id}/sessions/{session.session_id}/history",
            )
            if rh.status_code == 200:
                last_msgs = rh.json().get("data", {}).get("messages", []) or []
                last = last_msgs[-1] if last_msgs else {}
                turn_done = (
                    len(last_msgs) > initial_count
                    and last.get("role") == "assistant"
                    and (last.get("content") or "").strip()
                    and not (last.get("tool_calls") or [])
                )
                if turn_done and len(last_msgs) == last_seen_count:
                    stable_count += 1
                    if stable_count >= 2:
                        break
                else:
                    stable_count = 0
                    last_seen_count = len(last_msgs)
        except Exception as exc:
            logger.debug("poll error: %s", exc)
        time.sleep(0.4)

    # 4. extract assistant final text + tool_calls. Forward iteration
    # so the last assignment to ``assistant_text`` is the NEWEST
    # assistant message of this turn (not the first - that was a bug
    # that read "I'll check..." instead of the refusal that followed).
    assistant_text = ""
    tool_calls: list[dict[str, Any]] = []
    for m in last_msgs[initial_count:]:
        if m.get("role") == "assistant":
            content = m.get("content", "") or ""
            if content.strip():
                assistant_text = content
        tcs = m.get("tool_calls") or []
        if tcs:
            for tc in tcs:
                fn = (tc or {}).get("function") or {}
                tool_calls.append({
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", ""),
                })

    ok = (len(last_msgs) > initial_count)
    if require_assistant_text and not assistant_text.strip():
        ok = False
    err = None if ok else "no new assistant message before timeout"
    return {
        "ok": ok,
        "messages_before": initial_count,
        "messages_after": len(last_msgs),
        "assistant_text": assistant_text,
        "tool_calls": tool_calls,
        "elapsed_s": time.monotonic() - t0,
        "error": err,
    }


def fetch_workspace_file(
    client: DevClient, session: SessionHandle, path: str,
) -> str | None:
    """Read a workspace file via the daemon REST API. Returns None on
    404 or empty. Useful for asserting on artefact contents."""
    try:
        r = client._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}"
            f"/workspace/files/{path}",
        )
        if r.status_code != 200:
            return None
        data = r.json().get("data") or {}
        return data.get("content") or ""
    except Exception:
        return None


def list_workspace_files(
    client: DevClient, session: SessionHandle,
) -> list[str]:
    try:
        r = client._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}"
            f"/workspace",
        )
        if r.status_code != 200:
            return []
        data = r.json().get("data") or {}
        files = data.get("files") or []
        return [f.get("path") for f in files if isinstance(f, dict) and f.get("path")]
    except Exception:
        return []


def session_events(
    client: DevClient, session: SessionHandle,
) -> list[dict[str, Any]]:
    return client.get_persistent_events(session) or []


def system_message_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [e for e in events if e.get("type") == "system_message"]
    out.sort(key=lambda e: int(e.get("seq", 0)))
    return out


# ── Pretty printing ─────────────────────────────────────────────────


class Reporter:
    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def ok(self, name: str, note: str = "") -> None:
        self.passed.append(name)
        suffix = f"  ({note})" if note else ""
        print(f"  [PASS] {name}{suffix}")

    def fail(self, name: str, detail: str) -> None:
        self.failed.append((name, detail))
        print(f"  [FAIL] {name}  -- {detail}")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print()
        print(f"=== {self.phase}: {len(self.passed)}/{total} PASS ===")
        if self.failed:
            for n, d in self.failed:
                print(f"  FAIL  {n}  -- {d}")
        return 1 if self.failed else 0
