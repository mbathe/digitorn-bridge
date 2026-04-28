#!/usr/bin/env python3
"""Stress test - concurrent sessions and requests against the daemon.

Tests:
  1. Health endpoint throughput (no auth, no LLM)
  2. Authenticated API throughput (list apps, sessions, tools)
  3. Session creation via chat (real LLM calls)
  4. Concurrent chat sessions (real LLM calls)
  5. Mixed workload (health + list + chat)

Usage:
    py -3.12 tests/stress/run_stress.py
"""

from __future__ import annotations

import os
import sys
import time
import uuid
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import httpx

# ── Config ──────────────────────────────────────────────────

DAEMON = os.environ.get("DIGITORN_TEST_DAEMON", "http://127.0.0.1:8000")
TIMEOUT = 30.0
STREAM_TIMEOUT = 120.0
WORKSPACE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Concurrency levels - realistic for single-worker Windows
HEALTH_CONCURRENCY = 50
HEALTH_TOTAL = 500
API_CONCURRENCY = 30
API_TOTAL = 300
SESSION_CONCURRENCY = 10
SESSION_TOTAL = 50
CHAT_CONCURRENCY = 10
CHAT_TOTAL = 30
MIXED_CONCURRENCY = 30
MIXED_TOTAL = 300


@dataclass
class StressResults:
    name: str
    total: int = 0
    success: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)
    elapsed: float = 0.0

    def summary(self) -> str:
        ok_latencies = sorted(self.latencies) if self.latencies else [0]
        n = len(ok_latencies)
        p50 = ok_latencies[n // 2] if n else 0
        p95 = ok_latencies[int(n * 0.95)] if n > 1 else ok_latencies[-1]
        p99 = ok_latencies[int(n * 0.99)] if n > 1 else ok_latencies[-1]
        rps = self.total / self.elapsed if self.elapsed > 0 else 0
        status = "\033[32mOK\033[0m" if self.failed == 0 else (
            "\033[33mPARTIAL\033[0m" if self.success > self.total * 0.8 else "\033[31mFAIL\033[0m"
        )
        return (
            f"  {status} {self.name}: {self.success}/{self.total} "
            f"({self.failed} fail) {self.elapsed:.1f}s "
            f"| {rps:.0f} req/s "
            f"| p50={p50*1000:.0f}ms p95={p95*1000:.0f}ms p99={p99*1000:.0f}ms"
        )


# ── Auth ────────────────────────────────────────────────────

_auth_headers: dict[str, str] = {}


def ensure_auth() -> dict[str, str]:
    global _auth_headers
    if _auth_headers:
        return _auth_headers
    password = "StressTest_Secure_12345!"
    username = f"stress_{uuid.uuid4().hex[:8]}"
    r = httpx.post(f"{DAEMON}/auth/register", json={
        "username": username, "password": password,
        "email": f"{username}@test.local",
    }, timeout=TIMEOUT)
    if r.status_code == 200:
        token = r.json()["access_token"]
    else:
        # Fallback: login with existing user
        r = httpx.post(f"{DAEMON}/auth/login", json={
            "username": "testuser", "password": "testpass12345!",
        }, timeout=TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"Cannot authenticate: {r.status_code} {r.text[:200]}")
        token = r.json()["access_token"]
    _auth_headers = {"Authorization": f"Bearer {token}"}
    return _auth_headers


def req(method: str, path: str, **kwargs) -> httpx.Response:
    kwargs.setdefault("timeout", TIMEOUT)
    headers = dict(kwargs.pop("headers", {}))
    headers.update(ensure_auth())
    kwargs["headers"] = headers
    return getattr(httpx, method.lower())(f"{DAEMON}{path}", **kwargs)


def ensure_app_deployed() -> str:
    """Deploy test-full app, return app_id."""
    r = req("get", "/api/apps/test-full")
    if r.status_code == 200:
        return "test-full"
    yaml_path = os.path.abspath(os.path.join(WORKSPACE, "examples", "test-full.yaml"))
    for attempt in range(3):
        r = req("post", "/api/apps/deploy", json={"yaml_path": yaml_path, "force": True})
        if r.status_code == 429:
            time.sleep(r.json().get("retry_after", 10) + 1)
            continue
        if r.status_code == 200:
            data = r.json()
            return data.get("data", {}).get("app_id", "test-full")
        break
    raise RuntimeError(f"Cannot deploy app: {r.status_code} {r.text[:200]}")


# ── Infra detection ─────────────────────────────────────────

def detect_infra() -> dict[str, str]:
    """Detect actual backend infrastructure."""
    info = {}
    # Redis
    try:
        import redis as _r
        c = _r.Redis(host="127.0.0.1", port=6379, socket_timeout=2)
        c.ping()
        info["redis"] = f"OK ({c.dbsize()} keys)"
        c.close()
    except Exception:
        info["redis"] = "not available"
    # PostgreSQL
    try:
        import subprocess
        r = subprocess.run(
            [r"C:\Program Files\PostgreSQL\17\bin\pg_isready", "-h", "localhost", "-p", "5432"],
            capture_output=True, text=True, timeout=5,
        )
        info["postgresql"] = "OK" if r.returncode == 0 else "not available"
    except Exception:
        info["postgresql"] = "not available"
    return info


# ── Test 1: Health throughput ───────────────────────────────

def test_health_throughput() -> StressResults:
    result = StressResults(name="Health throughput", total=HEALTH_TOTAL)

    def do_health(_: int) -> tuple[bool, float]:
        t0 = time.perf_counter()
        try:
            r = httpx.get(f"{DAEMON}/health", timeout=10.0)
            return r.status_code == 200, time.perf_counter() - t0
        except Exception:
            return False, time.perf_counter() - t0

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=HEALTH_CONCURRENCY) as pool:
        for ok, lat in pool.map(lambda i: do_health(i), range(HEALTH_TOTAL)):
            result.success += ok
            result.failed += not ok
            result.latencies.append(lat)
    result.elapsed = time.monotonic() - t0
    return result


