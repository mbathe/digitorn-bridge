"""Test Socket.IO join_session cross-user authorization.

User A has a private session. User B tries to join A's session room via
Socket.IO join_session event. Backend should refuse.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import httpx
import socketio


async def main() -> None:
    # Create user A session
    r = httpx.post("http://127.0.0.1:8000/auth/login",
        json={"email": "tester2@prod.local", "password": "TestProd1234!"})
    tkA = r.json()["access_token"]

    r = httpx.post("http://127.0.0.1:8000/auth/login",
        json={"email": "tester3@prod.local", "password": "TestProd1234!"})
    tkB = r.json()["access_token"]

    sid = f"auth-{uuid.uuid4().hex[:6]}"
    # user A sends a message
    httpx.post(
        f"http://127.0.0.1:8000/api/apps/digitorn-chat/sessions/{sid}/messages",
        headers={"Authorization": f"Bearer {tkA}"},
        json={"message": "Hi, my secret is PINEAPPLE-TOPSECRET-42"},
    )
    await asyncio.sleep(2)

    # User B tries to join A's session
    sio = socketio.AsyncClient(reconnection=False)
    events_captured = []

    @sio.on("event", namespace="/events")
    async def on_event(payload):
        events_captured.append(payload)

    try:
        await asyncio.wait_for(
            sio.connect(
                "http://127.0.0.1:8000",
                auth={"token": tkB},
                namespaces=["/events"],
                transports=["websocket"],
            ), timeout=8,
        )
    except Exception as e:
        print(f"connect failed: {e}")
        return

    try:
        ack = await asyncio.wait_for(
            sio.call("join_session",
                {"app_id": "digitorn-chat", "session_id": sid, "since": 0},
                namespace="/events", timeout=5),
            timeout=8,
        )
        print(f"join_session ack for user B: {ack}")
    except Exception as e:
        print(f"join_session failed: {e}")

    await asyncio.sleep(3)

    # Check what B received
    secret_leaked = any("PINEAPPLE" in str(ev) for ev in events_captured)
    print(f"events captured: {len(events_captured)}")
    print(f"SECRET LEAKED: {secret_leaked}")
    # Show types
    types = sorted({ev.get("type") for ev in events_captured if isinstance(ev, dict)})
    print(f"event types: {types}")
    if secret_leaked:
        for ev in events_captured:
            if "PINEAPPLE" in str(ev):
                print(f"LEAK in: {ev}")
                break

    await sio.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
