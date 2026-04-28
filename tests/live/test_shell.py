"""Shell module tests - bash, background, edge cases."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from framework import LiveTester, TestCase, print_outcomes


def run():
    tester = LiveTester()
    cases = [
        TestCase(
            name="S1: Simple bash echo",
            app_id="shell-tester",
            app_yaml="shell-tester.yaml",
            message="Run bash command: echo hello digitorn",
            expected_tools=["bash"],
            expected_patterns=["hello digitorn"],
        ),
        TestCase(
            name="S2: Bash with pipes",
            app_id="shell-tester",
            app_yaml="shell-tester.yaml",
            message="Run bash: echo 'line1\\nline2\\nline3' | grep line2",
            expected_tools=["bash"],
            expected_patterns=["line2"],
        ),
        TestCase(
            name="S3: Bash stderr + exit code",
            app_id="shell-tester",
            app_yaml="shell-tester.yaml",
            message="Run bash: ls /nonexistent-dir-xyz 2>&1; echo exitcode=$?",
            expected_tools=["bash"],
        ),
        TestCase(
            name="S4: Bash with unicode",
            app_id="shell-tester",
            app_yaml="shell-tester.yaml",
            message="Run bash: echo '日本語 émoji 🔥'",
            expected_tools=["bash"],
        ),
        TestCase(
            name="S5: Bash background task (run_in_background=true)",
            app_id="shell-tester",
            app_yaml="shell-tester.yaml",
            message="Run bash 'sleep 2 && echo done' in the background (run_in_background=true) and return the task_id.",
            expected_tools=["bash"],
        ),
        TestCase(
            name="S6: Bash cwd workspace",
            app_id="shell-tester",
            app_yaml="shell-tester.yaml",
            message="Run bash: pwd",
            expected_tools=["bash"],
            expected_patterns=["digitorn"],
        ),
        TestCase(
            name="S7: Bash multi-line script",
            app_id="shell-tester",
            app_yaml="shell-tester.yaml",
            message="Run bash: for i in 1 2 3; do echo item_$i; done",
            expected_tools=["bash"],
            expected_patterns=["item_1", "item_3"],
        ),
    ]
    outcomes = []
    for tc in cases:
        print(f"  Running {tc.name}...")
        out = tester.run_case(tc)
        outcomes.append(out)
        status = "PASS" if out.passed else "FAIL"
        print(f"    [{status}] ({out.duration:.1f}s) tools={out.tools_called}")
    print_outcomes(outcomes, "shell")
    tester.undeploy_all()
    return outcomes


if __name__ == "__main__":
    run()
