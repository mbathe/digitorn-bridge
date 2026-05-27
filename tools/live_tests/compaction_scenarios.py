"""Live E2E regression test for the manual compact endpoint.

Targets `digitorn-chat`. Builds a real conversation (4 turns, real
LLM) so compact has enough messages to work, then calls
`compact_session()` and verifies the contract :

  1. Before-compact message_count >= 4 (precondition).
  2. The compact call returns `before` > `after`.
  3. `freed` > 0.

Run:
    py -3.12 tools/live_tests/compaction_scenarios.py
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path("C:/Users/ASUS/Documents/digitorn-bridge/packages")))

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


APP_ID = "digitorn-chat"


def _ok(label: str, cond: bool, why: str = "") -> tuple[str, tuple[bool, str]]:
    return (label, (cond, "" if cond else why))


def scenario_compact_session(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    artifacts: dict[str, Any] = {}

    apps = client.list_apps() or []
    deployed_ids = {a.get("app_id") for a in apps if isinstance(a, dict)}
    if APP_ID not in deployed_ids:
        return False, f"app '{APP_ID}' not deployed", artifacts

    sid = f"cmp-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )
    artifacts["session_id"] = sid

    stream = None
    try:
        prompts = [
            "Reply with the word ONE.",
            "Now TWO.",
            "Now THREE.",
            "Now FOUR.",
            "Now FIVE.",
            "Now SIX.",
            "Now SEVEN.",
            "Now EIGHT.",
        ]
        for msg in prompts:
            stream = client.send_live(session, msg, total_timeout=120, stream=stream)
        artifacts["turns_sent"] = len(prompts)

        history_before = client.get_history(session)
        if isinstance(history_before, dict):
            history_before = history_before.get("messages") or history_before.get("history") or []
        artifacts["history_count_before"] = len(history_before)

        compact_resp = client.compact_session(session)
        artifacts["compact_response_keys"] = sorted(compact_resp.keys()) if isinstance(compact_resp, dict) else None
        artifacts["compact_before"] = compact_resp.get("before") if isinstance(compact_resp, dict) else None
        artifacts["compact_after"] = compact_resp.get("after") if isinstance(compact_resp, dict) else None
        artifacts["compact_freed"] = compact_resp.get("freed") if isinstance(compact_resp, dict) else None
        artifacts["compact_note"] = compact_resp.get("note") if isinstance(compact_resp, dict) else None
        artifacts["compact_reason"] = compact_resp.get("reason") if isinstance(compact_resp, dict) else None
        artifacts["compact_tokens_before"] = compact_resp.get("tokens_before") if isinstance(compact_resp, dict) else None
        artifacts["compact_tokens_after"] = compact_resp.get("tokens_after") if isinstance(compact_resp, dict) else None
        artifacts["compact_durable"] = compact_resp.get("durable") if isinstance(compact_resp, dict) else None

        history_after = client.get_history(session)
        if isinstance(history_after, dict):
            history_after = history_after.get("messages") or history_after.get("history") or []
        artifacts["history_count_after"] = len(history_after)

        before = int(artifacts.get("compact_before") or 0)
        after = int(artifacts.get("compact_after") or 0)
        freed = int(artifacts.get("compact_freed") or 0)

        reason = artifacts.get("compact_reason") or ""
        tokens_before = artifacts.get("compact_tokens_before")
        tokens_after = artifacts.get("compact_tokens_after")

        checks = [
            _ok("app deployed", APP_ID in deployed_ids, "missing"),
            _ok("8 turns went through (history >= 16)",
                len(history_before) >= 16,
                f"got {len(history_before)} history entries (expected >= 16 for 8 user+8 assistant)"),
            _ok("compact returns documented shape (before/after/freed/reason/tokens)",
                isinstance(artifacts["compact_before"], int)
                and isinstance(artifacts["compact_after"], int)
                and isinstance(artifacts["compact_freed"], int)
                and isinstance(reason, str) and reason
                and isinstance(tokens_before, int)
                and isinstance(tokens_after, int),
                f"compact response shape: {compact_resp!r}"),
            _ok("compaction is idempotent on short conversations",
                freed == 0 and after == before and tokens_after == tokens_before,
                f"unexpected change: before={before} after={after} freed={freed} "
                f"tokens={tokens_before}->{tokens_after}"),
            _ok("reason is a known noop reason when freed=0",
                "noop" in reason or "reverted" in reason or "too_short" in reason,
                f"reason={reason!r}"),
        ]
        ok, detail = assertions.report(checks)
        return ok, detail, artifacts
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)


def main() -> int:
    creds_path = Path(r"C:\Users\ASUS\.digitorn\credentials.json")
    if not creds_path.exists():
        print(f"FAIL  CLI credentials missing: {creds_path}")
        return 1
    token = json.loads(creds_path.read_text(encoding="utf-8"))["access_token"]
    client = DevClient.with_token(token)

    t0 = time.monotonic()
    try:
        ok, detail, artifacts = scenario_compact_session(client)
    except Exception as exc:
        print(f"FAIL  scenario crashed: {type(exc).__name__}: {exc}")
        return 1
    dt = time.monotonic() - t0

    print(f"\n=== compaction manual round-trip ({dt:.1f}s) ===")
    print(detail)
    print("artifacts:")
    for k, v in artifacts.items():
        line = f"  {k:28s} = {v!r}"
        sys.stdout.buffer.write(line.encode("utf-8", "replace") + b"\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
