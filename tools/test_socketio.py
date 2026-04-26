"""End-to-end Socket.IO test against the digitorn daemon.

Connects, joins a session, sends a message, prints every event that
arrives until the turn is done (or 30 s timeout).

Usage:
    py -3.12 tools/test_socketio.py

Configure via env vars (optional, defaults shown below).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request
import urllib.parse
from collections import Counter

import socketio  # pip install python-socketio[client]


DAEMON_URL = os.environ.get("DAEMON_URL", "http://localhost:8000")
USERNAME = os.environ.get("AUDIT_USER", "audit1777042175")
PASSWORD = os.environ.get("AUDIT_PASS", "NewPass456!")
APP_ID = os.environ.get("APP_ID", "digitorn-code")
MESSAGE = os.environ.get("MESSAGE", "Bonjour, en une phrase courte stp.")


def http_post(path: str, body: dict, *, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        DAEMON_URL + path, headers=headers,
        data=json.dumps(body).encode(), method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


async def main() -> None:
    # 1. Login → get an access token.
    login = http_post("/auth/login", {"username": USERNAME, "password": PASSWORD})
    token = login["access_token"]
    print(f"\n[1] login OK user={USERNAME} token_len={len(token)}")

    # 2. Create a fresh session via REST so we know it exists and we own it.
    create = http_post(
        f"/api/apps/{APP_ID}/sessions", {}, token=token,
    )
    sid = create["data"]["session_id"]
    print(f"[2] created session sid={sid}")

    # 3. Set up the Socket.IO client.
    sio = socketio.AsyncClient(
        logger=False, engineio_logger=False,
        reconnection=False,
    )

    received: list[str] = []
    turn_done = asyncio.Event()

    @sio.on("event", namespace="/events")
    async def on_event(envelope: dict) -> None:
        """The ONE listener — every server-side event flows here."""
        etype = envelope.get("type", "?")
        seq = envelope.get("seq", "-")
        received.append(etype)
        # Pretty-print the meaningful payload, truncate noisy ones.
        payload = envelope.get("payload") or {}
        if etype == "token":
            preview = (payload.get("content") or "")[:30].replace("\n", " ")
            print(f"  seq={seq:>4} {etype}: {preview!r}")
        else:
            preview = json.dumps(payload, ensure_ascii=False)[:120]
            print(f"  seq={seq:>4} {etype}: {preview}")
        if etype in ("turn_complete", "message_done", "error"):
            # Wait one extra tick for trailing events, then unblock main.
            await asyncio.sleep(0.5)
            turn_done.set()

    @sio.event(namespace="/events")
    async def connect_error(data) -> None:
        print(f"[!] connect_error: {data}")

    # 4. Connect to the /events namespace with the bearer token.
    print(f"[3] connecting to {DAEMON_URL}/events ...")
    await sio.connect(
        DAEMON_URL,
        auth={"token": token},
        namespaces=["/events"],
        transports=["websocket", "polling"],
    )
    print(f"    connected, sid={sio.sid}")

    # 5. Join the session — server sends snapshots immediately.
    print(f"[4] join_session ...")
    ack = await sio.call(
        "join_session",
        {"app_id": APP_ID, "session_id": sid},
        namespace="/events", timeout=10,
    )
    print(f"    ack: {ack}")
    if not ack.get("ok"):
        print("    ✗ join failed, abort")
        await sio.disconnect()
        return

    # Give the snapshots a moment to land before sending.
    await asyncio.sleep(0.3)

    # 6. Send a message — triggers the full turn cascade.
    print(f"[5] send_message: {MESSAGE!r}")
    ack2 = await sio.call(
        "send_message",
        {"app_id": APP_ID, "session_id": sid, "message": MESSAGE},
        namespace="/events", timeout=10,
    )
    print(f"    ack: {ack2}\n")

    # 7. Wait until the turn ends (or 30 s).
    try:
        await asyncio.wait_for(turn_done.wait(), timeout=30)
    except asyncio.TimeoutError:
        print("\n[!] timeout — turn did not complete in 30 s")

    # 8. Summary.
    print("\n" + "=" * 60)
    print(f"Total events received: {len(received)}")
    for typ, n in Counter(received).most_common():
        print(f"  {typ:30}  x{n}")
    print("=" * 60)

    await sio.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
