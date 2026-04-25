"""Verify the shell module injects the workspace into PYTHONPATH so that
`python -c "from src.foo import ..."` works from the workspace root even
when a parent pytest.ini would otherwise hijack sys.path.
"""
from __future__ import annotations
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages"))

from digitorn.testing.client import DevClient

APP_YAML = Path(__file__).parent / "coding-assistant-local.yaml"
WORKSPACE = Path(__file__).parent / "workspace"


def main() -> int:
    client = DevClient(daemon_url="http://127.0.0.1:8000", auto_approve=True, timeout=90)
    app = client.deploy(APP_YAML, force=True, wait=5)
    print(f"deployed: status={app.status}")

    session = client.create_session("prod-coding-assistant-local", workspace=str(WORKSPACE))

    msg = (
        "Run this bash command (and nothing else), then report exit_code and stdout verbatim: "
        "`python -c \"from src.calculator import divide; print('RESULT=', divide(10, 2))\"`"
    )
    print(f"\nuser: {msg}")
    r = client.send(session, msg, timeout=90)

    history = client.get_history(session, include_system=False)
    bash_results = []
    for m in history:
        if m.get("role") != "tool":
            continue
        content = m.get("content", "")
        try:
            data = json.loads(content) if isinstance(content, str) else content
        except Exception:
            data = {"raw": str(content)[:400]}
        if isinstance(data, dict) and ("stdout" in data or "exit_code" in data):
            bash_results.append(data)

    print(f"\nbash tool results captured: {len(bash_results)}")
    for i, d in enumerate(bash_results):
        print(f"  [{i}] exit_code={d.get('exit_code')!r} stdout={str(d.get('stdout'))[:200]!r}")
        if d.get("error"):
            print(f"      error={str(d.get('error'))[:200]!r}")

    ok = any(
        d.get("exit_code") == 0
        and "RESULT= 5" in str(d.get("stdout", ""))
        for d in bash_results
    )
    no_module = any(
        "No module named 'src'" in str(d.get("error", "")) + str(d.get("stdout", ""))
        for d in bash_results
    )
    if ok:
        print("\nPASS — workspace is on PYTHONPATH, `from src.calculator` works.")
        return 0
    if no_module:
        print("\nFAIL — ModuleNotFoundError: 'src' — PYTHONPATH fix not active.")
        return 1
    print("\nINCONCLUSIVE — agent didn't call bash with the expected command.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
