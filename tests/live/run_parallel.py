"""Parallel test runner - stress tests the daemon while testing modules.

Launches N concurrent sessions against the port-8001 daemon using the
local Ollama model (llama3.1-8b-gpu). Same LLM, multiple agents, single
daemon. Verifies that concurrency works and modules behave correctly
under load.
"""
from __future__ import annotations

import io
import sys
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import os
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "packages"))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from framework import LiveTester, TestCase, TestOutcome

DAEMON_URL = "http://127.0.0.1:8001"
WORKSPACE = str(ROOT)

# All test cases from all modules, using Ollama variants
CASES: list[TestCase] = [
    # ── FILESYSTEM ──
    TestCase(
        name="fs/R1: Read normal file",
        app_id="filesystem-tester-ol", app_yaml="filesystem-tester-ol.yaml",
        message="Read the file tests/live/sandbox/hello.txt",
        expected_tools=["read"], expected_patterns=["Hello"],
    ),
    TestCase(
        name="fs/R2: Read empty file",
        app_id="filesystem-tester-ol", app_yaml="filesystem-tester-ol.yaml",
        message="Read tests/live/sandbox/empty.txt",
        expected_tools=["read"],
    ),
    TestCase(
        name="fs/G1: Grep existing pattern",
        app_id="filesystem-tester-ol", app_yaml="filesystem-tester-ol.yaml",
        message="Use Grep to find 'validate_behavior_config' in packages/digitorn/modules/behavior/",
        expected_tools=["grep"], expected_patterns=["validator.py"],
        forbidden_errors=["No matches found"],
    ),
    TestCase(
        name="fs/GL1: Glob yaml files",
        app_id="filesystem-tester-ol", app_yaml="filesystem-tester-ol.yaml",
        message="List all .yaml files in tests/live/apps using Glob",
        expected_tools=["glob"], expected_patterns=["filesystem-tester"],
    ),
    TestCase(
        name="fs/W1: Write file",
        app_id="filesystem-tester-ol", app_yaml="filesystem-tester-ol.yaml",
        message="Create tests/live/sandbox/par_w1.txt containing 'parallel test'",
        expected_tools=["write"],
    ),
    TestCase(
        name="fs/E1: Edit with read first",
        app_id="filesystem-tester-ol", app_yaml="filesystem-tester-ol.yaml",
        message="Read tests/live/sandbox/target.py then Edit to replace 'original' with 'parallel_modified'",
        expected_tools=["read", "edit"],
    ),

    # ── SHELL ──
    TestCase(
        name="sh/S1: Bash echo",
        app_id="shell-tester-ol", app_yaml="shell-tester-ol.yaml",
        message="Run bash command: echo parallel_test_OK",
        expected_tools=["bash"], expected_patterns=["parallel_test_OK"],
    ),
    TestCase(
        name="sh/S2: Bash pwd",
        app_id="shell-tester-ol", app_yaml="shell-tester-ol.yaml",
        message="Run bash command: pwd",
        expected_tools=["bash"], expected_patterns=["digitorn"],
    ),
    TestCase(
        name="sh/S3: Bash loop",
        app_id="shell-tester-ol", app_yaml="shell-tester-ol.yaml",
        message="Run bash: for i in 1 2 3; do echo num_$i; done",
        expected_tools=["bash"], expected_patterns=["num_1", "num_3"],
    ),

    # ── MEMORY ──
    TestCase(
        name="mem/M1: Set goal",
        app_id="memory-tester-ol", app_yaml="memory-tester-ol.yaml",
        message="Call memory.set_goal with goal='Build parallel test feature'",
        expected_tools=["set_goal"],
    ),
    TestCase(
        name="mem/M2: Remember fact",
        app_id="memory-tester-ol", app_yaml="memory-tester-ol.yaml",
        message="Use memory.remember with key='test_key' and value='parallel'",
        expected_tools=["remember"],
    ),
    TestCase(
        name="mem/M3: Create task",
        app_id="memory-tester-ol", app_yaml="memory-tester-ol.yaml",
        message="Use memory.task_create to create a task with title='Write tests'",
        expected_tools=["task_create"],
    ),
]


def run_one(tc: TestCase) -> TestOutcome:
    """Run a single test case with its own DevClient (thread-safe)."""
    tester = LiveTester(daemon_url=DAEMON_URL, workspace=WORKSPACE)
    # Deploy (idempotent - force=True)
    tester.ensure_deployed(tc.app_id, tc.app_yaml)
    return tester.run_case(tc)


def main(concurrency: int = 4):
    print(f"\n{'═' * 70}")
    print(f"  PARALLEL LIVE TESTS  (concurrency={concurrency})")
    print(f"  Daemon: {DAEMON_URL}   Model: llama3.1-8b-gpu")
    print(f"  Total cases: {len(CASES)}")
    print(f"{'═' * 70}\n")

    # Pre-deploy all apps once (sequentially) to avoid racing deploys
    seen_apps: set[str] = set()
    tester = LiveTester(daemon_url=DAEMON_URL, workspace=WORKSPACE)
    for tc in CASES:
        if tc.app_id in seen_apps:
            continue
        seen_apps.add(tc.app_id)
        print(f"  Deploying {tc.app_id}...")
        tester.ensure_deployed(tc.app_id, tc.app_yaml)
    print()

    # Run cases in parallel
    results: list[TestOutcome] = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = {pool.submit(run_one, tc): tc for tc in CASES}
        for fut in as_completed(futs):
            tc = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = TestOutcome(name=tc.name, passed=False, reason=f"Executor crash: {e}")
            results.append(r)
            status = "PASS" if r.passed else "FAIL"
            print(f"  [{status}] {r.name:50s} ({r.duration:.1f}s) tools={r.tools_called}")
            if not r.passed:
                for bug in r.bugs_found[:2]:
                    print(f"         → {bug[:180]}")

    total_t = time.monotonic() - t0

    # Summary per module
    print(f"\n{'═' * 70}")
    print(f"  SUMMARY")
    print(f"{'═' * 70}")
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    print(f"  Total: {passed}/{len(results)} passed, {failed} failed")
    print(f"  Wall time: {total_t:.0f}s (serial would be ~{sum(r.duration for r in results):.0f}s)")
    print(f"  Speedup: {sum(r.duration for r in results) / total_t:.1f}x")

    # By module
    modules: dict[str, list[TestOutcome]] = {}
    for r in results:
        mod = r.name.split("/")[0]
        modules.setdefault(mod, []).append(r)
    print()
    for mod, outs in modules.items():
        p = sum(1 for o in outs if o.passed)
        print(f"  {mod}: {p}/{len(outs)}")

    # Failed tests with details
    if failed:
        print(f"\n{'═' * 70}")
        print(f"  FAILED TESTS")
        print(f"{'═' * 70}")
        for r in results:
            if r.passed:
                continue
            print(f"\n  [{r.name}]")
            print(f"    Tools called: {r.tools_called}")
            for bug in r.bugs_found:
                print(f"    - {bug[:250]}")
            if r.tool_results_preview:
                print(f"    Result preview: {r.tool_results_preview[0][:200]}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    concurrency = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    sys.exit(main(concurrency))
