"""Run the real-LLM stabilization battery against a spawn-isolated
daemon connected to local Ollama.

Usage:
    py -3.12 -m tools.live_tests.stab.run

Exit code 0 if all PASS, else 1.
"""
from __future__ import annotations

import sys
import time
import traceback

from digitorn.testing import DevClient

from tools.live_tests.refactor_baseline.harness import (
    attach_external_daemon, load_test_jwt, mint_test_jwt, wait_for_app_deployed,
)
from .harness import spawn_ollama_daemon, write_ollama_test_app, health_loop_stalls
from .scenarios import (
    scenario_abort_mid_turn,
    scenario_concurrent_sessions,
    scenario_delete_session_cleanup,
    scenario_history_reconstruction,
    scenario_long_chat_full_persistence,
    scenario_manual_compaction,
    scenario_multi_turn_context,
    scenario_sequential_stress,
    scenario_single_turn,
    scenario_tool_execution,
)


SCENARIOS = [
    ("single_turn", scenario_single_turn),
    ("multi_turn_context", scenario_multi_turn_context),
    ("abort_mid_turn", scenario_abort_mid_turn),
    ("concurrent_sessions", scenario_concurrent_sessions),
    ("tool_execution", scenario_tool_execution),
    ("manual_compaction", scenario_manual_compaction),
    ("history_reconstruction", scenario_history_reconstruction),
    ("delete_session_cleanup", scenario_delete_session_cleanup),
    ("sequential_stress", scenario_sequential_stress),
    ("long_chat_full_persistence", scenario_long_chat_full_persistence),
]


def _separator(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(title)
    print('=' * 72)


def main() -> int:
    # Attach to the operator's running daemon (port 8000) instead of
    # spawning a fresh one. That daemon is already wired to the live
    # ``digitorn-gateway`` with real model aliases + credentials -- the
    # spawn-test path doesn't have those and would always fail at the
    # LLM hop.
    daemon_url = "http://127.0.0.1:8000"
    app_id = "digitorn-chat"  # running, has memory tools, real LLM via gw

    cloud_jwt = load_test_jwt()
    print(f"[stab] attaching to {daemon_url}, app={app_id}")
    print(f"[stab] cloud JWT (len={len(cloud_jwt)})")

    with attach_external_daemon(base_url=daemon_url) as daemon:
        print(f"[stab] daemon healthy on {daemon.base_url}")

        client = DevClient(token=cloud_jwt, daemon_url=daemon.base_url, auto_approve=True)

        results: list[tuple[str, bool, str, dict]] = []
        for name, fn in SCENARIOS:
            _separator(f"Running: {name}")
            t_run = time.monotonic()
            try:
                ok, detail, artifacts = fn(client, app_id, daemon.base_url)
            except Exception as exc:  # noqa: BLE001
                ok = False
                detail = f"scenario raised: {type(exc).__name__}: {exc}"
                artifacts = {"traceback": traceback.format_exc()}
            wall = time.monotonic() - t_run
            status = "PASS" if ok else "FAIL"
            print(f"[{status}] {name}  ({wall:.1f}s)")
            print(f"  {detail}")
            for k, v in artifacts.items():
                rendered = v
                if isinstance(v, list) and len(v) > 5:
                    rendered = f"[{len(v)} items, head={v[:3]}...]"
                if isinstance(v, str) and len(v) > 250:
                    rendered = v[:250] + "..."
                print(f"  {k} = {rendered}")
            results.append((name, ok, detail, artifacts))

        _separator("FINAL LOOP-STALL SNAPSHOT")
        print(f"  {health_loop_stalls(daemon.base_url)}")

        _separator("SUMMARY")
        for name, ok, _detail, _ in results:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        failed = sum(1 for _, ok, *_ in results if not ok)
        print(f"\n{len(results) - failed}/{len(results)} passed")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
