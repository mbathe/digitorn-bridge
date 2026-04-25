"""Concurrency load test — N users sending M messages in parallel.

Each virtual user:
  1. Registers / logs in (dedicated account).
  2. Opens a Socket.IO stream on a fresh session.
  3. POSTs M short prompts back-to-back.
  4. Measures:
     - POST latency (HTTP response time)
     - time-to-first-event (message_started received)
     - time-to-done (message_done received)

All users run concurrently using a thread pool so the daemon sees
simultaneous load. A background sampler polls ``/health`` every 2 s
to capture ``event_loop_lag_ms`` + watchdog ``stalls_total``.

Aggregates p50 / p95 / max per step and flags the first user that
errors out.

Usage:
    DAEMON_URL=http://127.0.0.1:9876 N=5 M=1 py -3.12 \\
        tools/live_tests/concurrency_load.py
"""
from __future__ import annotations

import concurrent.futures
import json
import os as _os
import statistics
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from digitorn.testing.client import DevClient
from digitorn.testing.models import SessionHandle


_PROMPT = (
    "Reponds simplement 'pret' en un seul mot. Aucun outil, aucun fichier."
)


@dataclass
class TurnTiming:
    user_id: str
    session_id: str
    post_ms: float = 0.0
    first_event_ms: float = 0.0
    done_ms: float = 0.0
    error: str = ""


@dataclass
class HealthSample:
    t: float
    loop_lag_ms: float
    stalls_total: int
    last_stall_gap_ms: float


def _health_sampler(
    daemon_url: str, stop_evt: threading.Event, samples: list[HealthSample],
) -> None:
    while not stop_evt.is_set():
        try:
            r = httpx.get(f"{daemon_url}/health", timeout=3.0)
            if r.status_code == 200:
                d = r.json()
                wd = d.get("event_loop_watchdog") or {}
                samples.append(HealthSample(
                    t=time.time(),
                    loop_lag_ms=float(d.get("event_loop_lag_ms") or 0.0),
                    stalls_total=int(wd.get("stalls_total") or 0),
                    last_stall_gap_ms=float(wd.get("last_stall_gap_ms") or 0.0),
                ))
        except Exception:
            pass
        stop_evt.wait(2.0)


def _direct_register_or_login(
    daemon_url: str, email: str, password: str,
) -> tuple[str | None, str]:
    """Auth directly via httpx so we don't trigger the interactive CLI
    prompt that ``DevClient.with_user`` falls into when no creds file
    exists. Returns (access_token|None, diagnostic)."""
    last_detail = ""
    for path in ("/auth/login", "/auth/register"):
        try:
            body: dict[str, Any] = {"email": email, "password": password}
            if path.endswith("register"):
                body["username"] = email.split("@")[0]
                body["name"] = email.split("@")[0]
            r = httpx.post(f"{daemon_url}{path}", json=body, timeout=30.0)
            if r.status_code == 200:
                return r.json().get("access_token"), ""
            last_detail = f"{path}->{r.status_code}:{r.text[:100]}"
        except Exception as exc:
            last_detail = f"{path}->{type(exc).__name__}:{exc}"
    return None, last_detail


def _run_user_with_token(
    daemon_url: str, app_id: str, user_idx: int, m_messages: int,
    run_id: str, token: str,
) -> list[TurnTiming]:
    """One virtual user — M turns back-to-back on a single session. The
    token is obtained ahead of time so auth work is excluded from the
    concurrent section."""
    email = f"load-{run_id}-{user_idx}@test.local"
    timings: list[TurnTiming] = []
    client = DevClient.with_token(token, daemon_url=daemon_url)

    sid = f"load-{run_id}-u{user_idx}"
    session = SessionHandle(
        session_id=sid, app_id=app_id, daemon_url=daemon_url, workspace="",
    )
    stream = None
    for msg_i in range(m_messages):
        rec = TurnTiming(user_id=email, session_id=sid)
        try:
            t_post_start = time.perf_counter()
            post = client.post_message_raw(session, _PROMPT)
            rec.post_ms = (time.perf_counter() - t_post_start) * 1000
            cid = (post.get("body") or {}).get("data", {}).get("correlation_id") or ""
            if not cid:
                rec.error = f"no correlation_id (status={post.get('status_code')})"
                timings.append(rec)
                continue

            if stream is None:
                stream = client.open_event_stream(session, wait_for_session=True)

            t_wait_start = time.perf_counter()
            started = stream.wait_for(
                "message_started", timeout=60.0,
                predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
            )
            if started is not None:
                rec.first_event_ms = (time.perf_counter() - t_wait_start) * 1000

            done = stream.wait_for(
                "message_done", timeout=180.0,
                predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
            )
            if done is None:
                rec.error = "no message_done within 180s"
            else:
                rec.done_ms = (time.perf_counter() - t_post_start) * 1000
        except Exception as exc:
            rec.error = f"{type(exc).__name__}: {exc}"
        timings.append(rec)

    if stream is not None:
        try:
            stream.stop(timeout=2.0)
        except Exception:
            pass
    return timings


