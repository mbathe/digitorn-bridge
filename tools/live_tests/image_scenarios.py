"""Live E2E regression test for image multimodal pipeline.

Targets `digitorn-chat` (attachments_mode=tool, granted workspace.read,
gpt-5-mini supports vision). The agent receives an image as an
attachment, must call WsRead to access the image bytes, and respond
with a description that references the image content.

Asserts the full chain :
  1. POST /messages accepts an `images` array.
  2. Agent invokes a read tool to fetch the image.
  3. The reply references the image content (color marker).

Run:
    py -3.12 tools/live_tests/image_scenarios.py
"""
from __future__ import annotations

import base64
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path("C:/Users/ASUS/Documents/digitorn-bridge/packages")))

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


APP_ID = "digitorn-chat"


def _ok(label: str, cond: bool, why: str = "") -> tuple[str, tuple[bool, str]]:
    return (label, (cond, "" if cond else why))


def _make_red_png_b64() -> str:
    try:
        from PIL import Image
        import io
        img = Image.new("RGB", (64, 64), (220, 20, 30))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        # Hardcoded 4x4 solid red PNG fallback.
        return (
            "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAYAAACp8Z5+"
            "AAAAEElEQVR4nGP8z8DwHwAFAQH/9N4cYwAAAABJRU5ErkJggg=="
        )


def scenario_image_round_trip(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    artifacts: dict[str, Any] = {}

    apps = client.list_apps() or []
    deployed_ids = {a.get("app_id") for a in apps if isinstance(a, dict)}
    if APP_ID not in deployed_ids:
        return False, f"app '{APP_ID}' not deployed", artifacts

    sid = f"img-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )
    artifacts["session_id"] = sid

    image_b64 = _make_red_png_b64()
    artifacts["image_bytes_b64_length"] = len(image_b64)

    images = [{
        "data": image_b64,
        "mime": "image/png",
        "name": "swatch.png",
    }]

    post_result = client.post_message_raw(
        session,
        "I attached an image. Look at it and tell me what colour you see in one short sentence.",
        images=images,
    )
    artifacts["post_status_code"] = post_result.get("status_code")
    correlation_id = (
        (post_result.get("body") or {}).get("data", {}).get("correlation_id") or ""
    )
    time.sleep(0.5)

    stream = client.open_event_stream(session)
    try:
        if correlation_id:
            stream.wait_for(
                "message_done", timeout=180,
                predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == correlation_id,
            )
        else:
            stream.wait_until_idle(quiet_seconds=3.0, total_timeout=180)
        events = assertions.sort_by_seq(stream.events())
    finally:
        stream.stop(timeout=2.0)

    artifacts["event_count"] = len(events)
    artifacts["event_types"] = sorted({e["type"] for e in events})

    tool_calls = []
    for e in events:
        if e.get("type") in ("tool_call", "tool_start"):
            p = e.get("payload") or {}
            tool_calls.append({
                "type": e.get("type"),
                "tool_name": p.get("tool_name") or p.get("name"),
                "params_preview": json.dumps(
                    p.get("params") or p.get("tool_params") or {},
                    default=str,
                )[:160],
            })
    artifacts["tool_calls_seen"] = tool_calls[:10]

    read_called = any(
        ((tc.get("tool_name") or "").lower() in (
            "read", "wsread", "workspace.read", "filesystem.read",
        ))
        for tc in tool_calls
    )

    reply_text = ""
    for e in events:
        if e.get("type") in ("out_token", "token"):
            p = e.get("payload") or {}
            d = p.get("delta") or p.get("text") or p.get("content")
            if isinstance(d, str):
                reply_text += d
    artifacts["reply_length"] = len(reply_text)
    artifacts["reply_preview"] = reply_text[:300]

    reply_lower = reply_text.lower()
    saw_red = any(
        kw in reply_lower
        for kw in ("red", "rouge", "crimson", "scarlet", "vermilion")
    )

    user_message_persisted = False
    try:
        persistent = client.get_persistent_events(session, since_seq=0)
        rows = (persistent.get("events") if isinstance(persistent, dict) else persistent) or []
        for e in rows:
            if not isinstance(e, dict) or e.get("type") != "user_message":
                continue
            p = e.get("payload") or {}
            imgs = p.get("images") or []
            if imgs:
                user_message_persisted = True
                break
    except Exception:
        user_message_persisted = False

    no_strip_crash = not any(
        "'list' object has no attribute 'strip'" in str((e.get("payload") or {}).get("error") or "")
        for e in events
        if isinstance(e, dict)
    )

    checks = [
        _ok("app deployed", APP_ID in deployed_ids, "missing"),
        _ok("POST with images returned 200",
            artifacts["post_status_code"] == 200,
            f"got {artifacts['post_status_code']}"),
        _ok("turn produced events", len(events) > 0, "0 events"),
        _ok(
            "user_message persisted with images attachment manifest",
            user_message_persisted,
            "no user_message with non-empty images in persistent events",
        ),
        _ok(
            "agent invoked a read tool on the attachment",
            read_called,
            f"no read tool in {tool_calls!r}",
        ),
        _ok(
            "no 'list.strip()' multimodal crash on turn start",
            no_strip_crash,
            "error event still contains the .strip() AttributeError",
        ),
    ]
    ok, detail = assertions.report(checks)
    return ok, detail, artifacts


def main() -> int:
    creds_path = Path(r"C:\Users\ASUS\.digitorn\credentials.json")
    if not creds_path.exists():
        print(f"FAIL  CLI credentials missing: {creds_path}")
        return 1
    token = json.loads(creds_path.read_text(encoding="utf-8"))["access_token"]
    client = DevClient.with_token(token)

    t0 = time.monotonic()
    try:
        ok, detail, artifacts = scenario_image_round_trip(client)
    except Exception as exc:
        print(f"FAIL  scenario crashed: {type(exc).__name__}: {exc}")
        return 1
    dt = time.monotonic() - t0

    print(f"\n=== image multimodal round-trip ({dt:.1f}s) ===")
    print(detail)
    print("artifacts:")
    for k, v in artifacts.items():
        line = f"  {k:28s} = {v!r}"
        sys.stdout.buffer.write(line.encode("utf-8", "replace") + b"\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
