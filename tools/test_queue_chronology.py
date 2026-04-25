"""End-to-end chronology test with real LLM.

Scenario:
  - POST msg1 (long generation so turn 1 takes seconds)
  - Immediately POST msg2 (must be queued)
  - Wait for both turns to finish
  - Assert:
    * msg1 correlation_id starts with fp-
    * msg2 correlation_id is a UUID (queued)
    * history order: [user1, assistant1, user2, assistant2]
    * both assistants have non-empty content
"""
from __future__ import annotations

import time
import uuid

from digitorn.testing import DevClient
from digitorn.testing.models import SessionHandle


def main() -> None:
    client = DevClient(auto_approve=True)
    app_id = "digitorn-chat"

    sid = f"chron-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=app_id, daemon_url=client.daemon_url, workspace="",
    )
    print(f"session={sid}\n")

    msg1 = (
        "Énumère 15 capitales africaines, une par ligne, avec une phrase "
        "de contexte historique pour chacune. Sois détaillé."
    )
    msg2 = "Maintenant, juste dis 'MSG2_OK' sans rien d'autre."

    t0 = time.time()
    r1 = client._post(
        f"/api/apps/{app_id}/sessions/{sid}/messages",
        json={"message": msg1, "workspace": ""},
    )
    body1 = r1.json().get("data", {})
    cid1 = body1.get("correlation_id", "")
    print(f"[+{time.time()-t0:.2f}s] POST msg1 cid={cid1}")

    r2 = client._post(
        f"/api/apps/{app_id}/sessions/{sid}/messages",
        json={"message": msg2, "workspace": ""},
    )
    body2 = r2.json().get("data", {})
    cid2 = body2.get("correlation_id", "")
    print(f"[+{time.time()-t0:.2f}s] POST msg2 cid={cid2}")

    print(f"\nmsg1 fast-path? {cid1.startswith('fp-')}")
    print(f"msg2 queued?    {cid2 and not cid2.startswith('fp-')}")

    print("\nWaiting for both turns to finish...")
    last_turn_count = 0
    deadline = time.time() + 180
    while time.time() < deadline:
        summ = client._get(f"/api/apps/{app_id}/sessions/{sid}").json().get("data", {})
        tc = summ.get("turn_count", 0)
        is_active = summ.get("is_active")
        if tc != last_turn_count:
            print(f"  [+{time.time()-t0:.1f}s] turn_count={tc} is_active={is_active}")
            last_turn_count = tc
        if tc >= 2 and not is_active:
            break
        time.sleep(0.5)

    duration = time.time() - t0
    print(f"\nTotal duration: {duration:.1f}s")

    msgs = client.get_history(session, include_system=False)
    print(f"\nHistory ({len(msgs)} messages):")
    for i, m in enumerate(msgs):
        role = m.get("role", "")
        content = str(m.get("content", ""))
        print(f"  [{i}] {role}: {content[:120]}")

    roles = [m.get("role", "") for m in msgs]
    print(f"\nRole sequence: {roles}")
    expected = ["user", "assistant", "user", "assistant"]
    print(f"Expected:      {expected}")
    print(f"Chronological? {roles == expected}")

    assistants = [m for m in msgs if m.get("role") == "assistant"]
    assistant_texts = [str(m.get("content", "")) for m in assistants]
    has_content = [bool(t.strip()) for t in assistant_texts]
    print(f"Assistants with content: {has_content}")

    user_contents = [str(m.get("content", "")) for m in msgs if m.get("role") == "user"]
    msg1_preserved = user_contents and "capitales africaines" in user_contents[0]
    msg2_preserved = len(user_contents) >= 2 and "MSG2_OK" in user_contents[1]
    print(f"msg1 preserved at index 0: {msg1_preserved}")
    print(f"msg2 preserved at index 2: {msg2_preserved}")

    msg2_answer_ok = (
        len(assistant_texts) >= 2
        and "MSG2_OK" in assistant_texts[1]
    )
    print(f"msg2's response mentions MSG2_OK: {msg2_answer_ok}")

    r = client._get(f"/api/apps/{app_id}/sessions/{sid}/queue")
    entries = r.json().get("data", {}).get("entries", []) or []
    print(f"\nFinal active queue entries: {len(entries)}")

    all_ok = (
        cid1.startswith("fp-")
        and cid2 and not cid2.startswith("fp-")
        and roles == expected
        and all(has_content)
        and msg1_preserved
        and msg2_preserved
        and len(entries) == 0
    )
    print(f"\n{'PASS' if all_ok else 'FAIL'}: full chronological scenario")


if __name__ == "__main__":
    main()