def _pct(vs: list[float], q: float) -> float:
    if not vs:
        return 0.0
    vs_sorted = sorted(vs)
    k = max(0, min(len(vs_sorted) - 1, int(round(q * (len(vs_sorted) - 1)))))
    return vs_sorted[k]


def run(daemon_url: str, app_id: str, n_users: int, m_messages: int) -> int:
    print(f"\n=== concurrency_load N={n_users} users × M={m_messages} msg on {app_id} ===\n")
    run_id = uuid.uuid4().hex[:6]

    # Pre-register users sequentially so the timed section exercises
    # chat concurrency, not the SQLite single-writer auth path. (When
    # measured, register contended on the ``users`` table INSERT under
    # WAL — 50 parallel registers saturated SQLite's ~20 writes/sec
    # ceiling long before the event loop itself broke a sweat.)
    print(f"pre-registering {n_users} users sequentially...")
    t_reg = time.perf_counter()
    tokens: dict[int, str] = {}
    password = "LoadTestPassword123!"
    for i in range(n_users):
        email = f"load-{run_id}-{i}@test.local"
        tok, detail = _direct_register_or_login(daemon_url, email, password)
        if tok:
            tokens[i] = tok
        else:
            print(f"  [skip] user {i}: {detail}")
    print(f"  pre-registration took {time.perf_counter()-t_reg:.1f}s "
          f"({len(tokens)}/{n_users} ok)")
    if not tokens:
        print("  no authenticated users — aborting")
        return 1

    stop_evt = threading.Event()
    health_samples: list[HealthSample] = []
    sampler = threading.Thread(
        target=_health_sampler, args=(daemon_url, stop_evt, health_samples),
        daemon=True, name="health-sampler",
    )
    sampler.start()

    t_wall = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tokens)) as pool:
        futs = [
            pool.submit(
                _run_user_with_token, daemon_url, app_id, i, m_messages,
                run_id, tokens[i],
            )
            for i in sorted(tokens.keys())
        ]
        all_timings: list[TurnTiming] = []
        for f in concurrent.futures.as_completed(futs):
            try:
                all_timings.extend(f.result())
            except Exception as exc:
                all_timings.append(TurnTiming(
                    user_id="?", session_id="",
                    error=f"worker crash: {type(exc).__name__}: {exc}",
                ))
    wall_s = time.perf_counter() - t_wall

    stop_evt.set()
    sampler.join(timeout=3.0)

    # ── aggregate ──
    ok = [t for t in all_timings if not t.error]
    fail = [t for t in all_timings if t.error]
    post_ms = [t.post_ms for t in ok if t.post_ms > 0]
    fe_ms = [t.first_event_ms for t in ok if t.first_event_ms > 0]
    done_ms = [t.done_ms for t in ok if t.done_ms > 0]
    lag_samples = [s.loop_lag_ms for s in health_samples]
    max_stalls = max((s.stalls_total for s in health_samples), default=0)
    baseline_stalls = min((s.stalls_total for s in health_samples), default=0)
    new_stalls = max_stalls - baseline_stalls

    def _fmt(vs: list[float]) -> str:
        if not vs:
            return "n/a"
        return (
            f"p50={_pct(vs, 0.5):7.0f}  p95={_pct(vs, 0.95):7.0f}  "
            f"max={max(vs):7.0f}  n={len(vs)}"
        )

    print(f"wall clock     : {wall_s:.1f}s")
    print(f"total turns    : {len(all_timings)}  ok={len(ok)}  fail={len(fail)}")
    print(f"POST ms        : {_fmt(post_ms)}")
    print(f"first_event ms : {_fmt(fe_ms)}")
    print(f"full done ms   : {_fmt(done_ms)}")
    print(
        f"/health lag ms : p50={_pct(lag_samples, 0.5):.0f}  "
        f"p95={_pct(lag_samples, 0.95):.0f}  "
        f"max={max(lag_samples) if lag_samples else 0:.0f}  "
        f"samples={len(lag_samples)}"
    )
    print(
        f"loop stalls    : new={new_stalls}  "
        f"last_gap_ms={max((s.last_stall_gap_ms for s in health_samples), default=0):.0f}"
    )
    if fail:
        print("\nfailures (up to 5):")
        for t in fail[:5]:
            print(f"  {t.user_id} {t.session_id}: {t.error}")

    # Pass/fail heuristics for the runner's exit code.
    critical = (
        new_stalls > 0
        or len(fail) > max(1, n_users // 10)  # > 10% fail rate (min 1)
    )
    return 1 if critical else 0


if __name__ == "__main__":
    daemon_url = _os.environ.get("DAEMON_URL", "http://127.0.0.1:9876")
    app_id = _os.environ.get("APP_ID", "digitorn-chat")
    n = int(_os.environ.get("N", "5"))
    m = int(_os.environ.get("M", "1"))
    sys.exit(run(daemon_url, app_id, n, m))
