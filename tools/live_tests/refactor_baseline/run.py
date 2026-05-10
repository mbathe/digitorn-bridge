"""CLI runner for the SessionStore-unification refactor baseline.

Usage::

    # Default: attach to the operator's running daemon (8000).
    py -3 -m tools.live_tests.refactor_baseline.run

    # Or explicitly:
    py -3 -m tools.live_tests.refactor_baseline.run --external http://127.0.0.1:8000

    # Or spawn an isolated daemon (heavy boot, see harness):
    py -3 -m tools.live_tests.refactor_baseline.run --spawn

The default is "external" mode -- attach to a running daemon the
operator launched with the right env. This avoids the multi-minute
boot of a fresh test daemon (HF model downloads, builtin auto-deploy,
etc.) and tests against the same configuration the operator runs.

Exit code: 0 if all PASS, else 1.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Callable

from digitorn.testing import DevClient

from .harness import (
    LatencyTimer,
    attach_external_daemon,
    list_deployed_apps,
    load_test_jwt,
    mint_test_jwt,
    spawn_test_daemon,
    wait_for_app_deployed,
    write_test_app_yaml,
)
from .scenarios import (
    scenario_abort_mid_turn_recovery,
    scenario_append_event_latency_budget,
    scenario_bg_snapshot_worker_active,
    scenario_browser_chat_round_trip,
    scenario_cold_reload_history_fidelity,
    scenario_compaction_mid_chat,
    scenario_concurrent_open_idempotence,
    scenario_concurrent_seq_invariant,
    scenario_direct_events_jsonl,
    scenario_endpoint_events_filters,
    scenario_endpoint_history_backward,
    scenario_endpoint_history_pagination,
    scenario_endpoint_history_payload,
    scenario_eviction_under_pressure,
    scenario_phase4_sustained_load,
    scenario_mega_session_load,
    scenario_multi_session_fanout,
    scenario_restart_seq_continuity,
    scenario_session_index_integrity,
    scenario_smoke_boot_and_auth,
    scenario_snapshot_reload_fast,
    scenario_sub_agent_spawn_wait_result,
    scenario_throughput_no_loss,
)
from .phase2_scenarios import (
    scenario_delete_removes_session,
    scenario_file_job_store,
    scenario_get_any_owner,
    scenario_list_for_app,
    scenario_recover_orphans,
    scenario_session_lock_isolation,
)


SCENARIOS: list[tuple[str, Callable]] = [
    # ── Phase 0 baseline (HTTP, against the operator's daemon) ───────
    ("smoke_boot_and_auth", scenario_smoke_boot_and_auth),
    ("direct_events_jsonl", scenario_direct_events_jsonl),
    ("concurrent_seq_invariant", scenario_concurrent_seq_invariant),
    ("compaction_mid_chat", scenario_compaction_mid_chat),
    ("sub_agent_spawn_wait_result", scenario_sub_agent_spawn_wait_result),
    ("abort_mid_turn_recovery", scenario_abort_mid_turn_recovery),
    ("restart_seq_continuity", scenario_restart_seq_continuity),
    ("eviction_under_pressure", scenario_eviction_under_pressure),
    ("throughput_no_loss", scenario_throughput_no_loss),
    ("snapshot_reload_fast", scenario_snapshot_reload_fast),
    ("session_index_integrity", scenario_session_index_integrity),
    ("browser_chat_round_trip", scenario_browser_chat_round_trip),
    ("append_event_latency_budget", scenario_append_event_latency_budget),
    # ── Phase 6 stress: ultra-loin load tests ────────────────────────
    ("mega_session_load", scenario_mega_session_load),
    ("multi_session_fanout", scenario_multi_session_fanout),
    ("concurrent_open_idempotence", scenario_concurrent_open_idempotence),
    ("bg_snapshot_worker_active", scenario_bg_snapshot_worker_active),
    # ── Phase 4b: HTTP endpoint migration (history_view) ────────────
    ("endpoint_history_payload", scenario_endpoint_history_payload),
    ("endpoint_history_pagination", scenario_endpoint_history_pagination),
    ("endpoint_history_backward", scenario_endpoint_history_backward),
    ("endpoint_events_filters", scenario_endpoint_events_filters),
    ("cold_reload_history_fidelity", scenario_cold_reload_history_fidelity),
    ("phase4_sustained_load", scenario_phase4_sustained_load),
    # ── Phase 2 in-process (new SessionStore primitives) ─────────────
    ("p2_session_lock_isolation", scenario_session_lock_isolation),
    ("p2_delete_removes_session", scenario_delete_removes_session),
    ("p2_list_for_app", scenario_list_for_app),
    ("p2_get_any_owner", scenario_get_any_owner),
    ("p2_recover_orphans", scenario_recover_orphans),
    ("p2_file_job_store", scenario_file_job_store),
]


def _separator(title: str) -> None:
    print(f"\n{'=' * 72}")
    print(title)
    print('=' * 72)


def _run_scenarios(daemon, client, timer) -> tuple[int, int]:
    results: list[tuple[str, bool, str, dict]] = []
    for name, fn in SCENARIOS:
        _separator(f"Running: {name}")
        t_run = time.monotonic()
        try:
            ok, detail, artifacts = fn(daemon, client, timer)
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = f"scenario raised: {type(exc).__name__}: {exc}"
            artifacts = {}
        wall = time.monotonic() - t_run
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}  ({wall:.1f}s)")
        print(f"  {detail}")
        for k, v in artifacts.items():
            rendered = v
            if isinstance(v, list) and len(v) > 6:
                rendered = f"[{len(v)} items, head={v[:3]}...]"
            if isinstance(v, str) and len(v) > 200:
                rendered = v[:200] + "..."
            print(f"  {k} = {rendered}")
        results.append((name, ok, detail, artifacts))

    _separator("LATENCY")
    print(timer.report() or "(no samples)")

    _separator("SUMMARY")
    for name, ok, _detail, _ in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    failed = sum(1 for _, ok, *_ in results if not ok)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return len(results), failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--external", default="http://127.0.0.1:8000",
        help="Base URL of an external daemon to attach to (default).",
    )
    parser.add_argument(
        "--spawn", action="store_true",
        help="Spawn an isolated test daemon instead of attaching.",
    )
    parser.add_argument(
        "--app-id", default="baseline-chat",
        help="App id to use for chat scenarios. Spawn mode deploys it; "
             "external mode expects it to be deployed already (or will "
             "deploy it via DevClient if missing).",
    )
    args = parser.parse_args()

    timer = LatencyTimer()
    # External mode: reuse the operator's existing test JWT so issuer
    # claims match the daemon's accept_issuers config without us having
    # to know it. Spawn mode mints a fresh one (we control the spawned
    # daemon's accept_issuers via env vars).
    if args.spawn:
        token = mint_test_jwt(name="baseline", roles=["admin", "developer"])
        print(f"[harness] minted admin JWT (len={len(token)})")
    else:
        token = load_test_jwt()
        print(f"[harness] loaded test JWT from ~/.digitorn/test-auth.json "
              f"(len={len(token)})")

    if args.spawn:
        print("[harness] spawning isolated daemon (mode=primary, sqlite local)...")
        t0 = time.monotonic()
        with spawn_test_daemon(gateway_jwt=token) as daemon:
            boot_s = time.monotonic() - t0
            print(f"[harness] daemon up on {daemon.base_url} (boot {boot_s:.1f}s)")
            print(f"[harness] sessions root: {daemon.sessions_root}")
            print(f"[harness] sqlite db:     {daemon.db_url}")

            client = DevClient(
                token=token, daemon_url=daemon.base_url, auto_approve=True,
            )
            print("[harness] writing + deploying baseline-chat test app...")
            yaml_path = write_test_app_yaml(
                daemon.tmpdir,
                app_id=args.app_id,
                gateway_url="http://127.0.0.1:8202",
                gateway_jwt=token,
                model_alias="lb-test",
            )
            try:
                client.deploy(yaml_path, force=True, wait=5.0)
            except Exception as exc:
                print(f"[harness] FATAL deploy failed: "
                      f"{type(exc).__name__}: {exc}")
                return 2
            if not wait_for_app_deployed(
                daemon, token, args.app_id, timeout=10,
            ):
                print(f"[harness] FATAL: {args.app_id} not visible after deploy")
                return 2

            _, failed = _run_scenarios(daemon, client, timer)
        return 0 if failed == 0 else 1

    # External mode (default)
    print(f"[harness] attaching to external daemon at {args.external}")
    with attach_external_daemon(base_url=args.external) as daemon:
        print(f"[harness] daemon healthy on {daemon.base_url}")
        print(f"[harness] sessions root: {daemon.sessions_root}")

        client = DevClient(
            token=token, daemon_url=daemon.base_url, auto_approve=True,
        )

        # If the requested app isn't deployed yet for the test user,
        # deploy it on the fly so scenarios can chat. The external
        # daemon stays up after the test ends; this is idempotent.
        if args.app_id not in list_deployed_apps(daemon, token):
            print(f"[harness] {args.app_id} not deployed, deploying on the fly...")
            import tempfile
            from pathlib import Path
            tmp = Path(tempfile.mkdtemp(prefix="digitorn-baseline-"))
            yaml_path = write_test_app_yaml(
                tmp,
                app_id=args.app_id,
                gateway_url="http://127.0.0.1:8202",
                gateway_jwt=token,
                model_alias="lb-test",
            )
            try:
                client.deploy(yaml_path, force=True, wait=5.0)
            except Exception as exc:
                print(f"[harness] FATAL deploy failed: "
                      f"{type(exc).__name__}: {exc}")
                return 2
            if not wait_for_app_deployed(
                daemon, token, args.app_id, timeout=10,
            ):
                print(f"[harness] FATAL: {args.app_id} not visible after deploy")
                return 2
            print(f"[harness] {args.app_id} deployed")
        else:
            print(f"[harness] {args.app_id} already deployed")

        _, failed = _run_scenarios(daemon, client, timer)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