# ── Test 2: Authenticated API throughput ────────────────────

def test_api_throughput() -> StressResults:
    result = StressResults(name="API throughput (auth)", total=API_TOTAL)
    headers = ensure_auth()

    endpoints = [
        "/api/apps",
        f"/api/apps/test-full",
        f"/api/apps/test-full/sessions",
    ]

    def do_api(i: int) -> tuple[bool, float]:
        ep = endpoints[i % len(endpoints)]
        t0 = time.perf_counter()
        try:
            r = httpx.get(f"{DAEMON}{ep}", headers=headers, timeout=15.0)
            return r.status_code == 200, time.perf_counter() - t0
        except Exception:
            return False, time.perf_counter() - t0

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=API_CONCURRENCY) as pool:
        for ok, lat in pool.map(lambda i: do_api(i), range(API_TOTAL)):
            result.success += ok
            result.failed += not ok
            result.latencies.append(lat)
    result.elapsed = time.monotonic() - t0
    return result


# ── Test 3: Session creation ────────────────────────────────

def test_session_creation() -> StressResults:
    result = StressResults(name="Session creation (LLM)", total=SESSION_TOTAL)
    headers = ensure_auth()

    def do_create(i: int) -> tuple[bool, float]:
        sid = str(uuid.uuid4())
        t0 = time.perf_counter()
        try:
            r = httpx.post(
                f"{DAEMON}/api/apps/test-full/chat",
                headers=headers,
                json={"session_id": sid, "message": f"Say {i}."},
                timeout=STREAM_TIMEOUT,
            )
            return r.status_code == 200, time.perf_counter() - t0
        except Exception:
            return False, time.perf_counter() - t0

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=SESSION_CONCURRENCY) as pool:
        for ok, lat in pool.map(lambda i: do_create(i), range(SESSION_TOTAL)):
            result.success += ok
            result.failed += not ok
            result.latencies.append(lat)
    result.elapsed = time.monotonic() - t0
    return result


# ── Test 4: Concurrent SSE chat ─────────────────────────────

def test_concurrent_chat() -> StressResults:
    result = StressResults(name="Concurrent SSE chat (LLM)", total=CHAT_TOTAL)
    headers = ensure_auth()

    def do_chat(i: int) -> tuple[bool, float]:
        sid = str(uuid.uuid4())
        t0 = time.perf_counter()
        try:
            r = httpx.post(
                f"{DAEMON}/api/apps/test-full/chat/stream",
                headers=headers,
                json={"session_id": sid, "message": f"Say {i}.", "workspace": WORKSPACE},
                timeout=STREAM_TIMEOUT,
            )
            return r.status_code == 200, time.perf_counter() - t0
        except Exception:
            return False, time.perf_counter() - t0

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=CHAT_CONCURRENCY) as pool:
        for ok, lat in pool.map(lambda i: do_chat(i), range(CHAT_TOTAL)):
            result.success += ok
            result.failed += not ok
            result.latencies.append(lat)
    result.elapsed = time.monotonic() - t0
    return result


# ── Test 5: Mixed workload ─────────────────────────────────

