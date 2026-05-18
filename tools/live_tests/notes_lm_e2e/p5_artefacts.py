"""P5 - Artefact generation (briefing, mindmap, timeline, study_guide).

Setup: 2 distinct sources covering complementary topics. Then ask
the agent to generate each artefact, and assert:
  - the target file lands at the right path (briefing.md, mindmap.md,
    timeline.md, study_guide.md - all at workspace root, NOT under
    attachments/)
  - the file is non-empty and parseable for its kind
  - it contains a citation back to at least one source (proves the
    artefact is grounded, not fabricated)
"""
from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner import (  # noqa: E402
    APP_ID, Reporter, fetch_workspace_file, list_workspace_files,
    make_client, make_session, send_and_wait,
)


SOURCE_A = "attachments/anthropic-policy.md"
CONTENT_A = """\
# Anthropic Policy Brief - 2026-03

Constitutional AI was published in late 2022. The technique uses a
small set of principles, called a constitution, to guide RLHF
training. Claude-3 was the first model trained with a v2 constitution.

In March 2026 Anthropic adopted a stricter refusal taxonomy:
  - off-policy: model declines
  - harmful: model declines AND logs to a safety log
  - ambiguous: model asks clarification

The taxonomy is enforced by a thin post-hoc classifier, not the
base model. Latency overhead: ~80ms per turn.
"""

SOURCE_B = "attachments/openai-evals.md"
CONTENT_B = """\
# OpenAI evals notes - 2026-Q1

GPT-5 family evaluation: MMLU 91.2, HumanEval 89.5, GSM8K 95.1.

In April 2026 a new eval bench called "Hard-Refuse" was introduced
to score how models handle adversarial framing of off-policy prompts.

Top scores on Hard-Refuse:
  - claude-3.7-sonnet: 88
  - gpt-5-mini: 84
  - llama-4-70b: 79

The bench is open source on github.com/openai-evals/hard-refuse.
"""

CITATION_RE = re.compile(
    r"(attachments/[\w\-./]+)\s*:\s*(L\d+-L\d+|p\.\d+)",
)


def _seed(client, session, path: str, content: str) -> bool:
    try:
        r = client._put(
            f"/api/apps/{APP_ID}/sessions/{session.session_id}"
            f"/workspace/files/{path}",
            json={"content": content, "auto_approve": True, "source": "user"},
        )
        return r.status_code == 200
    except Exception:
        return False


def _wait_for_file(client, session, path: str, timeout: float = 30) -> bool:
    """Poll workspace listing until path appears, or timeout."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        files = list_workspace_files(client, session)
        if path in files:
            return True
        time.sleep(1.0)
    return False


def run() -> int:
    print("=== P5 ARTEFACTS ===")
    report = Reporter("P5 artefacts")
    client = make_client()
    session = make_session(client, label="p5")
    print(f"  session: {session.session_id}")

    # ── Seed 2 sources ────────────────────────────────────────────
    if not _seed(client, session, SOURCE_A, CONTENT_A):
        report.fail("seed-A", SOURCE_A)
        return report.summary()
    if not _seed(client, session, SOURCE_B, CONTENT_B):
        report.fail("seed-B", SOURCE_B)
        return report.summary()
    report.ok("seed-sources", f"{SOURCE_A} + {SOURCE_B}")

    artefacts = [
        ("briefing", "briefing.md", "Write a briefing document covering all my sources."),
        ("mindmap", "mindmap.md", "Build a mind map of my sources."),
        ("timeline", "timeline.md", "Extract a chronological timeline from my sources."),
        ("study_guide", "study_guide.md", "Turn my sources into a study guide."),
    ]

    for kind, path, prompt in artefacts:
        res = send_and_wait(
            client, session, message=prompt, timeout=240,
        )
        if not res["ok"]:
            report.fail(f"{kind}:reply", f"err={res['error']}")
            continue

        # Wait briefly for file event to settle
        if not _wait_for_file(client, session, path, timeout=20):
            files = list_workspace_files(client, session)
            report.fail(
                f"{kind}:file-created",
                f"{path} missing  workspace listing: {files}",
            )
            continue
        content = fetch_workspace_file(client, session, path) or ""
        if not content.strip():
            report.fail(f"{kind}:file-not-empty", f"{path} is empty")
            continue
        report.ok(
            f"{kind}:file-created",
            f"{path} ({len(content)} chars)",
        )

        # Citation present?
        cites = CITATION_RE.findall(content)
        if cites:
            report.ok(
                f"{kind}:cites-source",
                f"{len(cites)} citation(s)",
            )
        else:
            report.fail(
                f"{kind}:cites-source",
                f"no path:Lstart-Lend token in {path}",
            )

        # Kind-specific shape check
        if kind == "timeline":
            # Should contain dates (2022/2026/Q-style)
            has_date = re.search(r"\b(202[0-9]|Q[1-4])\b", content) is not None
            if has_date:
                report.ok(f"{kind}:has-dates", "year/quarter token found")
            else:
                report.fail(f"{kind}:has-dates", "no date-like token")

        if kind == "mindmap":
            # Should have list/tree structure (- or *) or markdown bullets
            has_list = re.search(r"^\s*[-*]\s", content, re.M) is not None
            if has_list:
                report.ok(f"{kind}:has-structure", "list markers found")
            else:
                report.fail(f"{kind}:has-structure", "no list/tree structure")

    return report.summary()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(run())
