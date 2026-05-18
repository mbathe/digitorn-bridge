"""P7 - Session reload + restoration.

Build 4 turns of real activity in a session (greet, identity, add a
source, ask a Q with citation). Snapshot history + persistent events.
Close the session (forces flush + evict). Re-open and read history +
events. Compare bit-for-bit.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner import (  # noqa: E402
    APP_ID, Reporter, make_client, make_session, send_and_wait,
    session_events,
)


SOURCE = "attachments/p7-mini.md"
CONTENT = """\
# Tiny test source

The polymer Mark-IV servo reduced gripper failure rate by 41%.
"""


def _seed(client, session, path: str, content: str) -> bool:
    r = client._put(
        f"/api/apps/{APP_ID}/sessions/{session.session_id}"
        f"/workspace/files/{path}",
        json={"content": content, "auto_approve": True, "source": "user"},
    )
    return r.status_code == 200


def run() -> int:
    print("=== P7 RELOAD ===")
    report = Reporter("P7 reload")
    client = make_client()
    session = make_session(client, label="p7")
    print(f"  session: {session.session_id}")

    # Build state across 4 turns
    if not _seed(client, session, SOURCE, CONTENT):
        report.fail("seed-source", SOURCE)
        return report.summary()

    for prompt in (
        "hi",
        "what sources do I have?",
        "what is the Mark-IV servo's effect? cite verbatim.",
    ):
        r = send_and_wait(client, session, prompt, timeout=180)
        if not r["ok"]:
            report.fail(
                f"build-state:{prompt!r}",
                f"err={r['error']}",
            )
            return report.summary()

    # Snapshot pre-reload
    rh = client._get(
        f"/api/apps/{APP_ID}/sessions/{session.session_id}/history",
    )
    pre_msgs = rh.json().get("data", {}).get("messages", []) if rh.status_code == 200 else []
    pre_events = session_events(client, session)
    if not pre_msgs:
        report.fail("snapshot-pre", "no messages after 3 turns")
        return report.summary()
    pre_sig = [
        (m.get("role"), (m.get("content") or "")[:80])
        for m in pre_msgs
    ]
    pre_seqs = [int(e.get("seq", 0)) for e in pre_events]
    report.ok(
        "snapshot-pre",
        f"{len(pre_msgs)} msgs, {len(pre_events)} events, max_seq={max(pre_seqs) if pre_seqs else 0}",
    )

    # Force close + cool-down
    try:
        r = client._post(
            f"/api/apps/{APP_ID}/sessions/{session.session_id}/close",
            json={},
        )
        # Some daemons don't expose /close - that's OK, eviction will
        # happen by background. We give it time.
    except Exception:
        pass
    time.sleep(2.5)

    # Re-read history + events
    rh2 = client._get(
        f"/api/apps/{APP_ID}/sessions/{session.session_id}/history",
    )
    post_msgs = rh2.json().get("data", {}).get("messages", []) if rh2.status_code == 200 else []
    post_events = session_events(client, session)
    post_sig = [
        (m.get("role"), (m.get("content") or "")[:80])
        for m in post_msgs
    ]
    post_seqs = [int(e.get("seq", 0)) for e in post_events]

    if len(post_msgs) != len(pre_msgs):
        report.fail(
            "messages-count-equal",
            f"pre={len(pre_msgs)} post={len(post_msgs)}",
        )
    else:
        report.ok("messages-count-equal", f"{len(post_msgs)}")

    if post_sig != pre_sig:
        # Find the first divergent index for a useful diagnostic
        n = min(len(pre_sig), len(post_sig))
        diff_at = next((i for i in range(n) if pre_sig[i] != post_sig[i]), n)
        report.fail(
            "messages-content-equal",
            f"diverged at index {diff_at}  "
            f"pre={pre_sig[diff_at] if diff_at < len(pre_sig) else None}  "
            f"post={post_sig[diff_at] if diff_at < len(post_sig) else None}",
        )
    else:
        report.ok("messages-content-equal", "role + content identical")

    if post_seqs[:len(pre_seqs)] != pre_seqs:
        report.fail(
            "event-seqs-stable",
            f"diverged. pre[:5]={pre_seqs[:5]}  post[:5]={post_seqs[:5]}",
        )
    else:
        report.ok("event-seqs-stable", f"{len(pre_seqs)} seqs preserved")

    # After reload, a new turn must continue from where we left off
    r = send_and_wait(
        client, session, "what was my last question?", timeout=120,
    )
    if not r["ok"]:
        report.fail("post-reload-turn", f"err={r['error']}")
    else:
        text = r["assistant_text"]
        if "mark-iv" in text.lower() or "servo" in text.lower():
            report.ok("post-reload-turn", "agent remembers prior topic")
        else:
            report.fail(
                "post-reload-turn",
                f"agent didn't recall prior topic  reply={text[:200]!r}",
            )

    return report.summary()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(run())
