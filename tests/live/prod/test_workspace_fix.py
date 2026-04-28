"""Verify {WORKSPACE} placeholder is substituted in agent system prompt.

Deploys digitorn-code-local, sends a trivial question that makes the agent
mention its workspace, and asserts:
  - Agent reply does NOT contain the literal "{WORKSPACE}"
  - Persisted system message DOES contain the resolved path
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages"))

from digitorn.testing.client import DevClient

APP_YAML = Path(__file__).parent / "digitorn-code-local.yaml"
WORKSPACE = Path(__file__).parent / "workspace"


def main() -> int:
    client = DevClient(daemon_url="http://127.0.0.1:8000", auto_approve=True, timeout=180)
    print("Deploying digitorn-code-local...")
    app = client.deploy(APP_YAML, force=True, wait=5)
    print(f"  status={app.status} tools={app.total_tools}")

    session = client.create_session("digitorn-code-local", workspace=str(WORKSPACE))
    print(f"  session_id={session.session_id}")
    print(f"  workspace={WORKSPACE}")

    msg = "Quel est ton workspace actuel ? Reponds en une phrase."
    print(f"\nUser: {msg}")
    result = client.send(session, msg, timeout=180)
    reply = (result.text or "").strip()
    print(f"\nAgent reply:\n{reply}\n")

    history = client.get_history(session, include_system=True)
    sysmsg = next((m for m in history if m.get("role") == "system"), None)
    sys_content = (sysmsg or {}).get("content", "") or ""

    fail = []
    if "{WORKSPACE}" in reply:
        fail.append("Agent reply contains literal {WORKSPACE}")
    if "{WORKSPACE}" in sys_content:
        fail.append("System prompt still contains literal {WORKSPACE}")
    if str(WORKSPACE).replace("\\", "/") not in sys_content.replace("\\", "/") and \
       str(WORKSPACE) not in sys_content:
        fail.append(f"System prompt is missing the resolved workspace path: {WORKSPACE}")

    if fail:
        print("FAIL:")
        for f in fail:
            print(f"  - {f}")
        print(f"\nSystem prompt head:\n{sys_content[:500]}")
        return 1
    print("PASS - {WORKSPACE} correctly substituted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
