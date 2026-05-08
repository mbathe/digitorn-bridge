"""Runner for gateway health live scenarios."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from tools.live_tests.gateway_health_scenarios import (  # noqa: E402
    scenario_daemon_proxy_forwards_to_gateway,
    scenario_gateway_metrics_endpoint,
)


def _load_test_token() -> str:
    p = Path.home() / ".digitorn" / "test-auth.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    return raw["access_token"]


def main() -> int:
    daemon = os.environ.get("DIGITORN_DAEMON_URL", "http://127.0.0.1:8000")
    gateway = os.environ.get("DIGITORN_GATEWAY_URL", "http://127.0.0.1:8002")
    token = _load_test_token()

    scenarios = [
        ("gateway_metrics_endpoint", lambda: scenario_gateway_metrics_endpoint(
            daemon, gateway,
        )),
        ("daemon_proxy_forwards", lambda: scenario_daemon_proxy_forwards_to_gateway(
            daemon, token,
        )),
    ]
    summary: list[tuple[str, bool, str]] = []
    for name, fn in scenarios:
        print(f"\n{'=' * 70}\nRunning: {name}\n{'=' * 70}")
        try:
            ok, detail, artifacts = fn()
        except Exception as exc:
            ok = False
            detail = f"scenario raised: {type(exc).__name__}: {exc}"
            artifacts = {}
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}\n{detail}")
        for k, v in artifacts.items():
            if isinstance(v, str) and len(v) > 100:
                v = v[:100] + "..."
            print(f"  {k} = {v}")
        summary.append((name, ok, detail))

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    for name, ok, _detail in summary:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    failed = sum(1 for _, ok, _ in summary if not ok)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
