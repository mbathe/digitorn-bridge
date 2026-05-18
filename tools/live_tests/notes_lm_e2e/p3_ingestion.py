"""P3 - Source ingestion.

Notes LM accepts sources via THREE paths that must all converge in
``attachments/<name>.md`` (or .pdf etc.):

  1. Agent-side URL fetch: user types "ingest <url> save this" in chat
     -> agent uses ``web.fetch`` + ``WsWrite("attachments/<slug>.md")``
  2. SDK ``ingestFile()`` from the iframe ``+ Add`` button: posts to
     ``/workspace/ingest-source``, daemon extracts text, lands at
     ``attachments/<name>.md``.
  3. SDK ``writeFile()`` from the iframe paste-text mode: direct PUT
     to ``/workspace/files/attachments/<name>.md``.

We test (1) and (3) via REST. (2) needs a binary file which we can
simulate with a small text upload via the same endpoint.

For each path:
  - assert the file lands under ``attachments/``
  - assert the agent ACKNOWLEDGES the new source on next turn (i.e.
    the ``addHint`` mechanism fed the agent context properly)
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner import (  # noqa: E402
    APP_ID, Reporter, fetch_workspace_file, list_workspace_files,
    make_client, make_session, send_and_wait,
)


SAMPLE_TEXT = """\
# Acme Internal Memo - Q3 2026

The Acme Robotics core finding from Q3 was that gripper failure rate
dropped 41% after introducing the Mark-IV servo. We attribute this to
the new closed-loop calibration that runs every 800 cycles.

Operating window: 18-28C ambient, 35-65% humidity. Outside that band
the polymer hardens and grip force drifts by up to 12%.

NEXT STEPS:
- Roll Mark-IV to fleet by 2026-Q4
- Recall serials AC-2024-* for retro-fit
- Patch firmware 4.3.1 to enforce the 800-cycle calibration cadence
"""

UNIQUE_TOKEN = "Mark-IV servo"  # phrase only in the source above


def run() -> int:
    print("=== P3 INGESTION ===")
    report = Reporter("P3 ingestion")
    client = make_client()
    session = make_session(client, label="p3")
    print(f"  session: {session.session_id}")

    # ── Path 1: paste-text via direct PUT (SDK writeFile mode) ────
    path = "attachments/acme-memo.md"
    try:
        r = client._put(
            f"/api/apps/{APP_ID}/sessions/{session.session_id}"
            f"/workspace/files/{path}",
            json={"content": SAMPLE_TEXT, "auto_approve": True, "source": "user"},
        )
        if r.status_code != 200:
            report.fail(
                "write-source-via-PUT",
                f"status={r.status_code} body={r.text[:200]}",
            )
        else:
            report.ok("write-source-via-PUT", f"{path}")
    except Exception as exc:
        report.fail("write-source-via-PUT", f"{exc!r}")

    # File present in workspace listing?
    files = list_workspace_files(client, session)
    if path in files:
        report.ok("file-listed", f"{path} in workspace listing")
    else:
        report.fail("file-listed", f"workspace listing={files}")

    # Content retrievable verbatim?
    content = fetch_workspace_file(client, session, path)
    if content and UNIQUE_TOKEN in content:
        report.ok("content-retrievable", f"{len(content)} chars, token present")
    else:
        report.fail("content-retrievable", f"content={content!r}")

    # ── Path 2: agent-driven URL ingest ───────────────────────────
    # We use a stable, small URL. To avoid flakes we use a localhost
    # endpoint that the daemon's web.fetch can reach. If that's not
    # available we skip this path.
    # For now we test with a real URL and a longer timeout. If fetch
    # is sandboxed-blocked the test will tell us.
    res = send_and_wait(
        client, session,
        message=(
            "Save this source: https://en.wikipedia.org/wiki/Retrieval-augmented_generation "
            "ingest it and tell me when done."
        ),
        timeout=180,
    )
    if not res["ok"]:
        report.fail("agent-url-ingest", f"err={res['error']}")
    else:
        # Did a NEW attachment land?
        time.sleep(1.5)  # let the workspace event settle
        files = list_workspace_files(client, session)
        new_files = [
            f for f in files
            if f.startswith("attachments/") and f != path
        ]
        if new_files:
            report.ok(
                "agent-url-ingest",
                f"agent created {new_files}",
            )
        else:
            report.fail(
                "agent-url-ingest",
                f"no new attachment after agent run. files={files}  "
                f"reply={res['assistant_text'][:200]!r}",
            )

    # ── Path 3: agent must NOTICE the new source ──────────────────
    # We added acme-memo.md silently via PUT; the iframe's
    # addHint hook normally injects a system_addendum on next turn.
    # Direct PUT bypasses that hint mechanism in this harness, so
    # the test is whether the agent can find the file when ASKED.
    res = send_and_wait(
        client, session,
        message="What sources have I added so far? List them.",
        timeout=120,
    )
    if not res["ok"]:
        report.fail("agent-knows-sources", f"err={res['error']}")
    else:
        text = res["assistant_text"]
        # Acceptable: mentions either the file path or its content
        # (the agent did a WsGlob and saw it).
        if "acme" in text.lower() or "memo" in text.lower():
            report.ok("agent-knows-sources", "agent listed acme-memo")
        else:
            report.fail(
                "agent-knows-sources",
                f"agent didn't mention acme  reply={text[:200]!r}",
            )

    return report.summary()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(run())
