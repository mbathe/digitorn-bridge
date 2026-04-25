"""Web module tests — search + fetch."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from framework import LiveTester, TestCase, print_outcomes


def run():
    tester = LiveTester()
    cases = [
        TestCase(
            name="W1: Web search",
            app_id="web-tester",
            app_yaml="web-tester.yaml",
            message="Use web.search to find 'Python documentation official'. Return the top 3 results.",
            expected_tools=["search"],
        ),
        TestCase(
            name="W2: Web fetch URL",
            app_id="web-tester",
            app_yaml="web-tester.yaml",
            message="Use web.fetch to retrieve https://example.com and return its text.",
            expected_tools=["fetch"],
            expected_patterns=["example"],
        ),
        TestCase(
            name="W3: Web fetch 404 (graceful)",
            app_id="web-tester",
            app_yaml="web-tester.yaml",
            message="Use web.fetch on https://httpbin.org/status/404",
            expected_tools=["fetch"],
            must_succeed=False,
        ),
    ]
    outcomes = []
    for tc in cases:
        print(f"  Running {tc.name}...")
        out = tester.run_case(tc)
        outcomes.append(out)
        status = "PASS" if out.passed else "FAIL"
        print(f"    [{status}] ({out.duration:.1f}s) tools={out.tools_called}")
    print_outcomes(outcomes, "web")
    tester.undeploy_all()
    return outcomes


if __name__ == "__main__":
    run()
