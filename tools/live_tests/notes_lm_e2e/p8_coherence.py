"""P8 - Multi-turn coherence on a real workload.

We push the agent through 8 turns on the SAME session, with TWO
different sources covering different topics. The agent must:
  - keep its identity throughout
  - distinguish citations between the two sources
  - refer to a fact from turn 2 when asked about it in turn 6
  - never invent a fact not in either source

This is the most representative of "real production use".
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner import (  # noqa: E402
    APP_ID, Reporter, make_client, make_session, send_and_wait,
)


SRC_PHYSICS = "attachments/physics-notes.md"
PHYSICS = """\
# Physics Notes - Black Body Radiation

The Planck constant h = 6.626e-34 J·s. Stefan-Boltzmann constant
sigma = 5.670e-8 W·m^-2·K^-4. A perfect black body radiates total
power per unit area = sigma * T^4 (T in Kelvin).

Wien's displacement law: lambda_max * T = 2.898e-3 m·K.

Common pitfall: students confuse Wien's law with the Stefan-Boltzmann
law because both involve T^4 in different rearrangements.
"""

SRC_HISTORY = "attachments/history-notes.md"
HISTORY = """\
# History Notes - 19th Century Italy

The Italian unification (Risorgimento) climaxed in 1861 with the
proclamation of the Kingdom of Italy under Victor Emmanuel II.

Camillo di Cavour was the architect of the diplomatic strategy.
Giuseppe Garibaldi led the Expedition of the Thousand in 1860.

