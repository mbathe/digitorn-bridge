"""Runner for the session_store live scenarios.

Reads the JWT from ~/.digitorn/test-auth.json and the store root from
DIGITORN_SESSION_STORE_ROOT (set by the runner before booting the
daemon). Executes each scenario and prints a PASS/FAIL summary.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from digitorn.testing import DevClient  # noqa: E402
from tools.live_tests.session_store_scenarios import (  # noqa: E402
    scenario_meta_json_written,
    scenario_session_store_writes_events,
)


def _load_test_token() -> str:
    p = Path.home() / ".digitorn" / "test-auth.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    return raw["access_token"]


def main() -> int:
    store_root = Path(
        os.environ.get(
            "DIGITORN_SESSION_STORE_ROOT",
            str(Path.home() / ".digitorn" / "sessions-test"),
        )
    )
    if not store_root.exists():
        print(
            f"FATAL: session_store_root does not exist: {store_root}\n"
            "Boot the daemon with DIGITORN_SESSION_STORE_MODE=primary "
            "before running this.",
            file=sys.stderr,
        )
        return 2

    token = _load_test_token()
    client = DevClient(token=token, auto_approve=True)
    app_id = os.environ.get("DIGITORN_TEST_APP_ID", "digitorn-chat")

    scenarios = [
        ("session_store_writes_events", scenario_session_store_writes_events),
        ("meta_json_written", scenario_meta_json_written),
    ]
    summary: list[tuple[str, bool, str]] = []
    for name, fn in scenarios:
        print(f"\n{'=' * 70}\nRunning: {name}\n{'=' * 70}")
        try:
            ok, detail, artifacts = fn(client, app_id, store_root)
        except Exception as exc:
            ok = False
            detail = f"scenario raised: {type(exc).__name__}: {exc}"
            artifacts = {}
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}\n{detail}")
        if artifacts:
            for k, v in artifacts.items():
                if isinstance(v, list) and len(v) > 8:
                    v = f"[{len(v)} items, head={v[:5]}]"
                print(f"  {k} = {v}")
        summary.append((name, ok, detail))

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    for name, ok, _detail in summary:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    failed = sum(1 for _, ok, _ in summary if not ok)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
