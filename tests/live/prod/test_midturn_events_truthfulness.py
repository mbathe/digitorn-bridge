"""Mid-turn events truthfulness check - REAL Socket.IO subscription.

Subscribe to the daemon's `/events` namespace BEFORE sending a message,
capture every envelope received during the turn, then verify:
  - sum(`out_token`.count) == provider-billed completion_tokens
  - sum(`in_token`.count)  == provider-billed prompt_tokens
  - `result`.usage.*       == provider-billed numbers
  - `result`.context.total_estimated_tokens  > 0

If any value drifts, the test fails. No mocks. Real daemon, real Ollama.
"""
from __future__ import annotations
import asyncio
import glob
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages"))

import socketio  # noqa: E402
from digitorn.testing.client import DevClient  # noqa: E402

APP_YAML = Path(__file__).parent / "coding-assistant-local.yaml"
WORKSPACE = Path(__file__).parent / "workspace"
APP_ID = "prod-coding-assistant-local"
DIAG_DIR = Path(os.environ["DIGITORN_DIAG_DIR"])


def clear_diag():
    for f in glob.glob(str(DIAG_DIR / "wire_*.json")):
        try: os.remove(f)
        except OSError: pass


def collect_wire_usage_after(t0: float) -> tuple[int, int]:
    p = c = 0
    for f in sorted(glob.glob(str(DIAG_DIR / "wire_usage_*.json"))):
        ts = int(Path(f).stem.split("_")[-1]) / 1000.0
        if ts < t0:
            continue
        u = json.loads(Path(f).read_text(encoding="utf-8"))
        p += int(u.get("prompt_tokens", 0))
        c += int(u.get("completion_tokens", 0))
    return p, c


async def run() -> int:
    clear_diag()

    events: list[dict] = []
    result_event_seen = asyncio.Event()

    # Create an authenticated client (register a fresh account for this run).
    import uuid as _uuid
    import httpx as _httpx
    test_user = f"tok-ws-{_uuid.uuid4().hex[:8]}"
    test_email = f"{test_user}@example.com"
    test_pw = "digitorn-test-passw0rd-123"
    try:
        reg = _httpx.post(
            "http://127.0.0.1:8000/auth/register",
            json={
                "username": test_user,
                "email": test_email,
                "password": test_pw,
            },
            timeout=15,
        )
        if reg.status_code not in (200, 201):
            reg = _httpx.post(
                "http://127.0.0.1:8000/auth/login",
                json={"username": test_user, "password": test_pw},
                timeout=15,
            )
        access_token = reg.json().get("access_token") or ""
    except Exception as exc:
        print(f"(auth bootstrap failed: {exc})")
        access_token = ""
    print(f"auth token: {'acquired' if access_token else 'EMPTY - socket will be rejected'}")

    sio = socketio.AsyncClient()

    @sio.on("event", namespace="/events")
    async def on_event(data):
        events.append(data)
        if data.get("type") == "result":
            result_event_seen.set()

    await sio.connect(
        "http://127.0.0.1:8000",
        namespaces=["/events"],
        transports=["websocket"],
        auth={"token": access_token} if access_token else None,
    )
    print("socket.io connected")

    # Deploy + create session via an HTTP client using the SAME token -
    # the session must belong to the user whose socket is subscribed,
    # otherwise join_session returns "access denied".
    client = DevClient.with_token(
        access_token, daemon_url="http://127.0.0.1:8000",
        auto_approve=True, timeout=180,
    )
    app = client.deploy(APP_YAML, force=True, wait=5)
    print(f"deployed: tools={app.total_tools}")
    session = client.create_session(APP_ID, workspace=str(WORKSPACE))
    print(f"session: {session.session_id}")

    # Join the session room explicitly. If this succeeds we get
    # events routed to `session:<sid>` which is where the daemon
    # publishes tool_start/tool_call/result/out_token/in_token.
    # (We're already auto-joined to `user:<uid>` on connect.)
    ack = await sio.call(
        "join_session",
        {"app_id": APP_ID, "session_id": session.session_id},
        namespace="/events",
        timeout=5,
    )
    print(f"joined session room: {ack}\n")
    if not ack.get("ok"):
        # On-the-wire rooms aren't available - but the user room gets
        # ALL events for this user. We're already in it via on_connect.
        print("(falling back to user-wide subscription)")

    # Send message (non-blocking; we want to listen while turn runs)
    t0 = time.time()
    send_task = asyncio.create_task(
        asyncio.to_thread(
            client.send, session,
            "Tell me in one sentence what python is.",
            180,
        ),
    )

    # Wait for result event or timeout
    try:
        await asyncio.wait_for(result_event_seen.wait(), timeout=120)
    except asyncio.TimeoutError:
        print("(timed out waiting for `result` event)")
    await send_task
    await asyncio.sleep(0.5)

    await sio.disconnect()

    wire_p, wire_c = collect_wire_usage_after(t0)
    print(f"ground truth (provider wire): prompt={wire_p}  completion={wire_c}\n")

    # Categorize
    by_type: dict[str, list] = {}
    for e in events:
        by_type.setdefault(e.get("type"), []).append(e)
    print("events captured during turn (by type):")
    for t, lst in sorted(by_type.items()):
        print(f"  {t:<20} {len(lst)}")

    out_token_events   = by_type.get("out_token", [])
    in_token_events    = by_type.get("in_token", [])
    token_events       = by_type.get("token", [])
    token_usage_events = by_type.get("token_usage", [])
    result_events      = by_type.get("result", [])

    def sum_counts(evs):
        total = 0
        for e in evs:
            p = e.get("payload") or {}
            total += int(p.get("count", 0))
        return total

    out_sum = sum_counts(out_token_events)
    in_sum  = sum_counts(in_token_events)

    result_data = (result_events[-1].get("payload") if result_events else {}) or {}
    result_usage = result_data.get("usage") or {}
    r_input  = int(result_usage.get("input_tokens")  or 0)
    r_output = int(result_usage.get("output_tokens") or 0)
    r_total_input  = int(result_usage.get("total_input_tokens")  or 0)
    r_total_output = int(result_usage.get("total_output_tokens") or 0)
    ctx_snap = result_data.get("context") or {}
    ctx_total = int(ctx_snap.get("total_estimated_tokens") or 0)

    checks = []
    def check(label, got, expected):
        ok = got == expected
        mark = "EXACT" if ok else "DRIFT"
        print(f"  {label:<46}  got={got:>7}  truth={expected:>7}  {mark}")
        checks.append(ok)

    print("\n── During-turn event deltas vs provider billing ──")
    check("sum(out_token.count)",        out_sum,  wire_c)
    check("sum(in_token.count)",         in_sum,   wire_p)

    print("\n── Final `result` event (end-of-turn) ──")
    check("result.usage.input_tokens",   r_input,  wire_p)
    check("result.usage.output_tokens",  r_output, wire_c)
    check("result.usage.total_input",    r_total_input,  wire_p)
    check("result.usage.total_output",   r_total_output, wire_c)
    print(f"  result.context.total_estimated_tokens = {ctx_total}")
    checks.append(ctx_total > 0)
    print(f"  text deltas seen (token events)        = {len(token_events)}")

    print("\n" + "=" * 70)
    if all(checks):
        print("PASS - every mid-turn and end-of-turn event value is byte-equal "
              "to provider billing.")
        return 0
    print(f"FAIL - {sum(1 for x in checks if not x)}/{len(checks)} checks diverged.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
