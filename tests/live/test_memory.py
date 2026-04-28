"""Memory module tests - working memory, todos, goals."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from framework import LiveTester, TestCase, print_outcomes


def run():
    tester = LiveTester()
    cases = [
        TestCase(
            name="M1: SetGoal",
            app_id="memory-tester",
            app_yaml="memory-tester.yaml",
            message="Use memory.set_goal to set the goal: 'Build a login feature'",
            expected_tools=["set_goal"],
        ),
        TestCase(
            name="M2: Remember a fact",
            app_id="memory-tester",
            app_yaml="memory-tester.yaml",
            message="Use memory.remember to store: key='user_name', value='Alice'",
            expected_tools=["remember"],
        ),
        TestCase(
            name="M3: TodoAdd three tasks",
            app_id="memory-tester",
            app_yaml="memory-tester.yaml",
            message="Use memory.task_create to create 3 tasks: 'Write code', 'Write tests', 'Deploy'",
            expected_tools=["task_create"],
        ),
        TestCase(
            name="M4: TodoUpdate status",
            app_id="memory-tester",
            app_yaml="memory-tester.yaml",
            message="First create a task 'Test task', then mark it as completed using task_update.",
            expected_tools=["task_create", "task_update"],
        ),
    ]
    outcomes = []
    for tc in cases:
        print(f"  Running {tc.name}...")
        out = tester.run_case(tc)
        outcomes.append(out)
        status = "PASS" if out.passed else "FAIL"
        print(f"    [{status}] ({out.duration:.1f}s) tools={out.tools_called}")
    print_outcomes(outcomes, "memory")
    tester.undeploy_all()
    return outcomes


if __name__ == "__main__":
    run()