def test_mixed_workload() -> StressResults:
    result = StressResults(name="Mixed workload", total=MIXED_TOTAL)
    headers = ensure_auth()

    def do_mixed(i: int) -> tuple[bool, float]:
        op = i % 5
        t0 = time.perf_counter()
        try:
            if op <= 1:
                # 40% health
                r = httpx.get(f"{DAEMON}/health", timeout=10.0)
            elif op == 2:
                # 20% app list
                r = httpx.get(f"{DAEMON}/api/apps", headers=headers, timeout=15.0)
            elif op == 3:
                # 20% session list
                r = httpx.get(f"{DAEMON}/api/apps/test-full/sessions", headers=headers, timeout=15.0)
            else:
                # 20% chat (LLM)
                r = httpx.post(
                    f"{DAEMON}/api/apps/test-full/chat",
                    headers=headers,
                    json={"session_id": str(uuid.uuid4()), "message": f"Say {i}."},
                    timeout=STREAM_TIMEOUT,
                )
            return r.status_code == 200, time.perf_counter() - t0
        except Exception:
            return False, time.perf_counter() - t0

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=MIXED_CONCURRENCY) as pool:
        for ok, lat in pool.map(lambda i: do_mixed(i), range(MIXED_TOTAL)):
            result.success += ok
            result.failed += not ok
            result.latencies.append(lat)
    result.elapsed = time.monotonic() - t0
    return result


# ── Main ────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("DIGITORN DAEMON - STRESS TEST")
    print("=" * 70)
    print(f"Daemon:    {DAEMON}")
    print(f"Time:      {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Pre-flight
    try:
        r = httpx.get(f"{DAEMON}/health", timeout=5.0)
        info = r.json()
        print(f"Health:    OK (v{info.get('version', '?')})")
    except Exception as e:
        print(f"\033[31mERROR: Daemon not reachable: {e}\033[0m")
        sys.exit(1)

    try:
        ensure_auth()
        print("Auth:      OK")
    except Exception as e:
        print(f"\033[31mERROR: Auth failed: {e}\033[0m")
        sys.exit(1)

    # Detect infra
    infra = detect_infra()
    for k, v in infra.items():
        print(f"{k:10s} {v}")

    # Deploy app
    try:
        app_id = ensure_app_deployed()
        print(f"App:       {app_id}")
    except Exception as e:
        print(f"\033[31mERROR: App deploy failed: {e}\033[0m")
        sys.exit(1)

    print(f"\n{'=' * 70}")
    print("Test plan:")
    print(f"  1. Health:     {HEALTH_TOTAL:>5} req, {HEALTH_CONCURRENCY:>3} concurrent")
    print(f"  2. API:        {API_TOTAL:>5} req, {API_CONCURRENCY:>3} concurrent")
    print(f"  3. Sessions:   {SESSION_TOTAL:>5} req, {SESSION_CONCURRENCY:>3} concurrent (LLM)")
    print(f"  4. Chat SSE:   {CHAT_TOTAL:>5} req, {CHAT_CONCURRENCY:>3} concurrent (LLM)")
    print(f"  5. Mixed:      {MIXED_TOTAL:>5} req, {MIXED_CONCURRENCY:>3} concurrent")
    print(f"{'=' * 70}\n")

    results: list[StressResults] = []

    for name, fn in [
        ("Health throughput", test_health_throughput),
        ("API throughput", test_api_throughput),
        ("Session creation", test_session_creation),
        ("Concurrent chat", test_concurrent_chat),
        ("Mixed workload", test_mixed_workload),
    ]:
        print(f"Running: {name}...")
        r = fn()
        print(r.summary())
        results.append(r)
        if r.errors:
            for e in r.errors[:3]:
                print(f"    error: {e[:120]}")
        print()

    # Summary
    total_ok = sum(r.success for r in results)
    total_all = sum(r.total for r in results)
    total_time = sum(r.elapsed for r in results)
    all_lat = []
    for r in results:
        all_lat.extend(r.latencies)
    all_lat.sort()
    n = len(all_lat)
    print("=" * 70)
    print(f"TOTAL:     {total_ok}/{total_all} successful ({total_all - total_ok} failed)")
    print(f"TIME:      {total_time:.1f}s")
    if all_lat:
        print(f"LATENCY:   p50={all_lat[n//2]*1000:.0f}ms  p95={all_lat[int(n*0.95)]*1000:.0f}ms  p99={all_lat[int(n*0.99)]*1000:.0f}ms")
    pct = total_ok / total_all * 100 if total_all else 0
    if pct >= 95:
        print(f"\033[32mSUCCESS RATE: {pct:.1f}%\033[0m")
    elif pct >= 80:
        print(f"\033[33mSUCCESS RATE: {pct:.1f}% (acceptable)\033[0m")
    else:
        print(f"\033[31mSUCCESS RATE: {pct:.1f}% (poor)\033[0m")
    print("=" * 70)
    sys.exit(0 if pct >= 80 else 1)


if __name__ == "__main__":
    main()
