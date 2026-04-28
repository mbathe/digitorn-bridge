#!/usr/bin/env python3
"""Wave 2 runner - deep validation tests only.

Usage:
    py -3.12 tests/production/run_wave2.py
"""

import importlib
import sys
import time
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.production.helpers import R, DAEMON, ensure_auth, cleanup_app

import httpx


def main():
    print("=" * 70)
    print("DIGITORN DAEMON - WAVE 2: DEEP VALIDATION TESTS")
    print("=" * 70)
    print(f"Daemon:    {DAEMON}")
    print(f"Time:      {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Pre-flight
    try:
        r = httpx.get(f"{DAEMON}/health", timeout=5.0)
        assert r.status_code == 200
        print(f"Daemon:    OK (v{r.json().get('version', '?')})")
    except Exception as exc:
        print(f"\n\033[31mERROR: Daemon not running at {DAEMON}\033[0m")
        sys.exit(1)

    try:
        ensure_auth()
        print(f"Auth:      OK")
    except Exception as exc:
        print(f"\n\033[31mERROR: Auth failed\033[0m: {exc}")
        sys.exit(1)

    print(f"\n{'=' * 70}\n")

    # Wave 2 test modules only
    test_modules = [
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

    # Summary
    print(f"\n{'=' * 70}")
    print(f"WAVE 2 RESULTS: {R.summary()}")
    print(f"Time: {elapsed:.1f}s")

    if R.failed:
        print(f"\n\033[31mFAILURES ({len(R.failed)}):\033[0m")
        for name in R.failed:
            print(f"  - {name}")
    else:
        print(f"\n\033[32mALL WAVE 2 TESTS PASSED\033[0m")

    if R.skipped:
        print(f"\n\033[33mSKIPPED ({len(R.skipped)}):\033[0m")
        for name in R.skipped[:15]:
            print(f"  - {name}")
        if len(R.skipped) > 15:
            print(f"  ... and {len(R.skipped) - 15} more")

    print("=" * 70)
    sys.exit(1 if R.failed else 0)


if __name__ == "__main__":
    main()
