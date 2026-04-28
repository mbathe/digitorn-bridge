"""Live test scenarios for Chess Coach.

Run via:
    PYTHONIOENCODING=utf-8 py -3.12 tools/live_tests/chess_coach_scenarios.py
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

APP_DIR = ROOT / "apps" / "digitorn-official" / "chess-coach"
APP_ID = "chess-coach"
TEST_USERNAME = "DrNykterstein"  # Magnus Carlsen's known public Lichess account


def deploy(client: DevClient) -> None:
    yaml_path = APP_DIR / "app.yaml"
    print(f"-> deploy {APP_ID} from {yaml_path}")
    res = client.deploy(yaml_path=str(yaml_path), force=True)
    print(f"  deploy result: status={res.status} agents={res.agents} total_tools={res.total_tools}")


def collect_assistant_text(client: DevClient, session: SessionHandle) -> str:
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
                parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append(p.get("text", ""))
                    elif isinstance(p, str):
                        parts.append(p)
                return "".join(parts)
    return ""


def run_turn(client, session, label, message, timeout=240):
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
    print(f"  text length: {len(text)}")
    if text:
        print(f"  preview: {text[:300]!r}")
    return True, text


def scenario_multi_turn(client: DevClient) -> tuple[bool, str, dict]:
    sid = f"chess-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )
    print(f"\n=== session {sid} ===")
    art: dict = {"session": sid, "turns": []}

    # Turn 1 - give username + ask analysis (just 1 game to keep response small)
    ok, text = run_turn(
        client, session, "turn 1",
        f"Analyse my last 1 Lichess game. Username: {TEST_USERNAME}. "
        "Keep it short, English. Use a SINGLE http.get with ?max=1.",
        timeout=240,
    )
    art["turns"].append({"n": 1, "len": len(text), "preview": text[:200]})
    if not ok or len(text) < 200:
        return False, "turn 1: no meaningful analysis", art
    if "lichess" not in text.lower() and "game" not in text.lower():
        return False, f"turn 1: response doesn't look like a chess analysis:\n{text[:500]}", art

    time.sleep(1)

    # Turn 2 - recall username from memory
    ok, text = run_turn(
        client, session, "turn 2",
        "Quels sont mes points faibles récurrents ? Réponds en français.",
        timeout=180,
    )
    art["turns"].append({"n": 2, "len": len(text), "preview": text[:200]})
    if not ok or len(text) < 100:
        return False, "turn 2: no meaningful weaknesses response", art
    if not any(m in text.lower() for m in ("ouverture", "blunder", "tactique", "endgame", "milieu", "pattern", "centipion", "centipawn")):
        return False, f"turn 2: doesn't discuss weaknesses:\n{text[:500]}", art

    return True, "chess-coach: 2 turns OK (Lichess fetch + multilingual coaching + memory)", art


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
