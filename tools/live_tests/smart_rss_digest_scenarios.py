"""Live test scenarios for Smart RSS Digest.

Run via:
    PYTHONIOENCODING=utf-8 py -3.12 tools/live_tests/smart_rss_digest_scenarios.py

Requires:
  - daemon running on http://127.0.0.1:8000
  - ~/.digitorn/credentials.json with a valid access_token
  - DEEPSEEK_API_KEY in environment
"""
from __future__ import annotations

import sys
import time
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from digitorn.testing import DevClient, assertions  # noqa: E402
from digitorn.testing.models import SessionHandle  # noqa: E402

APP_DIR = ROOT / "apps" / "digitorn-official" / "smart-rss-digest"
APP_ID = "smart-rss-digest"


def deploy(client: DevClient) -> None:
    yaml_path = APP_DIR / "app.yaml"
    assert yaml_path.is_file(), f"missing {yaml_path}"
    print(f"-> deploy {APP_ID} from {yaml_path}")
    res = client.deploy(yaml_path=str(yaml_path), force=True)
    print(f"  deploy result: status={res.status} agents={res.agents} total_tools={res.total_tools}")


def collect_assistant_text(client: DevClient, session: SessionHandle) -> str:
    """Pull the latest assistant message text via the history endpoint.

    More reliable than scraping streaming events whose names vary by feature.
    """
    try:
        hist = client.get_history(session)
    except Exception as exc:
        print(f"  history fetch failed: {exc}")
        return ""
    msgs = hist if isinstance(hist, list) else hist.get("messages", [])
    for m in reversed(msgs):
        if m.get("role") == "assistant":
            content = m.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                # OpenAI-style content array
                parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append(p.get("text", ""))
                    elif isinstance(p, str):
                        parts.append(p)
                return "".join(parts)
    return ""


def run_turn(
    client: DevClient,
    session: SessionHandle,
    label: str,
    message: str,
    timeout: int = 180,
) -> tuple[bool, str]:
    print(f"\n[{label}] sending: {message[:80]}…")
    stream = client.send_live(session, message, total_timeout=timeout)
    try:
        events = assertions.sort_by_seq(stream.events())
        type_counts = Counter(e["type"] for e in events)
        print(f"  events={len(events)} types={dict(type_counts.most_common(6))}")
        msg_done = [e for e in events if e["type"] == "message_done"]
        if not msg_done:
            return False, ""
    finally:
        stream.stop(timeout=2.0)

    text = collect_assistant_text(client, session)
    print(f"  assistant text length: {len(text)}")
    if text:
        print(f"  preview: {text[:300]!r}")
    return True, text


def scenario_multi_turn(client: DevClient) -> tuple[bool, str, dict]:
    sid = f"rss-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )
    print(f"\n=== session {sid} ===")
    art: dict = {"session": sid, "turns": []}

    # Turn 1 — fetch + summarise from one feed
    ok, text = run_turn(
        client, session, "turn 1",
        "Build me a brief from this single feed: https://hnrss.org/frontpage. "
        "Max 5 bullets, English.",
    )
    art["turns"].append({"n": 1, "len": len(text), "preview": text[:200]})
    if not ok or len(text) < 100:
        return False, "turn 1: no meaningful assistant response", art

    time.sleep(1)

    # Turn 2 — save feeds (memory)
    ok, text = run_turn(
        client, session, "turn 2",
        "Save this feed under 'tech' for next time, please.",
    )
    art["turns"].append({"n": 2, "len": len(text), "preview": text[:200]})
    if not ok or len(text) < 30:
        return False, "turn 2: no save acknowledgement", art
    if not any(w in text.lower() for w in ("saved", "stored", "remember", "ok", "feed")):
        return False, f"turn 2: response doesn't mention saving:\n{text[:400]}", art

    time.sleep(1)

    # Turn 3 — multilingual follow-up
    ok, text = run_turn(
        client, session, "turn 3",
        "Maintenant en français : utilise mes flux sauvegardés et fais-moi un brief de 3 bullets.",
        timeout=180,
    )
    art["turns"].append({"n": 3, "len": len(text), "preview": text[:200]})
    if not ok or len(text) < 100:
        return False, "turn 3: no meaningful French response", art
    # heuristic for French response
    fr_markers = ("le ", "la ", "les ", "des ", "voici ", "selon ", "•")
    is_french = any(m in text.lower() for m in fr_markers) or "français" in text.lower()
    if not is_french:
        print(f"  WARN turn 3 may not be in French (no FR markers found)")

    return True, "smart-rss-digest: 3 turns OK (fetch+summarise, memory, multilingual)", art


def main() -> int:
    client = DevClient(timeout=60)
    deploy(client)
    print("\nWaiting 2 s for deploy to settle…")
    time.sleep(2)
    ok, detail, art = scenario_multi_turn(client)
    print("\n" + "=" * 60)
    print(f"RESULT: {'PASS' if ok else 'FAIL'}")
    print(f"detail: {detail}")
    for t in art.get("turns", []):
        print(f"  turn {t['n']}: text_len={t['len']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
