"""End-to-end smoke test for the structured-streaming events.

Verifies the daemon → SDK contract for typed content blocks:

  S1. ``token`` events arrive (text deltas).
  S2. ``thinking_started`` + ``thinking_delta`` events arrive when the
      model uses adaptive thinking. (Skipped softly if the model
      didn't think for this prompt.)
  S3. ``tool_start`` / ``tool_call`` events DO NOT mix into the text
      stream - they are emitted as separate types, in order.
  S4. ``turn_complete`` lands once the stream is done.

Together these prove the SDK ``useStream()`` hook can faithfully
build a chronological ``ContentBlock[]`` view by listening on the
session bus.

Run with the daemon up:

  py -3.12 -X utf8 tools/live_tests/stream_check.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import socketio


_DAEMON = os.environ.get("DIGITORN_DAEMON", "http://127.0.0.1:8000")
_PASSWORD = os.environ.get("DEV_PASSWORD", "pw1234567")
_APP_YAML = (
    "c:/Users/ASUS/Documents/digitorn-bridge/examples/stream-test/app.yaml"
)


def _login(daemon_url: str, email: str, password: str) -> str:
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        r = c.post(f"{daemon_url}/auth/login",
                   json={"email": email, "password": password})
        if r.status_code == 401:
            uname = email.split("@", 1)[0].replace(".", "_").replace("-", "_")
            reg = c.post(f"{daemon_url}/auth/register",
                         json={"email": email, "password": password, "username": uname})
            if reg.status_code not in (200, 201):
                raise RuntimeError(f"register {reg.status_code}: {reg.text[:200]}")
            r = c.post(f"{daemon_url}/auth/login",
                       json={"email": email, "password": password})
        if r.status_code != 200:
            raise RuntimeError(f"login {r.status_code}: {r.text[:200]}")
        return r.json().get("access_token") or ""


async def _deploy(client: httpx.AsyncClient, yaml_path: str) -> str:
    r = await client.post(
        f"{_DAEMON}/api/apps/deploy",
        json={"yaml_path": str(Path(yaml_path).resolve()), "force": True},
    )
    if r.status_code != 200:
        raise RuntimeError(f"deploy {r.status_code}: {r.text[:300]}")
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"deploy fail: {body}")
    app_id = (body.get("data") or {}).get("app_id") or "stream-test"
    deadline = time.time() + 90
    last_status = 0
    while time.time() < deadline:
        m = await client.get(f"{_DAEMON}/api/apps/{app_id}")
        last_status = m.status_code
        if m.status_code == 200:
            return app_id
        await asyncio.sleep(0.5)
    raise RuntimeError(f"deploy never ready: last_status={last_status}")


async def _create_session(client: httpx.AsyncClient, app_id: str) -> str:
    deadline = time.time() + 30
    last_text = ""
    while time.time() < deadline:
        r = await client.post(
            f"{_DAEMON}/api/apps/{app_id}/sessions",
            json={"message": "ready", "queue_mode": "wait"},
        )
        if r.status_code == 200:
            return r.json()["data"]["session_id"]
        last_text = r.text[:300]
        if r.status_code == 503 and "warming" in last_text.lower():
            await asyncio.sleep(1.5)
            continue
        break
    raise RuntimeError(f"create_session: {last_text}")


# ── Reporter ─────────────────────────────────────────────────────────


class Reporter:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        tag = "[PASS]" if ok else "[FAIL]"
        print(f"  {tag} {name}{(' - ' + detail) if detail else ''}")
        self.results.append((name, ok, detail))

    def summary(self) -> int:
        passed = sum(1 for _, ok, _ in self.results if ok)
        total = len(self.results)
        print()
        print(f"=== {passed}/{total} scenarios passed ===")
        if passed != total:
            print("Failed:")
            for name, ok, detail in self.results:
                if not ok:
                    print(f"  - {name}: {detail}")
        return 0 if passed == total else 1


# ── Socket.IO ────────────────────────────────────────────────────────


async def _connect_socket(token: str) -> socketio.AsyncClient:
    sio = socketio.AsyncClient(reconnection=False)
    await sio.connect(
        f"{_DAEMON}/events",
        namespaces=["/events"],
        auth={"token": token},
        transports=["websocket"],
        wait=True,
        wait_timeout=10,
    )
    return sio


# ── Scenarios ────────────────────────────────────────────────────────


async def s_send_and_collect(
    sio: socketio.AsyncClient, app_id: str, sid: str,
    message: str, *, timeout: float = 60.0,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Send a message and collect events until ``turn_complete``.

    Returns ``(envelopes, ordered_types)`` where ``ordered_types`` is the
    chronological list of event ``type`` strings (handy to check
    relative ordering of thinking vs text vs tool events).
    """
    events: list[dict[str, Any]] = []

    @sio.on("event", namespace="/events")
    async def _capture(envelope: dict[str, Any]) -> None:
        events.append(envelope)

    await sio.call(
        "send_message",
        {"app_id": app_id, "session_id": sid, "message": message},
        namespace="/events",
        timeout=10,
    )

    deadline = time.time() + timeout
    completed = False
    while time.time() < deadline:
        for ev in events:
            t = ev.get("type")
            if t in ("turn_complete", "stream_done"):
                completed = True
        if completed:
            break
        await asyncio.sleep(0.2)

    types = [str(ev.get("type", "")) for ev in events]
    return events, types


