from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from digitorn.testing import DevClient  # noqa: E402
from tools.live_tests.tracker_streaming_scenarios import (  # noqa: E402
    scenario_live_stream_intact_after_jsonfile,
)


def main() -> int:
    runs_root = Path.home() / ".digitorn" / "runs" / "jsonl"
    token = json.loads(
        (Path.home() / ".digitorn" / "test-auth.json").read_text(encoding="utf-8")
    )["access_token"]
    client = DevClient(token=token, auto_approve=True)

    print(f"\n{'=' * 70}")
    ok, detail, artifacts = scenario_live_stream_intact_after_jsonfile(
        client, "digitorn-chat", runs_root,
    )
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] live_stream_intact_after_jsonfile\n{detail}")
    for k, v in artifacts.items():
        if isinstance(v, list) and len(v) > 8:
            v = f"[{len(v)} items, head={v[:5]}]"
        print(f"  {k} = {v}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
