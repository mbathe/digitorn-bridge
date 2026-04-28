"""Test image upload + vision prompt on digitorn-chat.

Generates a tiny 2x2 PNG (red/blue) and asks the agent what it sees.
digitorn-chat uses DeepSeek-chat (no vision) by default → expect graceful
degradation (image converted to text description, agent can't actually see).
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
import uuid

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


def _tiny_png() -> bytes:
    # 1x1 red PNG (smallest possible valid PNG)
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )


def run() -> tuple[bool, list[str], dict]:
    token = os.environ.get("DIGITORN_TEST_TOKEN", "")
    client = DevClient.with_token(token)
    app_id = "digitorn-chat"
    sid = f"img-{uuid.uuid4().hex[:6]}"
    session = SessionHandle(session_id=sid, app_id=app_id,
                            daemon_url=client.daemon_url, workspace="")
    bugs: list[str] = []
    art: dict = {"session_id": sid}
    stream = None

    try:
        png_bytes = _tiny_png()
        img = {
            "data": base64.b64encode(png_bytes).decode("ascii"),
            "mime": "image/png",
            "name": "red.png",
        }

        post = client.post_message_raw(session, "What do you see in this image? Be brief.",
                                        images=[img])
        art["post_result"] = post
        cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
        if not cid:
            bugs.append(f"No correlation_id returned from POST. Response: {post}")
            return False, bugs, art

        stream = client.open_event_stream(session)
        done = stream.wait_for("message_done", timeout=120,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid)
        art["done_received"] = done is not None
        if done is None:
            bugs.append("message_done never received (120s)")

        time.sleep(0.5)
        hist = client.get_history(session)
        last_a = next((m.get("content","") for m in reversed(hist) if m.get("role")=="assistant" and m.get("content")), "")
        art["last_assistant"] = last_a[:300]

        # Check that the user message has the image attached (in history)
        user_msgs = [m for m in hist if m.get("role") == "user"]
        art["user_message_keys"] = list(user_msgs[-1].keys()) if user_msgs else []
        has_images_ref = any("image" in str(k).lower() for k in (user_msgs[-1].keys() if user_msgs else []))
        art["user_has_images_ref"] = has_images_ref

        # Try to list images for this session
        r = client._get(f"/api/apps/{app_id}/sessions/{sid}/images")
        art["list_images_status"] = r.status_code
        if r.status_code == 200:
            art["images_list"] = r.json().get("data", {})
        elif r.status_code == 404:
            # maybe route doesn't exist, try another path
            pass

        if not last_a:
            bugs.append("No assistant text in reply")
        # Don't assert anything specific about content - provider may not have vision

    except Exception as e:
        bugs.append(f"EXCEPTION: {type(e).__name__}: {e}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    return (len(bugs) == 0), bugs, art


if __name__ == "__main__":
    ok, bugs, art = run()
    print(f"\n{'=' * 60}\nMULTIMODAL: {'PASS' if ok else 'FAIL'}\n{'=' * 60}")
    for i, b in enumerate(bugs, 1):
        print(f"  {i}. {b}")
    print("\nARTIFACTS:", json.dumps(art, indent=2, default=str)[:3500])
    sys.exit(0 if ok else 1)