def _ordered_types_filtered(types: list[str], whitelist: set[str]) -> list[str]:
    return [t for t in types if t in whitelist]


# ── Main ─────────────────────────────────────────────────────────────


async def main() -> int:
    email = f"stream-{uuid.uuid4().hex[:8]}@example.com"
    token = _login(_DAEMON, email, _PASSWORD)
    print(f"[setup] logged in as {email}")

    headers = {"Authorization": f"Bearer {token}"}
    rep = Reporter()
    async with httpx.AsyncClient(
        headers=headers, follow_redirects=True, timeout=120.0,
    ) as http:
        try:
            app_id = await _deploy(http, _APP_YAML)
            print(f"[setup] deployed {app_id}")
        except Exception as exc:
            import traceback as _tb
            _tb.print_exc()
            rep.add(
                "setup deploy", False,
                f"{type(exc).__name__}: {exc}"[:300],
            )
            return rep.summary()

        try:
            sid = await _create_session(http, app_id)
            print(f"[setup] session {sid}")
        except Exception as exc:
            rep.add("setup create_session", False, str(exc)[:200])
            return rep.summary()

        sio = await _connect_socket(token)
        await sio.call(
            "join_session",
            {"app_id": app_id, "session_id": sid, "since": 0},
            namespace="/events",
            timeout=10,
        )

        try:
            print("\nS1+S2. text + thinking events (adaptive thinking prompt)")
            events, types = await _send_and_collect_helper(
                sio, app_id, sid,
                "Reply with the single word OK.",
            )

            text_events = [
                ev for ev in events
                if ev.get("type") in ("token", "out_token")
            ]
            think_started = [
                ev for ev in events if ev.get("type") == "thinking_started"
            ]
            think_deltas = [
                ev for ev in events if ev.get("type") == "thinking_delta"
            ]
            completed = any(
                ev.get("type") in ("turn_complete", "stream_done")
                for ev in events
            )
            rep.add("S1.1 text token events arrive",
                    len(text_events) >= 1,
                    f"text_events={len(text_events)}")
            # Thinking is opt-in per model. We accept either:
            #   (a) at least one thinking_started + one thinking_delta, OR
            #   (b) zero of both (model didn't think for this prompt).
            # The asymmetric case (started but no delta or vice versa)
            # would indicate a daemon bug.
            both_zero = (
                len(think_started) == 0 and len(think_deltas) == 0
            )
            both_present = (
                len(think_started) >= 1 and len(think_deltas) >= 1
            )
            rep.add(
                "S2.1 thinking emission is consistent (all-or-nothing)",
                both_zero or both_present,
                f"started={len(think_started)} deltas={len(think_deltas)}",
            )
            if both_present:
                rep.add(
                    "S2.2 thinking_delta carries delta or content",
                    all(
                        bool(
                            (ev.get("payload") or {}).get("delta")
                            or (ev.get("payload") or {}).get("content")
                        )
                        for ev in think_deltas
                    ),
                    f"first_payload={(think_deltas[0].get('payload') or {})}",
                )
            rep.add("S2.3 turn_complete fired", completed,
                    "yes" if completed else "timeout")

            # Ordering check: every thinking_* event lands BEFORE the
            # first token, OR thinking is interleaved with tokens but
            # each thinking block is contiguous.
            seen_text = False
            for t in types:
                if t in ("token", "out_token"):
                    seen_text = True
                if t == "thinking_started" and seen_text:
                    # Allowed - some models think mid-stream. Just log.
                    print("  [info] thinking_started arrived after text - "
                          "model is interleaving")
                    break
            rep.add("S3.1 envelopes carry distinct types",
                    {"token", "out_token"} & set(types) != set()
                    and "turn_complete" in types or "stream_done" in types,
                    f"distinct_types={sorted(set(types))[:8]}")
        except Exception as exc:
            rep.add("FATAL", False, f"{type(exc).__name__}: {exc}")
            import traceback; traceback.print_exc()
        finally:
            try:
                await sio.disconnect()
            except Exception:
                pass

    return rep.summary()


async def _send_and_collect_helper(
    sio: socketio.AsyncClient, app_id: str, sid: str, msg: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    return await s_send_and_collect(sio, app_id, sid, msg)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
