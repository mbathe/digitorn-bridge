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

from tools.live_tests.refactor_baseline.harness import mint_test_jwt, wait_for_app_deployed
from .harness import spawn_ollama_daemon, write_ollama_test_app, health_loop_stalls
from .scenarios import (
    scenario_abort_mid_turn,
    scenario_concurrent_sessions,
    scenario_multi_turn_context,
    scenario_sequential_stress,
    scenario_single_turn,
)


SCENARIOS = [
    ("single_turn", scenario_single_turn),
    ("multi_turn_context", scenario_multi_turn_context),
    ("abort_mid_turn", scenario_abort_mid_turn),
    ("concurrent_sessions", scenario_concurrent_sessions),
    ("sequential_stress", scenario_sequential_stress),
]


def _separator(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(title)
    print('=' * 72)


def main() -> int:
    token = mint_test_jwt(name="stab", roles=["admin", "developer"])
    print(f"[stab] minted JWT (len={len(token)})")

    print("[stab] spawning isolated daemon with Ollama backend...")
    t0 = time.monotonic()
    with spawn_ollama_daemon(gateway_jwt=token) as daemon:
        print(f"[stab] daemon up on {daemon.base_url} (boot {time.monotonic() - t0:.1f}s)")
        print(f"[stab] sessions root: {daemon.sessions_root}")

        client = DevClient(token=token, daemon_url=daemon.base_url, auto_approve=True)

        print("[stab] writing + deploying stab-chat app...")
        yaml_path = write_ollama_test_app(daemon.tmpdir, app_id="stab-chat")
        try:
            client.deploy(yaml_path, force=True, wait=10.0)
        except Exception as exc:
            print(f"[stab] FATAL deploy failed: {type(exc).__name__}: {exc}")
            return 2
        if not wait_for_app_deployed(daemon, token, "stab-chat", timeout=15):
            print(f"[stab] FATAL: stab-chat not visible after deploy")
            return 2

        # Probe Ollama via the daemon to confirm it answers.
        print("[stab] initial loop-stall snapshot:")
        print(f"  {health_loop_stalls(daemon.base_url)}")

        results: list[tuple[str, bool, str, dict]] = []
        for name, fn in SCENARIOS:
            _separator(f"Running: {name}")
            t_run = time.monotonic()
            try:
                ok, detail, artifacts = fn(client, "stab-chat", daemon.base_url)
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
