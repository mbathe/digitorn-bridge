"""Reproducer for the 'daemon freezes when agent writes HTML' report.

Sends the exact prompt the user pasted, then waits for message_done
with a bounded timeout. Also pings /api/apps every 5s from a
background thread so we can tell whether the daemon is still
responsive during the turn or actually frozen.
"""
from __future__ import annotations

import os as _os
import sys
import threading
import time
import uuid
from typing import Any

import httpx

from digitorn.testing.client import DevClient
from digitorn.testing.models import SessionHandle


_PROMPT = _os.environ.get(
    "PROMPT",
    "Genere moi une page web HTML complete avec plusieurs animations CSS "
    "interessantes (particules, dégradés animés, transitions). "
    "Donne le code HTML/CSS complet inline, sans fichier separé.",
)


def _ping_loop(url: str, stop_evt: threading.Event, log: list) -> None:
    while not stop_evt.is_set():
        t0 = time.perf_counter()
        try:
            r = httpx.get(url, timeout=3.0)
            ok = r.status_code == 200
            dt = time.perf_counter() - t0
            log.append((time.time(), ok, round(dt * 1000, 1)))
        except Exception as exc:
            log.append((time.time(), False, f"ERR {type(exc).__name__}"))
        stop_evt.wait(5.0)


def main() -> int:
    daemon_url = _os.environ.get("DAEMON_URL", "http://127.0.0.1:8000")
    email = _os.environ.get("DEV_EMAIL", "dev@digitorn.local")
    password = _os.environ.get("DEV_PASSWORD", "DevPassword123!")
    app_id = _os.environ.get("APP_ID", "ws-preview-test")
    total_timeout = float(_os.environ.get("TIMEOUT_S", "60"))

    client = DevClient.with_user(
        email, password, daemon_url=daemon_url, register_if_missing=True,
    )

    sid = f"html-freeze-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=app_id, daemon_url=daemon_url, workspace="",
    )
    print(f"daemon={daemon_url} app={app_id} session={sid}")

    stop_evt = threading.Event()
    ping_log: list[tuple[float, Any, Any]] = []
    pinger = threading.Thread(
        target=_ping_loop,
        args=(f"{daemon_url}/api/apps", stop_evt, ping_log),
        daemon=True,
    )
    pinger.start()

    t_start = time.perf_counter()
    post = client.post_message_raw(session, _PROMPT)
    cid = (post.get("body") or {}).get("data", {}).get("correlation_id") or ""
    print(f"POST returned in {(time.perf_counter()-t_start)*1000:.0f}ms "
          f"correlation_id={cid}")

    stream = client.open_event_stream(session, wait_for_session=True)
    try:
        t0 = time.perf_counter()
        done = stream.wait_for(
            "message_done", timeout=total_timeout,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
        )
        elapsed = time.perf_counter() - t0
        if done is None:
            print(f"\nFAIL: turn did not complete within {total_timeout}s "
                  f"(elapsed={elapsed:.1f}s)")
        else:
            print(f"\nPASS: turn completed in {elapsed:.1f}s")

        events = stream.events()
        by_type: dict[str, int] = {}
        for e in events:
            t = str(e.get("type") or "")
            by_type[t] = by_type.get(t, 0) + 1
        print(f"event_counts={dict(sorted(by_type.items()))}")
    finally:
        stop_evt.set()
        stream.stop(timeout=2.0)
        pinger.join(timeout=2.0)

    print("\n--- daemon /api/apps ping log (every 5s) ---")
    for ts, ok, detail in ping_log:
        flag = "OK " if ok is True else "FAIL"
        print(f"  {time.strftime('%H:%M:%S', time.localtime(ts))} {flag} {detail}ms")
    unresponsive = sum(1 for _, ok, _ in ping_log if ok is not True)
    print(f"unresponsive_pings={unresponsive}/{len(ping_log)}")

    return 0 if (done is not None and unresponsive == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
