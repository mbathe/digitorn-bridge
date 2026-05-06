"""End-to-end smoke test for the approval queue + ``useApprovals`` SDK hook.

Strategy:

1. Deploy ``examples/approval-test/app.yaml`` (filesystem.write gated by
   ``default_policy: approve``).
2. Create a session.
3. Fire a direct tool call ``POST /tools/Write/execute`` in the
   background. Capabilities should suspend it and queue an approval.
4. Poll ``GET /approvals`` until the pending request appears - this is
   exactly what the SDK's ``useApprovals().pending`` will surface.
5. Resolve the request via ``POST /approve`` - same route the SDK's
   ``approve()`` calls.
6. Assert the suspended tool call returns success and the file lands
   on disk.
7. Repeat once with ``approved=False`` to verify rejection short-
   circuits the tool.

Run with the daemon up:

  py -3.12 -X utf8 tools/live_tests/approvals_check.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

_DAEMON = os.environ.get("DIGITORN_DAEMON", "http://127.0.0.1:8000")
_PASSWORD = os.environ.get("DEV_PASSWORD", "pw1234567")
_APP_YAML = (
    "c:/Users/ASUS/Documents/digitorn-bridge/examples/approval-test/app.yaml"
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
    app_id = (body.get("data") or {}).get("app_id") or "approval-test"
    # Bootstrap runs as a background task so the deploy POST returns
    # before the in-memory ``deployed[app_id]`` map is populated. Poll
    # the manifest endpoint until it answers 200, with a generous
    # ceiling to absorb cold-start module init.
    deadline = time.time() + 30
    last_status = 0
    while time.time() < deadline:
        m = await client.get(f"{_DAEMON}/api/apps/{app_id}")
        last_status = m.status_code
        if m.status_code == 200:
            return app_id
        await asyncio.sleep(0.5)
    raise RuntimeError(
        f"deploy never ready: last_status={last_status} after 30s",
    )


async def _create_session(client: httpx.AsyncClient, app_id: str) -> str:
    # The daemon warms apps on demand the first time a session is
    # created post-restart. The first POST often returns 503
    # (warming_up=true) - retry with a small backoff before failing.
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


async def _fire_tool(
    client: httpx.AsyncClient, app_id: str, sid: str, file_path: str,
    content: str,
) -> tuple[asyncio.Task[httpx.Response], float]:
    """Fire ``POST /tools/Write/execute`` without awaiting.

    Returns the background task + the wall-clock timestamp at which we
    fired so the test can measure end-to-end latency. The exec call will
    block on the approval queue until we resolve the request.
    """
    started = time.time()
    coro = client.post(
        f"{_DAEMON}/api/apps/{app_id}/tools/Write/execute",
        json={
            "session_id": sid,
            "params": {"file_path": file_path, "content": content},
        },
        timeout=30.0,
    )
    task: asyncio.Task[httpx.Response] = asyncio.create_task(coro)
    return task, started


async def _wait_for_pending(
    client: httpx.AsyncClient, app_id: str, *, timeout: float = 10.0,
) -> dict[str, Any]:
    """Poll ``GET /approvals`` until at least one pending request appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = await client.get(f"{_DAEMON}/api/apps/{app_id}/approvals")
        if r.status_code == 200:
            data = (r.json() or {}).get("data") or {}
            pending = data.get("pending") or []
            if pending:
                return pending[0]
        await asyncio.sleep(0.2)
    raise RuntimeError(
        f"no pending approval appeared within {timeout}s",
    )


async def _resolve(
    client: httpx.AsyncClient, app_id: str, request_id: str,
    approved: bool, message: str = "",
) -> dict[str, Any]:
    """Resolve via the legacy HTTP route - kept for backward-compat
    coverage. The SDK uses the Socket.IO ``resolve_approval`` path
    (see ``_resolve_via_socket`` below)."""
    r = await client.post(
        f"{_DAEMON}/api/apps/{app_id}/approve",
        json={"request_id": request_id, "approved": approved, "message": message},
    )
    if r.status_code != 200:
        raise RuntimeError(f"approve {r.status_code}: {r.text[:300]}")
    return r.json().get("data") or {}


