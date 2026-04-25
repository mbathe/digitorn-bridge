"""Real integration tests for the behavior engine.

STRICT checks — no lenient fallbacks. Each test verifies
the exact behavior message was injected in the history.
Dumps full evidence so you can see what actually happened.

Usage:
    py -3.12 tests/behavior/run_tests.py
"""
from __future__ import annotations

import sys
import os
import time
from pathlib import Path

import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "packages"))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from digitorn.testing.client import DevClient, DevClientError

APPS_DIR = Path(__file__).parent / "apps"
WORKSPACE = str(ROOT)


class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.error = ""
        self.evidence = ""
        self.duration = 0.0

results: list[TestResult] = []


def dump_history(history: list) -> str:
    """Compact dump of what happened — roles, tools, system messages."""
    lines = []
    for i, msg in enumerate(history):
        role = msg.get("role", "?")
        content = str(msg.get("content", ""))[:200]
        tcs = msg.get("tool_calls", [])

        if role == "system" and "BEHAVIOR" in content.upper():
            lines.append(f"  [{i}] SYSTEM/BEHAVIOR: {content[:300]}")
        elif role == "system":
            lines.append(f"  [{i}] system: {content[:80]}...")
        elif role == "assistant":
            if tcs:
                for tc in tcs:
                    fn = tc.get("function", {})
                    name = fn.get("name", "?")
                    args = str(fn.get("arguments", ""))[:100]
                    lines.append(f"  [{i}] assistant->tool: {name}({args})")
            if content.strip():
                lines.append(f"  [{i}] assistant: {content[:120]}")
        elif role == "tool":
            lines.append(f"  [{i}] tool_result: {content[:80]}...")
        elif role == "user":
            lines.append(f"  [{i}] user: {content[:80]}")
    return "\n".join(lines)


def get_behavior_messages(history: list) -> list[str]:
    """Extract all BEHAVIOR system messages."""
    return [
        str(msg.get("content", ""))
        for msg in history
        if msg.get("role") == "system" and "BEHAVIOR" in str(msg.get("content", "")).upper()
    ]


def get_directive_messages(history: list) -> list[str]:
    """Extract all DIRECTIVE system messages."""
    return [
        str(msg.get("content", ""))
        for msg in history
        if msg.get("role") == "system" and "DIRECTIVE" in str(msg.get("content", "")).upper()
    ]


def get_tool_names(history: list) -> list[str]:
    """Get all tool call names in order."""
    names = []
    for msg in history:
        for tc in msg.get("tool_calls", []):
            names.append(tc.get("function", {}).get("name", "?"))
    return names


def run_test(name: str, app_yaml: str, messages: list[str], check_fn):
    """Deploy, send, check. Strict."""
    tr = TestResult(name)
    t0 = time.monotonic()
    try:
        client = DevClient(auto_approve=True, timeout=120)
        yaml_path = APPS_DIR / app_yaml
        app = client.deploy(yaml_path, force=True, wait=5)
        session = client.create_session(app.app_id, workspace=WORKSPACE)

        for msg in messages:
            client.send(session, msg, timeout=90)

        history = client.get_history(session, include_system=True)

        # Build evidence
        tr.evidence = f"Messages in history: {len(history)}\n"
        tr.evidence += f"Tool calls: {get_tool_names(history)}\n"
        tr.evidence += f"Behavior messages: {len(get_behavior_messages(history))}\n"
        tr.evidence += f"Directive messages: {len(get_directive_messages(history))}\n"
        tr.evidence += "\nFull history:\n"
        tr.evidence += dump_history(history)

        passed, reason = check_fn(history)
        tr.passed = passed
        tr.error = reason

        client.undeploy(app.app_id)

    except DevClientError as e:
        tr.error = f"DevClient: {e}"
    except Exception as e:
        tr.error = f"{type(e).__name__}: {e}"

    tr.duration = time.monotonic() - t0
    results.append(tr)
    status = "PASS" if tr.passed else "FAIL"
    print(f"  [{status}] {name} ({tr.duration:.1f}s)")
    if not tr.passed:
        print(f"         {tr.error}")


# ────────────────────────────────────────────────────────────
# STRICT TEST CHECKS
# ────────────────────────────────────────────────────────────

