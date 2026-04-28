"""Filesystem module - edge case tests with real LLM.

Tests every tool (Read/Write/Edit/Grep/Glob) with extreme scenarios:
- Normal ops, empty files, unicode, large files
- Non-existent paths, relative vs absolute paths
- Fuzzy edit (whitespace, indent variants)
- Grep with regex, special chars, no matches, tons of matches
- Glob patterns, deep paths, case sensitivity
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from framework import LiveTester, TestCase, print_outcomes, _workspace_setup


def run():
    sandbox = _workspace_setup()
    tester = LiveTester(workspace=str(sandbox.parent.parent.parent))  # project root

    cases = [
        # ── READ ──
        TestCase(
            name="R1: Read normal file",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=f"Read the file tests/live/sandbox/hello.txt and return its content.",
            expected_tools=["read"],
            expected_patterns=["Hello, World"],
            forbidden_errors=["does not exist", "not found"],
        ),
        TestCase(
            name="R2: Read empty file",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Read the file tests/live/sandbox/empty.txt.",
            expected_tools=["read"],
            forbidden_errors=["error", "cannot"],
        ),
        TestCase(
            name="R3: Read unicode file",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Read the file tests/live/sandbox/unicode.txt and show me the content.",
            expected_tools=["read"],
            expected_patterns=["日本語"],
        ),
        TestCase(
            name="R4: Read nonexistent file (expect graceful error)",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Read tests/live/sandbox/ghost.txt",
            expected_tools=["read"],
            # Agent should get an error but not crash
            must_succeed=False,
        ),
        TestCase(
            name="R5: Read with offset+limit",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Read tests/live/sandbox/large.txt lines 10 to 15 only (use offset=10, limit=5).",
            expected_tools=["read"],
        ),

        # ── WRITE ──
        TestCase(
            name="W1: Write new file",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Create tests/live/sandbox/created_w1.txt with content 'W1 test content'.",
            expected_tools=["write"],
            forbidden_errors=["denied", "permission"],
        ),
        TestCase(
            name="W2: Write with newlines",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Create tests/live/sandbox/multi_line.txt with three lines: 'alpha', 'beta', 'gamma' separated by newlines.",
            expected_tools=["write"],
        ),

        # ── EDIT ──
        TestCase(
            name="E1: Edit exact match",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="First Read tests/live/sandbox/target.py, then use Edit to change 'original' to 'modified'.",
            expected_tools=["read", "edit"],
            expected_patterns=["modified"],
        ),
        TestCase(
            name="E2: Edit without read (should warn or fail helpfully)",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Use Edit directly on tests/live/sandbox/hello.txt to replace 'World' with 'Digitorn' without reading first.",
            expected_tools=["edit"],
        ),

        # ── GREP ──
        TestCase(
            name="G1: Grep existing pattern",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Use Grep to search for 'validate_behavior_config' in packages/digitorn/modules/behavior/ and return matches.",
            expected_tools=["grep"],
            expected_patterns=["validator.py"],
            forbidden_errors=["No matches found"],
        ),
        TestCase(
            name="G2: Grep regex with special chars",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Use Grep with regex 'def\\s+\\w+' on packages/digitorn/modules/behavior/validator.py",
            expected_tools=["grep"],
            forbidden_errors=["Invalid regex"],
        ),
        TestCase(
            name="G3: Grep no match (should return empty gracefully)",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Use Grep to search for 'zyxwvutsrq_impossible_pattern_xyz' in packages/",
            expected_tools=["grep"],
            forbidden_errors=["crash", "exception"],
            must_succeed=True,  # should succeed with 0 matches
        ),

        # ── GLOB ──
        TestCase(
            name="GL1: Glob python files",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Use Glob to list all .yaml files in tests/live/apps/",
            expected_tools=["glob"],
            expected_patterns=["filesystem-tester.yaml"],
        ),
        TestCase(
            name="GL2: Glob recursive deep pattern",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Use Glob with pattern '**/validator.py' to find all files named validator.py in packages/",
            expected_tools=["glob"],
        ),
        TestCase(
            name="GL3: Glob no match",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Use Glob with pattern '**/*.xyz_impossible_ext' in packages/",
            expected_tools=["glob"],
            must_succeed=True,
        ),
    ]

    outcomes = []
    for tc in cases:
        print(f"  Running {tc.name}...")
        out = tester.run_case(tc)
        outcomes.append(out)
        status = "PASS" if out.passed else "FAIL"
        print(f"    [{status}] ({out.duration:.1f}s) tools={out.tools_called}")

    print_outcomes(outcomes, "filesystem")
    tester.undeploy_all()
    return outcomes


if __name__ == "__main__":
    run()
