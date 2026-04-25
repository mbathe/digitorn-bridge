"""Production-like tests — realistic user tasks, observe what breaks.

Each task is written as a real user would phrase it. We record:
  - Did the agent complete the task correctly?
  - What tool calls did it make?
  - What errors occurred (tool errors, daemon errors, logical errors)?
  - Time taken
"""
from __future__ import annotations

import io
import json
import sys
import time
import traceback
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT / "packages"))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from digitorn.testing.client import DevClient

import os as _os

WORKSPACE = Path(__file__).parent / "workspace"
# Default to local Ollama config (no API cost). Pass PROD_APP=cloud to use
# the DeepSeek variant when API credit is available.
_APP_CHOICE = _os.environ.get("PROD_APP", "local").lower()
APP_YAML = Path(__file__).parent / (
    "coding-assistant-local.yaml" if _APP_CHOICE == "local" else "coding-assistant.yaml"
)
_APP_ID = "prod-coding-assistant-local" if _APP_CHOICE == "local" else "prod-coding-assistant"


ORIGINAL_FILES = {}


def snapshot_workspace():
    """Save all file contents for reset between tests."""
    ORIGINAL_FILES.clear()
    for p in WORKSPACE.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            ORIGINAL_FILES[p] = p.read_bytes()


def reset_workspace():
    """Restore files to pre-test state."""
    for p, data in ORIGINAL_FILES.items():
        p.write_bytes(data)
    # Remove any files created during the test
    for p in WORKSPACE.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts and p not in ORIGINAL_FILES:
            try:
                p.unlink()
            except Exception:
                pass


# ── Test definitions ────────────────────────────────────────

TESTS = [
    {
        "name": "T1: Fix divide-by-zero bug",
        "message": (
            "The divide function in src/calculator.py crashes when the second arg is 0. "
            "Fix it so it returns None instead of crashing."
        ),
        "check": lambda: (
            "return None" in (WORKSPACE / "src/calculator.py").read_text(encoding="utf-8")
            and "if b == 0" in (WORKSPACE / "src/calculator.py").read_text(encoding="utf-8").replace(" ", "")
            or "b==0" in (WORKSPACE / "src/calculator.py").read_text(encoding="utf-8").replace(" ", "")
        ),
    },
    {
        "name": "T2: Fix the power() bug (off-by-one)",
        "message": (
            "The power function in src/calculator.py has a bug. power(2, 3) returns 4 instead of 8. "
            "Fix the off-by-one error in the loop."
        ),
        "check": lambda: _check_power_works(),
    },
    {
        "name": "T3: Add a missing test (divide by zero)",
        "message": (
            "There is no test for divide by zero. Add a test in tests/test_calculator.py "
            "that verifies divide(5, 0) returns None."
        ),
        "check": lambda: (
            "divide(5, 0)" in (WORKSPACE / "tests/test_calculator.py").read_text(encoding="utf-8")
            or "divide(10, 0)" in (WORKSPACE / "tests/test_calculator.py").read_text(encoding="utf-8")
        ),
    },
    {
        "name": "T4: Find where 'factorial' is defined",
        "message": (
            "Where is the factorial function defined in this project? "
            "Give me the file path and line number."
        ),
        # Soft check: did the agent find calculator.py
        "check": lambda: True,  # manual text check in eval
    },
    {
        "name": "T5: Add a new function modulo(a, b)",
        "message": (
            "Add a new function `modulo(a, b)` in src/calculator.py that returns a % b. "
            "Handle the case where b is 0 by returning None (no crash)."
        ),
        "check": lambda: "def modulo" in (WORKSPACE / "src/calculator.py").read_text(encoding="utf-8"),
    },
    {
        "name": "T6: Count lines of code",
        "message": (
            "How many lines of Python code are in src/? "
            "Give me the total across all .py files."
        ),
        "check": lambda: True,  # soft
    },
]


