"""Repro T5 bash failure in isolation to see what cwd the shell module uses."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages"))

from digitorn.testing.client import DevClient

APP_YAML = Path(__file__).parent / "digitorn-code-local.yaml"
WORKSPACE = Path(__file__).parent / "workspace"


def main() -> int:
    client = DevClient(daemon_url="http://127.0.0.1:8000", auto_approve=True, timeout=120)
    app = client.deploy(APP_YAML, force=True, wait=5)
    print(f"deployed: status={app.status} tools={app.total_tools}")

    session = client.create_session("digitorn-code-local", workspace=str(WORKSPACE))
    print(f"workspace: {WORKSPACE}")
    print(f"workspace has src/: {(WORKSPACE / 'src').is_dir()}")

    msg = (
        "Run this exact bash command and show me the output: "
        "`pwd && ls && cd src && pwd`"
    )
    print(f"\nuser: {msg}")
    r = client.send(session, msg, timeout=120)
    print(f"\nreply: {(r.text or '').strip()[:800]}\n")

    history = client.get_history(session, include_system=False)
    for m in history:
        for tc in m.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            print(f"  → {fn.get('name')}({str(fn.get('arguments'))[:200]})")
        if m.get("role") == "tool":
            content = str(m.get("content", ""))
            print(f"  tool result: {content[:500]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
