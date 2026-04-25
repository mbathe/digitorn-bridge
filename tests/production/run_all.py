#!/usr/bin/env python3
"""Master runner — executes ALL production test suites.

Usage:
    digitorn service start          # Start daemon first
    py -3.12 tests/production/run_all.py

Environment:
    DIGITORN_TEST_DAEMON=http://127.0.0.1:8000
    DIGITORN_TEST_APP=examples/opencode/app.yaml
"""

import importlib
import sys
import time
import os

# Ensure project root is on path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.production.helpers import R, DAEMON, ensure_auth, ensure_app_deployed, cleanup_app

import httpx


def main():
    print("=" * 70)
    print("DIGITORN DAEMON — FULL PRODUCTION TEST SUITE")
    print("=" * 70)
    print(f"Daemon:    {DAEMON}")
    print(f"Time:      {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Pre-flight check
    try:
        r = httpx.get(f"{DAEMON}/health", timeout=5.0)
        assert r.status_code == 200
        info = r.json()
        print(f"Daemon:    OK (v{info.get('version', '?')})")
    except Exception as exc:
        print(f"\n\033[31mERROR: Daemon not running at {DAEMON}\033[0m")
        print(f"Start with: digitorn service start")
        print(f"Error: {exc}")
        sys.exit(1)

    # Authenticate
    try:
        ensure_auth()
        print(f"Auth:      OK")
    except Exception as exc:
        print(f"\n\033[31mERROR: Authentication failed\033[0m")
        print(f"Error: {exc}")
        sys.exit(1)

    # Deploy test app
    try:
        app_id = ensure_app_deployed()
        print(f"Test app:  {app_id}")
    except Exception as exc:
        print(f"\n\033[31mERROR: App deployment failed\033[0m")
        print(f"Error: {exc}")
        sys.exit(1)

    print(f"\n{'=' * 70}\n")

    # Discover and run test modules
    # Wave 1: Core tests (from initial suite)
    # Wave 2: Deep validation tests (test_10+)
    test_modules = [
        "tests.production.test_01_auth_health",
        "tests.production.test_02_app_lifecycle",
        "tests.production.test_03_chat_streaming",
        "tests.production.test_04_sessions",
        "tests.production.test_05_security_errors",
        "tests.production.test_06_metrics_modules",
        "tests.production.test_07_llm_context",
        "tests.production.test_08_performance",
        # Wave 2: Deep validation
        "tests.production.test_10_streaming_deep",
        "tests.production.test_11_tokens_cost",
        "tests.production.test_12_retry_recovery",
        "tests.production.test_13_bg_tasks_watchers",
        "tests.production.test_14_compaction_context",
        "tests.production.test_15_limits_stress",
        "tests.production.test_16_tool_calls_deep",
        "tests.production.test_17_session_hooks",
    ]

    t0 = time.monotonic()

    for module_name in test_modules:
        try:
            mod = importlib.import_module(module_name)
            if hasattr(mod, "run_all"):
                mod.run_all()
            else:
                print(f"\n\033[33mWARN: {module_name} has no run_all()\033[0m")
        except ImportError as exc:
            R.skip(f"[{module_name}]", f"Import error: {exc}")
        except Exception as exc:
            R.fail(f"[{module_name}] CRASH", str(exc)[:200])
            import traceback
            traceback.print_exc()

    elapsed = time.monotonic() - t0

    # Cleanup
    print(f"\n{'=' * 70}")
    print("Cleaning up...")
    cleanup_app()

    # Final summary
    print(f"\n{'=' * 70}")
    print(f"RESULTS: {R.summary()}")
    print(f"Time: {elapsed:.1f}s")

    if R.failed:
        print(f"\n\033[31mFAILURES ({len(R.failed)}):\033[0m")
        for name in R.failed:
            print(f"  - {name}")
    else:
        print(f"\n\033[32mALL TESTS PASSED\033[0m")

    if R.skipped:
        print(f"\n\033[33mSKIPPED ({len(R.skipped)}):\033[0m")
        for name in R.skipped[:10]:
            print(f"  - {name}")
        if len(R.skipped) > 10:
            print(f"  ... and {len(R.skipped) - 10} more")

    print("=" * 70)
    sys.exit(1 if R.failed else 0)


if __name__ == "__main__":
    main()
