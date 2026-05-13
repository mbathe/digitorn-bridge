"""Production smoke test orchestrator.

Runs the curated set of ``prod_*`` scenarios in ``tools/live_tests/``
against a live daemon, aggregates pass/fail, and prints a tight summary
suitable for use as a deploy gate.

Each scenario lives in its own file and exposes a ``run()`` function
returning ``(ok: bool, bugs: list[str], artifacts: dict)``. This
orchestrator imports them, runs each with an isolation timeout, and
prints a final ``PASS`` / ``FAIL`` per scenario plus a global verdict.

Run:
    py -3.12 tools/smoke_prod.py
    py -3.12 tools/smoke_prod.py --only chat,abort  # subset
    py -3.12 tools/smoke_prod.py --timeout 120      # per-scenario seconds
    py -3.12 tools/smoke_prod.py --fast             # core 3 only

Environment:
    DIGITORN_TEST_TOKEN   Bearer token for daemon auth. Required.
    DIGITORN_DAEMON_URL   Override the daemon URL (default: http://127.0.0.1:8000).

Exit codes:
    0  every scenario passed
    1  at least one scenario failed
    2  setup error (no token, daemon unreachable, ...)
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
import time
import traceback
from pathlib import Path


# Curated set of scenarios with stable ``run()`` signature. Order =
# priority (lighter scenarios first; if an early one fails the whole
# stack is probably broken and later ones aren't worth waiting for).
SCENARIOS: list[tuple[str, str, int]] = [
    # (short_name, module_path, per-scenario timeout seconds)
    ("chat",       "tools.live_tests.prod_chat_multiturn", 120),
    ("abort",      "tools.live_tests.prod_abort",           90),
    ("queue",      "tools.live_tests.prod_queue",          120),
    ("concurrent", "tools.live_tests.prod_concurrent",     180),
    ("reconnect",  "tools.live_tests.prod_reconnect",      120),
    ("sessions",   "tools.live_tests.prod_sessions_ops",    90),
    ("code_tools", "tools.live_tests.prod_code_tools",     180),
]

FAST_SET = {"chat", "abort", "queue"}


def _check_daemon(daemon_url: str) -> str | None:
    """Return None on success, error message on failure."""
    try:
        import urllib.request
        req = urllib.request.Request(f"{daemon_url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                return f"/health returned {resp.status}"
        return None
    except Exception as exc:
        return f"/health unreachable: {exc}"


def _import_run(module_path: str):
    """Import the module and return its ``run`` callable."""
    mod = importlib.import_module(module_path)
    fn = getattr(mod, "run", None)
    if not callable(fn):
        raise RuntimeError(f"{module_path} has no run() callable")
    return fn


def _run_one(name: str, module_path: str, timeout: int) -> dict:
    """Run one scenario, capturing pass/fail + duration."""
    t0 = time.monotonic()
    result: dict = {
        "name": name, "module": module_path,
        "ok": False, "bugs": [],
        "duration_s": 0.0, "error": None,
    }
    try:
        run_fn = _import_run(module_path)
    except Exception as exc:
        result["error"] = f"import failed: {exc}"
        result["duration_s"] = time.monotonic() - t0
        return result

    # We do NOT use signal-based timeout on Windows. Each scenario has
    # its own internal timeouts via DevClient.wait_for(). The outer
    # ``timeout`` here is a courtesy hint, enforced by the scenario.
    try:
        ok, bugs, _artifacts = run_fn()
        result["ok"] = bool(ok)
        result["bugs"] = list(bugs or [])
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["bugs"] = traceback.format_exc().splitlines()[-3:]
    finally:
        result["duration_s"] = time.monotonic() - t0
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only", default="",
        help="Comma-separated subset of scenario short names to run.",
    )
    parser.add_argument(
        "--fast", action="store_true",
        help=f"Run only the core 3: {','.join(sorted(FAST_SET))}.",
    )
    parser.add_argument(
        "--timeout", type=int, default=180,
        help="Per-scenario advisory timeout in seconds.",
    )
    args = parser.parse_args(argv)

    daemon_url = os.environ.get(
        "DIGITORN_DAEMON_URL", "http://127.0.0.1:8000",
    )
    token = os.environ.get("DIGITORN_TEST_TOKEN", "")
    if not token:
        print("ERR: DIGITORN_TEST_TOKEN env var is empty.")
        print("     Set it to a fresh user JWT, e.g.")
        print("       $env:DIGITORN_TEST_TOKEN = (Get-Content "
              "~/.digitorn/.test-token).Trim()")
        return 2

    # Make ``tools.live_tests.*`` importable when running from repo root.
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    err = _check_daemon(daemon_url)
    if err is not None:
        print(f"ERR: daemon not reachable at {daemon_url}: {err}")
        return 2

    # Resolve the scenario set.
    if args.fast:
        wanted = FAST_SET
    elif args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
    else:
        wanted = {s[0] for s in SCENARIOS}
    plan = [(n, m, t) for (n, m, t) in SCENARIOS if n in wanted]
    if not plan:
        print(f"ERR: no scenarios match --only={args.only}.")
        return 2

    print(f"\n=== Digitorn prod smoke (daemon={daemon_url}) ===")
    print(f"Scenarios: {', '.join(n for n, _, _ in plan)}")
    print()

    results: list[dict] = []
    for name, module_path, t in plan:
        timeout = min(t, args.timeout) if args.timeout else t
        print(f"[ RUN  ] {name:12s} (timeout={timeout}s) ... ", end="", flush=True)
        r = _run_one(name, module_path, timeout)
        results.append(r)
        verdict = "PASS" if r["ok"] else "FAIL"
        print(f"{verdict}  ({r['duration_s']:.1f}s)")
        if not r["ok"]:
            if r.get("error"):
                print(f"          ERROR: {r['error']}")
            for bug in r["bugs"][:5]:
                print(f"          - {bug}")

    print()
    print("=" * 60)
    passed = sum(1 for r in results if r["ok"])
    failed = len(results) - passed
    total_s = sum(r["duration_s"] for r in results)
    print(f"Summary: {passed}/{len(results)} PASSED, {failed} FAILED  "
          f"(total {total_s:.1f}s)")
    print("=" * 60)
    if failed:
        print("\nFailed scenarios:")
        for r in results:
            if not r["ok"]:
                print(f"  - {r['name']}: {r.get('error') or r['bugs'][:1]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
