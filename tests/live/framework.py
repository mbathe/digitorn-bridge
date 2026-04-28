"""Live test framework - module-by-module smoke tests with a real LLM.

Runs against a test daemon on port 8001 (not the user's main daemon on 8000).
Each test deploys an app, sends a real message, inspects tool calls and
tool results for correctness.

Usage:
    py -3.12 tests/live/framework.py <module>   # e.g. 'filesystem', 'shell', 'all'
"""
from __future__ import annotations

import io
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "packages"))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from digitorn.testing.client import DevClient

DAEMON_URL = "http://127.0.0.1:8001"
APPS_DIR = ROOT / "tests" / "live" / "apps"
WORKSPACE = str(ROOT)


@dataclass
class TestCase:
    name: str
    app_id: str
    app_yaml: str
    message: str
    expected_tools: list[str] = field(default_factory=list)  # tool names expected to be called
    expected_patterns: list[str] = field(default_factory=list)  # strings to find in tool results or text
    forbidden_errors: list[str] = field(default_factory=list)  # strings that mean failure in results
    must_succeed: bool = True  # at least one tool must return success
    # Optional post-assertions run AFTER the agent finishes. Takes the
    # final history, must return (passed: bool, reason: str).
    post_check: Any = None


@dataclass
class TestOutcome:
    name: str
    passed: bool
    reason: str = ""
    duration: float = 0.0
    tools_called: list[str] = field(default_factory=list)
    tool_results_preview: list[str] = field(default_factory=list)
    bugs_found: list[str] = field(default_factory=list)


class LiveTester:
    def __init__(self, daemon_url: str = DAEMON_URL, workspace: str = WORKSPACE):
        self.client = DevClient(daemon_url=daemon_url, auto_approve=True, timeout=180)
        self.workspace = workspace
        self.deployed_apps: set[str] = set()

    def ensure_deployed(self, app_id: str, yaml_name: str) -> bool:
        yaml_path = APPS_DIR / yaml_name
        if not yaml_path.exists():
            return False
        try:
            self.client.deploy(yaml_path, force=True, wait=3)
            self.deployed_apps.add(app_id)
            return True
        except Exception as e:
            print(f"    [DEPLOY FAIL] {app_id}: {e}")
            return False

    def run_case(self, tc: TestCase) -> TestOutcome:
        out = TestOutcome(name=tc.name, passed=False)
        t0 = time.monotonic()
        try:
            if tc.app_id not in self.deployed_apps:
                if not self.ensure_deployed(tc.app_id, tc.app_yaml):
                    out.reason = f"Failed to deploy {tc.app_id}"
                    out.duration = time.monotonic() - t0
                    return out

            session = self.client.create_session(tc.app_id, workspace=self.workspace)
            result = self.client.send(session, tc.message, timeout=150)
            history = self.client.get_history(session, include_system=True)

            # Extract tool calls and results
            tool_calls = []
            tool_results = []
            for m in history:
                for tc_ in m.get("tool_calls", []):
                    fn_name = tc_.get("function", {}).get("name", "")
                    fn_args = str(tc_.get("function", {}).get("arguments", ""))
                    tool_calls.append((fn_name, fn_args))
                if m.get("role") == "tool":
                    tool_results.append(str(m.get("content", ""))[:400])

            out.tools_called = [tc[0] for tc in tool_calls]
            out.tool_results_preview = tool_results[:5]

            # Check expectations
            bugs: list[str] = []

            # 1. Expected tools called
            for expected in tc.expected_tools:
                if not any(expected.lower() in t.lower() for t in out.tools_called):
                    bugs.append(f"Expected tool '{expected}' not called. Got: {out.tools_called}")

            # 2. At least one tool must succeed (if must_succeed)
            if tc.must_succeed and tool_results:
                any_success = False
                for tr in tool_results:
                    if '"error"' not in tr and '"success": false' not in tr.lower():
                        any_success = True
                        break
                if not any_success:
                    bugs.append(f"No tool call succeeded. All results had errors: {tool_results[:2]}")
            elif tc.must_succeed and not tool_results:
                bugs.append(f"No tool calls made at all. Agent text: {(result.text or '')[:200]}")

            # 3. Expected patterns in results or text
            all_content = " ".join(tool_results) + " " + (result.text or "")
            for pattern in tc.expected_patterns:
                if pattern.lower() not in all_content.lower():
                    bugs.append(f"Expected pattern {pattern!r} not found in results")

            # 4. Forbidden errors
            for forbidden in tc.forbidden_errors:
                for tr in tool_results:
                    if forbidden.lower() in tr.lower():
                        bugs.append(f"Forbidden error {forbidden!r} in tool result: {tr[:150]}")

            # 5. Custom post-check (e.g. verify file contents on disk)
            if tc.post_check is not None:
                try:
                    ok, why = tc.post_check(history, tool_results)
                    if not ok:
                        bugs.append(f"post_check: {why}")
                except Exception as e:
                    bugs.append(f"post_check crashed: {type(e).__name__}: {e}")

            out.bugs_found = bugs
            out.passed = len(bugs) == 0
            if not out.passed:
                out.reason = "; ".join(bugs[:3])

        except Exception as e:
            out.reason = f"CRASH: {type(e).__name__}: {e}"
            out.bugs_found = [out.reason]

        out.duration = time.monotonic() - t0
        return out

    def undeploy_all(self) -> None:
        for app_id in list(self.deployed_apps):
            try:
                self.client.undeploy(app_id)
            except Exception:
                pass
        self.deployed_apps.clear()


def print_outcomes(outcomes: list[TestOutcome], module_name: str) -> None:
    passed = sum(1 for o in outcomes if o.passed)
    failed = sum(1 for o in outcomes if not o.passed)
    total_t = sum(o.duration for o in outcomes)

    print(f"\n{'═' * 70}")
    print(f"  MODULE: {module_name.upper()}  -  {passed}/{len(outcomes)} passed  ({total_t:.0f}s)")
    print(f"{'═' * 70}")
    for o in outcomes:
        status = "PASS" if o.passed else "FAIL"
        print(f"  [{status}] {o.name}  ({o.duration:.1f}s)")
        if not o.passed:
            print(f"         Tools: {o.tools_called}")
            for bug in o.bugs_found[:3]:
                print(f"         - {bug[:200]}")
            if o.tool_results_preview:
                print(f"         First result: {o.tool_results_preview[0][:200]}")


def _workspace_setup():
    """Create files for filesystem tests."""
    test_dir = ROOT / "tests" / "live" / "sandbox"
    test_dir.mkdir(exist_ok=True)
    (test_dir / "hello.txt").write_text("Hello, World!\nLine 2\nLine 3\n", encoding="utf-8")
    (test_dir / "empty.txt").write_text("", encoding="utf-8")
    (test_dir / "target.py").write_text("def foo():\n    return 'original'\n\ndef bar():\n    pass\n", encoding="utf-8")
    (test_dir / "large.txt").write_text("line\n" * 1000, encoding="utf-8")
    (test_dir / "unicode.txt").write_text("日本語 émoji 🔥\n", encoding="utf-8")
    return test_dir
