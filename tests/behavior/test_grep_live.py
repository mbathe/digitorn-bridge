"""Live test of Grep tool via DevClient.

Asks an agent to Grep for specific text in the workspace, then inspects
the actual tool call and the tool result to see if Grep works end-to-end.
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "packages"))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from digitorn.testing.client import DevClient

# Use a simple coding app that has filesystem access
APP_ID = "bhv-test-01"   # it has filesystem.grep granted
WORKSPACE = str(ROOT)


def main():
    client = DevClient(auto_approve=True, timeout=120)

    # Check app is deployed
    apps = client.list_apps()
    app_ids = {a.get("app_id") for a in apps}
    if APP_ID not in app_ids:
        print(f"App {APP_ID} not deployed. Deploying...")
        yaml_path = ROOT / "tests" / "behavior" / "apps" / "01-read-before-edit.yaml"
        client.deploy(yaml_path, force=True, wait=5)

    session = client.create_session(APP_ID, workspace=WORKSPACE)
    msg = (
        "Use the Grep tool to search for 'validate_behavior_config' "
        "in the packages/ directory. Return the file paths and line numbers."
    )
    print(f"Sending: {msg}\n")
    result = client.send(session, msg, timeout=90)
    print(f"success={result.success} error={result.error}")
    print(f"text={(result.text or '')[:300]}")
    print(f"duration={result.duration_seconds:.1f}s")
    print()

    history = client.get_history(session, include_system=True)
    print(f"History: {len(history)} messages\n")

    for i, m in enumerate(history):
        role = m.get("role", "?")
        content = str(m.get("content", ""))
        tcs = m.get("tool_calls", [])
        if role == "system" and "BEHAVIOR" in content.upper():
            print(f"  [{i}] BEHAVIOR: {content[:180]}")
        elif role == "user":
            print(f"  [{i}] user: {content[:100]}")
        elif role == "assistant":
            if content.strip():
                print(f"  [{i}] assistant: {content[:200]}")
            for tc in tcs:
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                args = str(fn.get("arguments", ""))[:300]
                print(f"  [{i}]   -> TOOL_CALL: {name}({args})")
        elif role == "tool":
            # Show Grep result in detail
            print(f"  [{i}] TOOL_RESULT: {content[:500]}")
        elif role == "system":
            print(f"  [{i}] system: {content[:80]}...")


if __name__ == "__main__":
    main()