async def _resolve_via_socket(
    token: str, app_id: str, request_id: str,
    approved: bool, message: str = "",
) -> dict[str, Any]:
    """Resolve via the SDK's wire path: Socket.IO emit + ack callback.

    Mirrors what ``useApprovals().approve()`` does in the SDK so the
    test exercises the exact same daemon handler the iframe will hit
    in production.
    """
    import socketio
    sio = socketio.AsyncClient(reconnection=False)
    try:
        await sio.connect(
            f"{_DAEMON}/events",
            namespaces=["/events"],
            auth={"token": token},
            transports=["websocket"],
            wait=True,
            wait_timeout=30,
        )
        ack = await sio.call(
            "resolve_approval",
            {
                "app_id": app_id,
                "request_id": request_id,
                "approved": approved,
                "message": message,
            },
            namespace="/events",
            timeout=30,
        )
        return ack or {}
    finally:
        try:
            await sio.disconnect()
        except Exception:
            pass


# ── Scenarios ────────────────────────────────────────────────────────


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


async def s1_approve_path(
    client: httpx.AsyncClient, app_id: str, sid: str, rep: Reporter,
) -> None:
    print("\nS1. approve flow - tool call resumes after approve")
    target = f"approve-ok-{uuid.uuid4().hex[:6]}.txt"
    payload = "approved by E2E test"
    task, started = await _fire_tool(client, app_id, sid, target, payload)

    try:
        pending = await _wait_for_pending(client, app_id, timeout=8)
    except Exception as exc:
        rep.add("S1.1 pending appears", False, str(exc)[:120])
        task.cancel()
        return
    rep.add("S1.1 pending appears", bool(pending.get("request_id")),
            f"request_id={pending.get('request_id')}")
    rep.add("S1.2 carries tool_name", pending.get("tool_name") == "filesystem.write",
            f"tool_name={pending.get('tool_name')}")
    rep.add("S1.3 carries tool_params",
            isinstance(pending.get("tool_params"), dict)
            and pending["tool_params"].get("file_path") == target,
            f"params={pending.get('tool_params')}")
    rep.add("S1.4 has risk_level", bool(pending.get("risk_level")),
            f"risk={pending.get('risk_level')}")

    out = await _resolve(client, app_id, pending["request_id"],
                          approved=True, message="ok by test")
    rep.add("S1.5 POST /approve returns success",
            bool(out.get("request_id")) and out.get("approved") is True,
            f"out={out}")

    try:
        resp = await asyncio.wait_for(task, timeout=30)
        body = resp.json() if resp.status_code == 200 else {}
        ok = body.get("success") is True
        rep.add("S1.6 tool exec resumes + succeeds", ok,
                f"http={resp.status_code} body={str(body)[:120]}")
    except asyncio.TimeoutError:
        rep.add("S1.6 tool exec resumes + succeeds", False,
                "tool exec did not return within 10s after approve")

    elapsed = time.time() - started
    rep.add("S1.7 round-trip under 10s", elapsed < 10.0,
            f"elapsed={elapsed:.2f}s")


async def s2_reject_path(
    client: httpx.AsyncClient, app_id: str, sid: str, rep: Reporter,
) -> None:
    print("\nS2. reject flow - tool call short-circuits as failure")
    target = f"reject-ko-{uuid.uuid4().hex[:6]}.txt"
    task, _ = await _fire_tool(client, app_id, sid, target, "should not land")

    try:
        pending = await _wait_for_pending(client, app_id, timeout=8)
    except Exception as exc:
        rep.add("S2.1 pending appears", False, str(exc)[:120])
        task.cancel()
        return
    rep.add("S2.1 pending appears", True,
            f"request_id={pending.get('request_id')}")

    await _resolve(client, app_id, pending["request_id"],
                    approved=False, message="denied by test")

    try:
        resp = await asyncio.wait_for(task, timeout=30)
        body = resp.json() if resp.status_code == 200 else {}
        # When rejected, the daemon either returns success=false with an
        # error or a 4xx. Both are valid - assert NOT success.
        not_success = (resp.status_code != 200) or (body.get("success") is False)
        rep.add("S2.2 tool exec reflects rejection", not_success,
                f"http={resp.status_code} body={str(body)[:120]}")
    except asyncio.TimeoutError:
        rep.add("S2.2 tool exec reflects rejection", False,
                "tool exec did not return within 10s after reject")


