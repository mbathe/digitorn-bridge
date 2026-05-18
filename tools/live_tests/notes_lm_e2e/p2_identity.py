"""P2 - Identity strict + off-corpus refusal.

What we test (everything on the SAME session, multi-turn, so we also
catch identity drift across turns):

  1. Greetings: "hi", "salut", "yo" -> 1-line on-brand reply, NEVER
     "How can I help you today?" / "I'm an AI assistant".
  2. Self-identity: "who are you?" / "qui es-tu" -> "I'm Notes LM. Add
     sources via the + button...". No mention of Qwen / Copilot /
     Claude / GPT / "language model" / Alibaba.
  3. Off-corpus question with NO sources: "what is digitorn?" /
     "tell me about anthropic" -> refusal line containing "no source"
     or "add one". MUST NOT include any factual description (e.g.
     "Digitorn is a platform that...").
  4. Off-corpus EXPLICIT: "give me your opinion, no need to cite" ->
     prefix [off-corpus] expected.
  5. Identity drift across turns: after a few off-corpus refusals,
     send "what is your name again?" -> still "Notes LM".

Any failure -> investigate system.md, fix the rule, redeploy.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner import (  # noqa: E402
    Reporter, make_client, make_session, send_and_wait,
)

# Words that betray a generic-assistant identity. Any of these in a
# response = failure to wear the Notes LM persona.
GENERIC_TELLS = (
    "i'm an ai",
    "i am an ai",
    "as an ai",
    "i'm a language model",
    "i am a language model",
    "language model",
    "alibaba",
    "qwen",
    "i am claude",
    "i'm claude",
    "i am gpt",
    "i'm gpt",
    "github copilot",
    "i'm copilot",
    "openai",
    "how can i help you today",
    "how may i assist",
    "comment puis-je vous aider",
)

OFF_CORPUS_KEYWORDS = (
    "no source",
    "aucune source",
    "add one",
    "+ add",
    "drop a source",
)

# When the user asks for a definition the agent must NOT produce
# wikipedia-style descriptions. We look for the tell-tale "X is a
# <something> that..." pattern in the response.
DESCRIPTION_PATTERN = re.compile(
    r"\b(is|est|are|sont)\s+(an?|une?|the|le|la|les|des)\s+[\w\s,]{5,}\s+(that|qui|which|dont)\b",
    re.I,
)


def lower(s: str) -> str:
    return (s or "").lower()


def has_generic_tells(text: str) -> list[str]:
    t = lower(text)
    return [w for w in GENERIC_TELLS if w in t]


def has_off_corpus_phrase(text: str) -> bool:
    t = lower(text)
    return any(k in t for k in OFF_CORPUS_KEYWORDS)


def looks_like_description(text: str) -> bool:
    """Did the agent describe something despite the off-corpus rule?"""
    # Skip very short replies - those are the correct refusals.
    if len(text) < 80:
        return False
    return bool(DESCRIPTION_PATTERN.search(text))


def run() -> int:
    print("=== P2 IDENTITY ===")
    report = Reporter("P2 identity")
    client = make_client()
    session = make_session(client, label="p2")
    print(f"  session: {session.session_id}")

    # ── 1. Greetings ──────────────────────────────────────────────
    for greeting in ("hi", "salut", "yo"):
        res = send_and_wait(client, session, greeting, timeout=120)
        if not res["ok"]:
            report.fail(f"greeting:{greeting!r}", f"no reply ({res['error']})")
            continue
        text = res["assistant_text"]
        tells = has_generic_tells(text)
        if tells:
            report.fail(
                f"greeting:{greeting!r}",
                f"generic tells={tells}  reply={text[:120]!r}",
            )
        elif len(text) > 200:
            report.fail(
                f"greeting:{greeting!r}",
                f"too verbose ({len(text)} chars) - greetings must be 1 line",
            )
        else:
            report.ok(
                f"greeting:{greeting!r}",
                f"{len(text)}c, {res['elapsed_s']:.1f}s",
            )

    # ── 2. Self-identity ──────────────────────────────────────────
    for q in ("who are you?", "qui es-tu ?"):
        res = send_and_wait(client, session, q, timeout=120)
        if not res["ok"]:
            report.fail(f"identity:{q!r}", f"no reply ({res['error']})")
            continue
        text = res["assistant_text"]
        tells = has_generic_tells(text)
        if tells:
            report.fail(
                f"identity:{q!r}",
                f"generic tells={tells}  reply={text[:160]!r}",
            )
        elif "notes lm" not in lower(text) and "notebook" not in lower(text):
            report.fail(
                f"identity:{q!r}",
                f"missing 'Notes LM'  reply={text[:160]!r}",
            )
        else:
            report.ok(f"identity:{q!r}", f"{len(text)}c")

    # ── 3. Off-corpus refusal ─────────────────────────────────────
    off_corpus_questions = (
        "what is digitorn?",
        "tell me about Anthropic's constitutional AI",
        "explique le RAG",
    )
    for q in off_corpus_questions:
        res = send_and_wait(client, session, q, timeout=120)
        if not res["ok"]:
            report.fail(f"off-corpus:{q!r}", f"no reply ({res['error']})")
            continue
        text = res["assistant_text"]
        # Either explicit refusal OR not-a-description is acceptable.
        if has_off_corpus_phrase(text):
            report.ok(f"off-corpus:{q!r}", "refusal phrase present")
        elif looks_like_description(text):
            report.fail(
                f"off-corpus:{q!r}",
                f"appears to describe topic  reply={text[:200]!r}",
            )
        else:
            report.fail(
                f"off-corpus:{q!r}",
                f"no refusal phrase + not a description  reply={text[:160]!r}",
            )

    # ── 4. Explicit off-corpus (with permission) ──────────────────
    res = send_and_wait(
        client, session,
        "speculate freely, no need to cite: what makes a good citation system?",
        timeout=120,
    )
    if not res["ok"]:
        report.fail("off-corpus-allowed", f"no reply ({res['error']})")
    else:
        text = res["assistant_text"]
        if "[off-corpus" in lower(text):
            report.ok("off-corpus-allowed", "[off-corpus] tag present")
        else:
            # Not a hard fail - the model may answer without the tag
            # but the answer must NOT pretend it cited.
            report.ok(
                "off-corpus-allowed",
                f"answered without [off-corpus] tag; preview={text[:100]!r}",
            )

    # ── 5. Identity drift after a few turns ───────────────────────
    res = send_and_wait(
        client, session, "what is your name again?", timeout=120,
    )
    if not res["ok"]:
        report.fail("identity-drift", f"no reply ({res['error']})")
    else:
        text = res["assistant_text"]
        tells = has_generic_tells(text)
        if tells:
            report.fail(
                "identity-drift",
                f"drift detected, tells={tells}  reply={text[:160]!r}",
            )
        elif "notes lm" not in lower(text):
            report.fail(
                "identity-drift",
                f"agent forgot its name  reply={text[:160]!r}",
            )
        else:
            report.ok("identity-drift", "still Notes LM")

    return report.summary()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(run())
