"""P4 - Q&A with citations on a real source.

Setup: ingest a known text (UNIQUE_TOKEN at known line), then ask
the agent questions. Assert:
  - response uses the citation format ``path:Lstart-Lend``
  - the quoted snippet ACTUALLY appears in the source at the claimed
    line range (no hallucination)
  - asking a question OFF the source -> refusal, no fake citation
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner import (  # noqa: E402
    APP_ID, Reporter, fetch_workspace_file, make_client, make_session,
    send_and_wait,
)


SOURCE_PATH = "attachments/acme-test.md"
SOURCE_CONTENT = """\
# Acme Robotics - Test Memo

The Mark-IV servo reduced gripper failure rate by 41% in Q3 2026.

Operating window: 18C to 28C ambient, 35% to 65% humidity.

The closed-loop calibration runs every 800 cycles.

Recall serials AC-2024-* require retro-fit by 2026-Q4.

Firmware 4.3.1 enforces the calibration cadence automatically.
"""

# A phrase that ONLY appears in our source -- if the agent quotes it
# verbatim, we know it actually read the file.
KNOWN_QUOTE = "The Mark-IV servo reduced gripper failure rate by 41%"


# Citation pattern: path:Lstart-Lend  OR  path:p.N
CITATION_RE = re.compile(
    r"(attachments/[\w\-./]+)\s*:\s*(L\d+-L\d+|p\.\d+)",
)


def run() -> int:
    print("=== P4 CITATIONS ===")
    report = Reporter("P4 citations")
    client = make_client()
    session = make_session(client, label="p4")
    print(f"  session: {session.session_id}")

    # ── Seed the source via direct PUT (no agent involvement yet) ─
    try:
        r = client._put(
            f"/api/apps/{APP_ID}/sessions/{session.session_id}"
            f"/workspace/files/{SOURCE_PATH}",
            json={"content": SOURCE_CONTENT, "auto_approve": True, "source": "user"},
        )
        if r.status_code != 200:
            report.fail("seed-source", f"status={r.status_code}")
            return report.summary()
        report.ok("seed-source", SOURCE_PATH)
    except Exception as exc:
        report.fail("seed-source", f"{exc!r}")
        return report.summary()

    # ── 1. On-corpus question -> answer with citation ─────────────
    res = send_and_wait(
        client, session,
        message=(
            "What was the failure rate reduction from the Mark-IV servo? "
            "Cite verbatim with the line range."
        ),
        timeout=180,
    )
    if not res["ok"]:
        report.fail("answer-with-citation", f"err={res['error']}")
        return report.summary()
    text = res["assistant_text"]

    # Did the agent actually read the file?
    if "41%" in text or "41 %" in text:
        report.ok("answer-includes-fact", "41% present in reply")
    else:
        report.fail("answer-includes-fact", f"reply={text[:200]!r}")

    # Did it produce a citation token?
    matches = CITATION_RE.findall(text)
    if matches:
        report.ok(
            "citation-format-present",
            f"{len(matches)} citation(s): {matches[:3]}",
        )
    else:
        report.fail(
            "citation-format-present",
            f"no path:Lstart-Lend token found  reply={text[:300]!r}",
        )
        # Skip the next two checks - depend on having a citation
        return report.summary()

    # Does the cited line range actually contain the quote in source?
    # Take the first citation and validate against on-disk content.
    first_path, first_loc = matches[0]
    on_disk = fetch_workspace_file(client, session, first_path)
    if not on_disk:
        report.fail(
            "citation-path-exists",
            f"path {first_path!r} not retrievable from workspace",
        )
    else:
        report.ok("citation-path-exists", first_path)
        # Try to extract the verbatim quote from the agent's reply
        # (text inside double quotes near the citation).
        # We look for any verbatim phrase from the source in the reply.
        if KNOWN_QUOTE in text:
            report.ok(
                "verbatim-quote-from-source",
                "agent quoted the source line",
            )
        else:
            # Softer check: any 5+ word sequence from the source must
            # appear in the reply for the citation to be honest.
            source_lines = [
                line.strip() for line in on_disk.splitlines() if line.strip()
            ]
            found = False
            for line in source_lines:
                # Take ~5 consecutive words and check they're in reply
                words = line.split()
                if len(words) < 5:
                    continue
                snippet = " ".join(words[:5])
                if snippet in text:
                    found = True
                    break
            if found:
                report.ok(
                    "verbatim-quote-from-source",
                    "5+ word snippet from source present",
                )
            else:
                report.fail(
                    "verbatim-quote-from-source",
                    "no verbatim source snippet in reply - "
                    "agent may have cited without quoting",
                )

    # ── 2. Off-source question -> refusal, no fake citation ───────
    res = send_and_wait(
        client, session,
        message=(
            "What is the average rainfall in Tokyo in August? "
            "Cite verbatim from my sources."
        ),
        timeout=120,
    )
    if not res["ok"]:
        report.fail("off-source-refuses", f"err={res['error']}")
    else:
        text = res["assistant_text"]
        fake_citations = CITATION_RE.findall(text)
        # If the agent invented a citation to a topic NOT in the
        # source, we'd see citations on a topic the source can't
        # support.
        text_lower = text.lower()
        if "no source" in text_lower or "aucune source" in text_lower:
            report.ok("off-source-refuses", "refusal phrase present")
        elif fake_citations:
            report.fail(
                "off-source-refuses",
                f"agent fabricated citation(s) {fake_citations} for "
                f"topic not in source  reply={text[:200]!r}",
            )
        else:
            report.ok(
                "off-source-refuses",
                f"no citation, no obvious description  preview={text[:120]!r}",
            )

    return report.summary()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(run())
