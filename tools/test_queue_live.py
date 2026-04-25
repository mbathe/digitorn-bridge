"""Live test: send a message while a turn is running.

Reproduces the user-reported bug:
- Turn 1 long running (ask LLM for big response)
- Send turn 2 while turn 1 runs
- Verify turn 2 is QUEUED, not dispatched concurrently
- Verify chronology: user1, assistant1, user2, assistant2
- Verify queue chip is cleared after turn 2 done
"""
from __future__ import annotations

import threading
import time
import uuid

from digitorn.testing import DevClient
from digitorn.testing.models import SessionHandle


def _post_message_raw(client: DevClient, session: SessionHandle, message: str) -> dict:
    r = client._post(
        f"/api/apps/{session.app_id}/sessions/{session.session_id}/messages",
        json={"message": message, "workspace": session.workspace or ""},
    )
    return {"status_code": r.status_code, "body": r.json()}


def _queue_entries(client: DevClient, session: SessionHandle) -> list[dict]:
    r = client._get(
        f"/api/apps/{session.app_id}/sessions/{session.session_id}/queue"
    )
    if r.status_code != 200:
        return []
    return r.json().get("data", {}).get("entries", []) or []


def _session_summary(client: DevClient, session: SessionHandle) -> dict:
    r = client._get(f"/api/apps/{session.app_id}/sessions/{session.session_id}")
    return r.json().get("data", {}) if r.status_code == 200 else {}


def _history_roles(client: DevClient, session: SessionHandle) -> list[tuple[str, str]]:
    msgs = client.get_history(session, include_system=False)
    return [(m.get("role", ""), str(m.get("content", ""))[:60]) for m in msgs]


def main() -> None:
    client = DevClient(auto_approve=True)
    app_id = "digitorn-chat"

    try:
        client.get_app(app_id)
    except Exception as e:
        print(f"digitorn-chat not deployed: {e}")
        return

    sid = f"qtest-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=app_id, daemon_url=client.daemon_url, workspace="",
    )
    print(f"session={sid}")

    msg1 = (
        "Liste 30 capitales de pays africains une par ligne avec une courte "
        "phrase descriptive pour chacune. Sois verbeux."
    )
    msg2 = "Maintenant ajoute juste 'STOP' à la fin."

    print("\n[T+0s] POST message 1 (long)")
    r1 = _post_message_raw(client, session, msg1)
    print(f"  status={r1['status_code']} body={r1['body']}")

    time.sleep(0.2)
    summ = _session_summary(client, session)
    print(f"  is_active right after POST 1: {summ.get('is_active')}")

    print("\n[T+0.2s] POST message 2 immediately (turn 1 should still be running)")
    t_before = time.time()
    r2 = _post_message_raw(client, session, msg2)
    dt = time.time() - t_before
    print(f"  status={r2['status_code']} dt={dt:.2f}s body={r2['body']}")

    print("\n[T+?] Queue state right after POST 2:")
    q = _queue_entries(client, session)
    print(f"  entries={len(q)} statuses={[e.get('status') for e in q]}")
    print(f"  correlation_ids={[e.get('correlation_id') for e in q]}")

    print("\n[T+?] Waiting for both turns to finish...")
    t0 = time.time()
    while time.time() - t0 < 300:
        summ = _session_summary(client, session)
        q = _queue_entries(client, session)
        turn_count = summ.get("turn_count", 0)
        is_active = summ.get("is_active")
        active_q = [e for e in q if e.get("status") in ("queued", "running")]
        if turn_count >= 2 and not is_active and not active_q:
            break
        time.sleep(1.0)
    else:
        print("  TIMEOUT waiting for completion")

    duration = time.time() - t0
    print(f"\n[T+{duration:.1f}s] Both turns done")

    final_q = _queue_entries(client, session)
    print(f"\nFinal queue: {len(final_q)} entries, statuses={[e.get('status') for e in final_q]}")

    print("\nHistory (role, content_preview):")
    for i, (role, content) in enumerate(_history_roles(client, session)):
        print(f"  [{i}] {role}: {content}")

    print("\nSession summary:")
    summ = _session_summary(client, session)
    print(f"  turn_count={summ.get('turn_count')}  is_active={summ.get('is_active')}")

    print("\nVerdict:")
    roles = [r for r, _ in _history_roles(client, session)]
    user_count = roles.count("user")
    assistant_count = roles.count("assistant")
    print(f"  users={user_count}  assistants={assistant_count}")
    print(f"  chronology OK? {roles == ['user', 'assistant', 'user', 'assistant']}")
    print(f"  queue fully cleared? {all(e.get('status') not in ('queued', 'running') for e in final_q)}")


if __name__ == "__main__":
    main()