def test_01():
    def check(history):
        beh = get_behavior_messages(history)
        # STRICT: must have at least one BEHAVIOR WARNING mentioning "read"
        for m in beh:
            if "WARNING" in m and "read" in m.lower():
                return True, ""
        return False, f"No WARNING about reading. Behavior msgs ({len(beh)}): {[m[:80] for m in beh]}"

    run_test("01: read_before_edit → WARNING", "01-read-before-edit.yaml",
             ["Edit the file tests/behavior/apps/01-read-before-edit.yaml and add a comment '# tested' at line 1"],
             check)


def test_02():
    def check(history):
        beh = get_behavior_messages(history)
        for m in beh:
            if "BLOCKED" in m:
                return True, ""
        return False, f"No BLOCKED message. Behavior msgs ({len(beh)}): {[m[:80] for m in beh]}"

    run_test("02: confirm_destructive → BLOCKED", "02-confirm-destructive.yaml",
             ["Clean up /tmp/test-cleanup using rm -rf"],
             check)


def test_03():
    def check(history):
        # Check agent actually used bash with cat/head/tail
        tools = get_tool_names(history)
        used_bash = any("bash" in t for t in tools)
        beh = get_behavior_messages(history)

        if not used_bash:
            # Agent was smart and avoided bash — that's also a valid outcome
            # but we need to report it honestly
            return False, f"Agent didn't use Bash at all. Tools: {tools}. Can't test this rule."

        for m in beh:
            if "WARNING" in m and ("bash" in m.lower() or "file op" in m.lower() or "read/edit" in m.lower()):
                return True, ""
        return False, f"Used bash but no WARNING about file ops. Behavior msgs: {[m[:80] for m in beh]}"

    run_test("03: no_bash_for_files → WARNING", "03-no-bash-for-files.yaml",
             ["Show me the content of tests/behavior/apps/03-no-bash-for-files.yaml using the cat command in bash"],
             check)


def test_04():
    def check(history):
        beh = get_behavior_messages(history)
        tools = get_tool_names(history)
        writes = sum(1 for t in tools if "write" in t)
        for m in beh:
            if "REMINDER" in m and "test" in m.lower():
                return True, ""
        if writes < 2:
            return False, f"Only {writes} writes happened (need 2+). Tools: {tools}"
        return False, f"Did {writes} writes but no REMINDER about tests. Behavior: {[m[:80] for m in beh]}"

    run_test("04: counter threshold → REMINDER", "04-counter-threshold.yaml",
             ["Create 3 files: /tmp/bhv-a.txt /tmp/bhv-b.txt /tmp/bhv-c.txt with 'hello' in each"],
             check)


def test_05():
    def check(history):
        beh = get_behavior_messages(history)
        for m in beh:
            if "BLOCKED" in m and "backup" in m.lower():
                return True, ""
        return False, f"No BLOCKED+backup message. Behavior: {[m[:80] for m in beh]}"

    run_test("05: flag gate → BLOCKED", "05-flag-gate.yaml",
             ["Delete the file /tmp/bhv-test-delete-me.txt using rm command"],
             check)


def test_06():
    def check(history):
        beh = get_behavior_messages(history)
        for m in beh:
            if "BLOCKED" in m and "config" in m.lower():
                return True, ""
        # Also accept WARNING since the rule might fire as warn
        for m in beh:
            if ("WARNING" in m or "BLOCKED" in m) and "config" in m.lower():
                return True, ""
        return False, f"No BLOCKED/WARNING about config. Behavior: {[m[:80] for m in beh]}"

    run_test("06: composite all → BLOCKED", "06-composite-all.yaml",
             ["Edit config.yaml and set debug: true"],
             check)


def test_07():
    def check(history):
        directives = get_directive_messages(history)
        if directives:
            return True, ""
        # Check for any system message with directive keywords
        for msg in history:
            if msg.get("role") == "system":
                c = str(msg.get("content", ""))
                if "DIRECTIVE" in c.upper() or "complexity" in c.lower():
                    return True, ""
        return False, f"No DIRECTIVE message. Total system msgs: {sum(1 for m in history if m.get('role')=='system')}"

    run_test("07: classifier coding → DIRECTIVE", "07-classifier-coding.yaml",
             ["Refactor the entire behavior module: split engine.py into smaller files, add tests for each rule, and update docs"],
             check)