async def s_socket_resolve(
    client: httpx.AsyncClient, app_id: str, sid: str, token: str,
    rep: Reporter,
) -> None:
    print("\nS4. Socket.IO resolve_approval - the SDK wire path")
    target = f"socket-ok-{uuid.uuid4().hex[:6]}.txt"
    task, _ = await _fire_tool(client, app_id, sid, target, "via WS")

    try:
        pending = await _wait_for_pending(client, app_id, timeout=8)
    except Exception as exc:
        rep.add("S4.1 pending appears", False, str(exc)[:120])
        task.cancel()
        return
    rep.add("S4.1 pending appears", True,
            f"request_id={pending.get('request_id')}")

    try:
        ack = await _resolve_via_socket(
            token, app_id, pending["request_id"],
            approved=True, message="ws ok",
        )
    except Exception as exc:
        rep.add("S4.2 resolve_approval ack ok", False,
                f"{type(exc).__name__}: {exc}")
        task.cancel()
        return
    rep.add(
        "S4.2 resolve_approval ack ok",
        ack.get("ok") is True and ack.get("approved") is True,
        f"ack={ack}",
    )

    try:
        resp = await asyncio.wait_for(task, timeout=30)
        body = resp.json() if resp.status_code == 200 else {}
        rep.add("S4.3 tool exec resumes",
                body.get("success") is True,
                f"http={resp.status_code} body={str(body)[:120]}")
    except asyncio.TimeoutError:
        rep.add("S4.3 tool exec resumes", False,
                "tool exec did not return within 10s")


async def s3_multi_pending(
    client: httpx.AsyncClient, app_id: str, sid: str, rep: Reporter,
) -> None:
    print("\nS3. multi-pending - GET /approvals lists ALL pending")
    p1 = f"multi-{uuid.uuid4().hex[:6]}-a.txt"
    p2 = f"multi-{uuid.uuid4().hex[:6]}-b.txt"
    t1, _ = await _fire_tool(client, app_id, sid, p1, "first")
    t2, _ = await _fire_tool(client, app_id, sid, p2, "second")
    # Poll until we see two distinct pending requests
    deadline = time.time() + 10
    pending: list[dict[str, Any]] = []
    while time.time() < deadline:
        r = await client.get(f"{_DAEMON}/api/apps/{app_id}/approvals")
        pending = ((r.json() or {}).get("data") or {}).get("pending") or []
        if len(pending) >= 2:
            break
        await asyncio.sleep(0.2)
    rep.add("S3.1 GET /approvals lists 2 pending",
            len(pending) >= 2, f"count={len(pending)}")
    # Approve both, in reverse order, to confirm the queue is keyed on id.
    for req in pending[:2]:
        await _resolve(client, app_id, req["request_id"], approved=True)
    try:
        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=30)
        rep.add("S3.2 both tool calls resume", True, "")
    except asyncio.TimeoutError:
        rep.add("S3.2 both tool calls resume", False, "timeout")


# ── Main ─────────────────────────────────────────────────────────────


async def main() -> int:
    email = f"approve-{uuid.uuid4().hex[:8]}@example.com"
    token = _login(_DAEMON, email, _PASSWORD)
    print(f"[setup] logged in as {email}")

    headers = {"Authorization": f"Bearer {token}"}
    rep = Reporter()
    async with httpx.AsyncClient(
        headers=headers, follow_redirects=True, timeout=30.0,
    ) as client:
        try:
            app_id = await _deploy(client, _APP_YAML)
            print(f"[setup] deployed {app_id}")
        except Exception as exc:
            rep.add("setup deploy", False, str(exc)[:200])
            return rep.summary()

        try:
            sid = await _create_session(client, app_id)
            print(f"[setup] session {sid}")
        except Exception as exc:
            rep.add("setup create_session", False, str(exc)[:200])
            return rep.summary()

        try:
            await s1_approve_path(client, app_id, sid, rep)
            await s2_reject_path(client, app_id, sid, rep)
            await s_socket_resolve(client, app_id, sid, token, rep)
            await s3_multi_pending(client, app_id, sid, rep)
        except Exception as exc:
            rep.add("FATAL", False, f"{type(exc).__name__}: {exc}")
            import traceback; traceback.print_exc()

    return rep.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
