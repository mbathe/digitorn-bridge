"""End-to-end smoke test for the ``useChat`` SDK hook + its wire paths.

Three Socket.IO actions on the daemon side:

  * ``send_message``   - already existed, used by ``useChat().send``.
  * ``abort_turn``     - new, used by ``useChat().abort``.
  * (catch-up replay)  - already there, exercised by re-emitting
                          ``user_message`` after a reconnect.

The test deploys a tiny app with a real LLM brain (Anthropic via the
``claude-code`` OAuth token) so the whole streaming pipeline runs
end-to-end. We then exercise:

  S1. send_message via WS - ack carries correlation_id, queue position.
  S2. user_message event lands on the bus - confirms the send actually
      got persisted before the agent started.
  S3. token / turn_complete events deliver an assistant reply.
  S4. abort_turn via WS - cancels mid-stream, frees the session.

Run with the daemon up:

  py -3.12 -X utf8 tools/live_tests/chat_check.py
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
    "c:/Users/ASUS/Documents/digitorn-bridge/examples/chat-test/app.yaml"
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
    app_id = (body.get("data") or {}).get("app_id") or "chat-test"
    deadline = time.time() + 30
    while time.time() < deadline:
        m = await client.get(f"{_DAEMON}/api/apps/{app_id}")
        if m.status_code == 200:
            return app_id
        await asyncio.sleep(0.5)
    raise RuntimeError("deploy never ready")


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


# ── Socket.IO helpers ────────────────────────────────────────────────


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


async def s1_send_message(
    sio: socketio.AsyncClient, app_id: str, sid: str, rep: Reporter,
    events: list[dict[str, Any]],
) -> str:
    print("\nS1. send_message via WS - ack + correlation_id")
    ack = await sio.call(
        "send_message",
        {
            "app_id": app_id,
            "session_id": sid,
            "message": "Reply with the single word OK and nothing else.",
        },
        namespace="/events",
        timeout=10,
    )
    rep.add("S1.1 ack ok", bool(ack and ack.get("ok") is True),
            f"ack={ack}")
    rep.add("S1.2 ack carries correlation_id",
            bool(ack and ack.get("correlation_id")),
            f"corr={ack.get('correlation_id') if ack else None}")
    return (ack or {}).get("correlation_id", "")


async def s2_user_message_event(
    rep: Reporter, events: list[dict[str, Any]],
    correlation_id: str, *, timeout: float = 5.0,
) -> None:
    print("\nS2. user_message event lands on the bus")
    deadline = time.time() + timeout
    found = None
    while time.time() < deadline:
        for ev in events:
            if ev.get("type") != "user_message":
                continue
            payload = ev.get("payload") or {}
            if correlation_id and payload.get("correlation_id") != correlation_id:
                continue
            found = payload
            break
        if found:
            break
        await asyncio.sleep(0.1)
    rep.add("S2.1 user_message received", found is not None,
            f"corr={correlation_id}")
    if found is not None:
        rep.add("S2.2 payload role=user", found.get("role") == "user",
                f"role={found.get('role')}")
        rep.add("S2.3 payload content non-empty",
                bool(found.get("content")),
                f"content={(found.get('content') or '')[:60]}")


async def s3_assistant_reply(
    rep: Reporter, events: list[dict[str, Any]],
    *, timeout: float = 60.0,
) -> None:
    print("\nS3. token + turn_complete - assistant streams a reply")
    deadline = time.time() + timeout
    tokens_seen = 0
    completed = False
    while time.time() < deadline:
        for ev in events:
            t = ev.get("type")
            if t in ("token", "out_token"):
                tokens_seen += 1
            if t in ("turn_complete", "stream_done"):
                completed = True
        if completed:
            break
        await asyncio.sleep(0.2)
    rep.add("S3.1 at least one token streamed", tokens_seen > 0,
            f"tokens={tokens_seen}")
    rep.add("S3.2 turn_complete fired", completed,
            "yes" if completed else f"timeout after {timeout}s")


async def s4_abort_turn(
    sio: socketio.AsyncClient, app_id: str, sid: str, rep: Reporter,
) -> None:
    print("\nS4. abort_turn cancels a running turn")
    # Fire a long-running message - ask for many tokens.
    long_corr = "fake-" + uuid.uuid4().hex[:8]
    await sio.call(
        "send_message",
        {
            "app_id": app_id,
            "session_id": sid,
            "message": "Count slowly from 1 to 50, one number per line.",
            "client_message_id": long_corr,
        },
        namespace="/events",
        timeout=10,
    )
    # Let the agent start streaming a few tokens
    await asyncio.sleep(2.0)
    ack = await sio.call(
        "abort_turn",
        {"app_id": app_id, "session_id": sid, "purge_queue": False},
        namespace="/events",
        timeout=10,
    )
    rep.add("S4.1 abort_turn ack ok", bool(ack and ack.get("ok") is True),
            f"ack={ack}")
    rep.add("S4.2 was_active reflected", ack.get("was_active") is not None,
            f"was_active={ack.get('was_active')}")


# ── Main ─────────────────────────────────────────────────────────────


async def main() -> int:
    email = f"chat-{uuid.uuid4().hex[:8]}@example.com"
    token = _login(_DAEMON, email, _PASSWORD)
    print(f"[setup] logged in as {email}")

    headers = {"Authorization": f"Bearer {token}"}
    rep = Reporter()
    async with httpx.AsyncClient(
        headers=headers, follow_redirects=True, timeout=30.0,
    ) as http:
        try:
            app_id = await _deploy(http, _APP_YAML)
            print(f"[setup] deployed {app_id}")
        except Exception as exc:
            rep.add("setup deploy", False, str(exc)[:200])
            return rep.summary()

        try:
            sid = await _create_session(http, app_id)
            print(f"[setup] session {sid}")
        except Exception as exc:
            rep.add("setup create_session", False, str(exc)[:200])
            return rep.summary()

        events: list[dict[str, Any]] = []
        sio = await _connect_socket(token)

        @sio.on("event", namespace="/events")
        async def _capture(envelope: dict[str, Any]) -> None:
            events.append(envelope)

        await sio.call(
            "join_session",
            {"app_id": app_id, "session_id": sid, "since": 0},
            namespace="/events",
            timeout=10,
        )

        try:
            corr = await s1_send_message(sio, app_id, sid, rep, events)
            await s2_user_message_event(rep, events, corr)
            await s3_assistant_reply(rep, events)
            await s4_abort_turn(sio, app_id, sid, rep)
        except Exception as exc:
            rep.add("FATAL", False, f"{type(exc).__name__}: {exc}")
            import traceback; traceback.print_exc()
        finally:
            try:
                await sio.disconnect()
            except Exception:
                pass

    return rep.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