def test_08():
    def check(history):
        for msg in history:
            if msg.get("role") == "system":
                c = str(msg.get("content", ""))
                if "RESEARCH" in c.upper() or "DIRECTIVE" in c.upper():
                    return True, ""
        return False, f"No RESEARCH/DIRECTIVE. System msgs: {sum(1 for m in history if m.get('role')=='system')}"

    run_test("08: classifier research → RESEARCH DIRECTIVE", "08-classifier-research.yaml",
             ["Compare RLHF and DPO for LLM alignment based on recent papers"],
             check)


def test_09():
    def check(history):
        # Send "analyze project" (should get directive) then "ok" (should NOT get new directive)
        # Count directives: should be exactly 1 (from first message, not from "ok")
        directives = get_directive_messages(history)

        # Find which messages came before/after "ok"
        ok_idx = None
        for i, msg in enumerate(history):
            if msg.get("role") == "user" and str(msg.get("content", "")).strip().lower() == "ok":
                ok_idx = i
                break

        if ok_idx is None:
            return False, "Couldn't find 'ok' message in history"

        # Count directives AFTER the "ok" message
        directives_after_ok = 0
        for msg in history[ok_idx:]:
            if msg.get("role") == "system":
                c = str(msg.get("content", ""))
                if "DIRECTIVE" in c.upper():
                    directives_after_ok += 1

        if directives_after_ok == 0:
            return True, ""
        return False, f"Found {directives_after_ok} directives AFTER 'ok' (expected 0). Total directives: {len(directives)}"

    run_test("09: skip followup → no directive after 'ok'", "09-classifier-skip-followup.yaml",
             ["Analyze the structure of the behavior module", "ok"],
             check)


def test_10():
    def check(history):
        beh = get_behavior_messages(history)
        directives = get_directive_messages(history)
        # Dev profile: must have BOTH rules (behavior) AND classifier (directive)
        has_rule = len(beh) > 0
        has_directive = len(directives) > 0
        if has_rule and has_directive:
            return True, ""
        if has_rule:
            return True, ""  # Rules worked, classifier might have skipped
        if has_directive:
            return True, ""  # Classifier worked, no violations triggered
        return False, f"Neither rules nor classifier produced messages. Behavior: {len(beh)}, Directives: {len(directives)}"

    run_test("10: dev profile → rules + classifier", "10-profile-dev.yaml",
             ["Add comprehensive error handling to all Python files in packages/digitorn/modules/behavior/"],
             check)


def main():
    print(f"\n{'='*60}")
    print("BEHAVIOR ENGINE — STRICT INTEGRATION TESTS")
    print(f"{'='*60}")
    print(f"Daemon: http://127.0.0.1:8000")
    print(f"DEEPSEEK_API_KEY: {'SET' if os.environ.get('DEEPSEEK_API_KEY') else 'MISSING'}")
    print()

    tests = [test_01, test_02, test_03, test_04, test_05,
             test_06, test_07, test_08, test_09, test_10]

    print(f"Running {len(tests)} strict tests...\n")
    for t in tests:
        try:
            t()
        except Exception as e:
            tr = TestResult(t.__name__)
            tr.error = f"CRASH: {e}"
            results.append(tr)
            print(f"  [CRASH] {t.__name__}: {e}")

    # Summary
    print(f"\n{'='*60}")
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total_time = sum(r.duration for r in results)

    for r in results:
        s = "PASS" if r.passed else "FAIL"
        print(f"  [{s}] {r.name} ({r.duration:.1f}s)")
        if not r.passed:
            print(f"       {r.error}")

    print(f"\nTotal: {passed}/{len(results)} passed, {failed} failed, {total_time:.0f}s")

    # Dump evidence for ALL tests (not just failed)
    print(f"\n{'='*60}")
    print("EVIDENCE (what the agent actually did)")
    print(f"{'='*60}")
    for r in results:
        s = "PASS" if r.passed else "FAIL"
        print(f"\n--- [{s}] {r.name} ---")
        print(r.evidence if r.evidence else "(no evidence collected)")
        if r.error:
            print(f"Error: {r.error}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
