"""Burst test: send 5 POSTs back-to-back.

Proves reserve_session blocks concurrent dispatches by inspecting the
correlation_ids returned in the POST responses:
  - exactly ONE should be fast-path (fp-xxx)
  - all others should be queue UUIDs
"""
from __future__ import annotations

import uuid

from digitorn.testing import DevClient
from digitorn.testing.models import SessionHandle


def main() -> None:
    client = DevClient(auto_approve=True)
    app_id = "digitorn-chat"

    sid = f"burst-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=app_id, daemon_url=client.daemon_url, workspace="",
    )
    print(f"session={sid}\n")

    N = 5
    correlation_ids: list[str] = []
    statuses: list[int] = []

    for i in range(N):
        r = client._post(
            f"/api/apps/{app_id}/sessions/{sid}/messages",
            json={"message": f"Message {i}: réponds juste 'ok {i}'.", "workspace": ""},
        )
        statuses.append(r.status_code)
        body = r.json()
        cid = body.get("data", {}).get("correlation_id", "")
        correlation_ids.append(cid)
        print(f"  POST {i}: status={r.status_code} correlation_id={cid}")

    print(f"\nAll status codes: {statuses}")
    print(f"All correlation_ids: {correlation_ids}")

    fast_path = [c for c in correlation_ids if c and c.startswith("fp-")]
    queued = [c for c in correlation_ids if c and not c.startswith("fp-")]
    missing = [i for i, c in enumerate(correlation_ids) if not c]

    print(f"\nFast-path count: {len(fast_path)}")
    print(f"Queued count: {len(queued)}")
    print(f"Missing correlation_id: {missing}")

    assert all(s in (200, 202) for s in statuses), f"Non-200 status: {statuses}"
    assert len(fast_path) == 1, f"Expected exactly 1 fast-path, got {len(fast_path)}: {fast_path}"
    assert len(queued) == N - 1, f"Expected {N-1} queued, got {len(queued)}: {queued}"

    print("\nPASS: reserve_session correctly gates fast-path for concurrent POSTs")

    r = client._get(f"/api/apps/{app_id}/sessions/{sid}/queue")
    entries = r.json().get("data", {}).get("entries", []) or []
    statuses_q = [e.get("status") for e in entries]
    print(f"\nQueue state right after burst: entries={len(entries)} statuses={statuses_q}")


if __name__ == "__main__":
    main()