def _check_power_works():
    """Run the fixed calculator and verify power(2, 3) == 8."""
    import subprocess
    code = (
        f"import sys; sys.path.insert(0, r'{WORKSPACE}'); "
        "from src.calculator import power; "
        "print(power(2, 3), power(5, 2))"
    )
    try:
        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=5,
            cwd=str(WORKSPACE),
        )
        out = r.stdout.strip()
        return out == "8 25"
    except Exception:
        return False


# ── Runner ──────────────────────────────────────────────────


def run_test(test, client, idx):
    print(f"\n{'─' * 70}")
    print(f"  {test['name']}")
    print(f"  User: {test['message']}")
    print(f"{'─' * 70}")

    reset_workspace()
    session = client.create_session(
        _APP_ID,
        workspace=str(WORKSPACE),
    )

    t0 = time.monotonic()
    try:
        result = client.send(session, test["message"], timeout=300)
    except Exception as e:
        print(f"  [CRASH] DevClient failed: {type(e).__name__}: {e}")
        return {
            "name": test["name"], "passed": False, "reason": f"DevClient crash: {e}",
            "duration": time.monotonic() - t0, "tool_calls": [],
        }
    duration = time.monotonic() - t0

    history = client.get_history(session, include_system=True)

    tool_calls = []
    tool_errors = []
    for m in history:
        for tc in m.get("tool_calls", []):
            fn = tc.get("function", {})
            tool_calls.append((fn.get("name", "?"), str(fn.get("arguments", ""))[:100]))
        if m.get("role") == "tool":
            content = str(m.get("content", ""))
            if '"error"' in content or '"success": false' in content.lower():
                tool_errors.append(content[:200])

    print(f"\n  Duration: {duration:.0f}s")
    print(f"  Tool calls: {len(tool_calls)}")
    for name, args in tool_calls[:10]:
        print(f"    → {name}({args})")
    if len(tool_calls) > 10:
        print(f"    ... (+{len(tool_calls) - 10} more)")
    if tool_errors:
        print(f"  Tool errors: {len(tool_errors)}")
        for err in tool_errors[:3]:
            print(f"    ⚠ {err[:200]}")

    print(f"\n  Agent reply: {(result.text or '')[:400]}")

    # Run the check
    try:
        passed = bool(test["check"]())
    except Exception as e:
        passed = False
        print(f"  [check() crashed: {e}]")

    status = "PASS" if passed else "FAIL"
    print(f"\n  RESULT: [{status}]")

    return {
        "name": test["name"],
        "passed": passed,
        "duration": duration,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "agent_text": (result.text or "")[:500],
    }


def main():
    snapshot_workspace()
    print(f"Workspace: {WORKSPACE}")
    print(f"Workspace files: {len(ORIGINAL_FILES)}")
    print()

    client = DevClient(daemon_url="http://127.0.0.1:8000", auto_approve=True, timeout=300)
    print(f"Deploying {_APP_ID} from {APP_YAML.name}...")
    app = client.deploy(APP_YAML, force=True, wait=5)
    print(f"  status={app.status} tools={app.total_tools}")

    outcomes = []
    for i, t in enumerate(TESTS, 1):
        try:
            outcomes.append(run_test(t, client, i))
        except Exception as e:
            print(f"  RUNNER CRASH: {type(e).__name__}: {e}")
            traceback.print_exc()
            outcomes.append({
                "name": t["name"], "passed": False,
                "reason": f"runner crash: {e}", "duration": 0,
                "tool_calls": [], "tool_errors": [],
            })

    # Summary
    passed = sum(1 for o in outcomes if o["passed"])
    print(f"\n\n{'═' * 70}")
    print(f"  SUMMARY: {passed}/{len(outcomes)} tasks completed successfully")
    print(f"{'═' * 70}")
    for o in outcomes:
        status = "PASS" if o["passed"] else "FAIL"
        print(f"  [{status}] {o['name']}  ({o.get('duration', 0):.0f}s, {len(o['tool_calls'])} tools, {len(o.get('tool_errors', []))} errors)")

    # Restore workspace
    reset_workspace()

    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