Rome was annexed in 1870 after the Franco-Prussian war withdrew
the French garrison. Italian capital moved from Florence to Rome
the same year.
"""

CITATION_RE = re.compile(
    r"(attachments/[\w\-./]+)\s*:\s*(L\d+-L\d+|p\.\d+)",
)


def _seed(client, session, path, content):
    return client._put(
        f"/api/apps/{APP_ID}/sessions/{session.session_id}"
        f"/workspace/files/{path}",
        json={"content": content, "auto_approve": True, "source": "user"},
    ).status_code == 200


def run() -> int:
    print("=== P8 COHERENCE ===")
    report = Reporter("P8 coherence")
    client = make_client()
    session = make_session(client, label="p8")
    print(f"  session: {session.session_id}")

    if not (_seed(client, session, SRC_PHYSICS, PHYSICS)
            and _seed(client, session, SRC_HISTORY, HISTORY)):
        report.fail("seed-sources", "could not write both sources")
        return report.summary()
    report.ok("seed-sources", "physics + history")

    # Turn 1: physics fact
    r = send_and_wait(
        client, session,
        "What does Stefan-Boltzmann say? Cite verbatim.",
        timeout=180,
    )
    if not r["ok"]:
        report.fail("t1:stefan-boltzmann", f"err={r['error']}")
        return report.summary()
    if "5.670e-8" in r["assistant_text"] or "sigma" in r["assistant_text"].lower():
        # Plus citation to physics-notes.md
        if "physics" in r["assistant_text"].lower():
            report.ok("t1:stefan-boltzmann", "cite physics-notes")
        else:
            report.fail(
                "t1:stefan-boltzmann",
                f"physics fact present but no physics-notes citation  "
                f"reply={r['assistant_text'][:200]!r}",
            )
    else:
        report.fail(
            "t1:stefan-boltzmann",
            f"missing fact 5.670e-8  reply={r['assistant_text'][:200]!r}",
        )

    # Turn 2: history fact
    r = send_and_wait(
        client, session,
        "Who led the Expedition of the Thousand? Cite the line.",
        timeout=180,
    )
    if not r["ok"]:
        report.fail("t2:garibaldi", f"err={r['error']}")
    elif "garibaldi" in r["assistant_text"].lower():
        report.ok("t2:garibaldi", "named Garibaldi")
    else:
        report.fail(
            "t2:garibaldi",
            f"reply doesn't name Garibaldi  reply={r['assistant_text'][:200]!r}",
        )

    # Turn 3: source distinction - ask which source covers what
    r = send_and_wait(
        client, session,
        "List my sources and what each covers in one line each.",
        timeout=180,
    )
    if not r["ok"]:
        report.fail("t3:source-list", f"err={r['error']}")
    else:
        txt = r["assistant_text"].lower()
        if "physics" in txt and "history" in txt:
            report.ok("t3:source-list", "both sources mentioned")
        else:
            report.fail(
                "t3:source-list",
                f"missing one source  reply={r['assistant_text'][:200]!r}",
            )

    # Turn 4: off-corpus question -> refusal
    r = send_and_wait(
        client, session,
        "What is the population of Tokyo in 2026?",
        timeout=120,
    )
    if not r["ok"]:
        report.fail("t4:off-corpus", f"err={r['error']}")
    else:
        txt = r["assistant_text"].lower()
        if "no source" in txt or "aucune source" in txt:
            report.ok("t4:off-corpus", "refused properly")
        else:
            report.fail(
                "t4:off-corpus",
                f"didn't refuse  reply={r['assistant_text'][:200]!r}",
            )

    # Turn 5: memory recall (was Garibaldi mentioned earlier?)
    r = send_and_wait(
        client, session,
        "Earlier you mentioned someone who led an expedition. Who was that, again?",
        timeout=120,
    )
    if not r["ok"]:
        report.fail("t5:recall", f"err={r['error']}")
    elif "garibaldi" in r["assistant_text"].lower():
        report.ok("t5:recall", "agent recalled Garibaldi from t2")
    else:
        report.fail(
            "t5:recall",
            f"reply didn't recall Garibaldi  reply={r['assistant_text'][:200]!r}",
        )

    # Turn 6: complex synthesis - both sources
    r = send_and_wait(
        client, session,
        "Both my sources mention a specific year. List both years with the source for each.",
        timeout=180,
    )
    if not r["ok"]:
        report.fail("t6:synthesis", f"err={r['error']}")
    else:
        txt = r["assistant_text"]
        # 1861 and 1870 are in history; physics has no year. But physics
        # mentions constants. The agent should either correctly say only
        # history has years, or correctly map the dates.
        years = re.findall(r"\b(186[0-9]|187[0-9])\b", txt)
        if years:
            report.ok(
                "t6:synthesis",
                f"found years={years}",
            )
        else:
            report.fail(
                "t6:synthesis",
                f"missed historic years  reply={txt[:200]!r}",
            )

    # Turn 7: invented question - does the agent fabricate or refuse?
    r = send_and_wait(
        client, session,
        "What does my physics source say about quantum entanglement? Cite.",
        timeout=120,
    )
    if not r["ok"]:
        report.fail("t7:no-fabricate", f"err={r['error']}")
    else:
        txt = r["assistant_text"].lower()
        # Source has NO mention of entanglement. Agent must NOT
        # invent a citation.
        if "entangle" in txt and "L" in r["assistant_text"]:
            # Suspicious - check if the citation actually points at a
            # line that contains "entangle" in source. Source DOESN'T.
            cites = CITATION_RE.findall(r["assistant_text"])
            if cites:
                report.fail(
                    "t7:no-fabricate",
                    f"agent fabricated citation about entanglement  "
                    f"cites={cites}",
                )
            else:
                report.ok(
                    "t7:no-fabricate",
                    "mentioned entanglement but no citation",
                )
        elif "no source" in txt or "ne couvre" in txt or "doesn't cover" in txt or "does not cover" in txt:
            report.ok("t7:no-fabricate", "refused properly")
        else:
            # Ambiguous - the agent may have soft-refused without our
            # canonical phrase. We'll accept absence of citation as
            # passing here.
            cites = CITATION_RE.findall(r["assistant_text"])
            if cites:
                report.fail(
                    "t7:no-fabricate",
                    f"unexpected citation {cites}  reply={r['assistant_text'][:200]!r}",
                )
            else:
                report.ok(
                    "t7:no-fabricate",
                    "no citation - acceptable soft refusal",
                )

    # Turn 8: identity drift after 7 turns
    r = send_and_wait(
        client, session,
        "what are you again?",
        timeout=120,
    )
    if not r["ok"]:
        report.fail("t8:identity", f"err={r['error']}")
    else:
        txt = r["assistant_text"].lower()
        tells = ("i'm an ai", "i am an ai", "language model", "qwen", "alibaba", "openai")
        if any(t in txt for t in tells):
            report.fail(
                "t8:identity",
                f"identity drift  reply={r['assistant_text'][:200]!r}",
            )
        elif "notes lm" in txt:
            report.ok("t8:identity", "still Notes LM after 8 turns")
        else:
            report.fail(
                "t8:identity",
                f"didn't say Notes LM  reply={r['assistant_text'][:200]!r}",
            )

    return report.summary()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(run())
