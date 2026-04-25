"""Hard filesystem tests — push every tool to its limits.

What these tests verify:
  - Edit works with fuzzy matching (whitespace, indent, CRLF)
  - Edit fails safely on ambiguous matches (multiple occurrences)
  - Grep handles regex edge cases (backreferences, special chars, multiline)
  - Grep respects context lines (-B, -A)
  - Grep scales on big dirs (2000+ files)
  - Glob handles nested patterns and edge characters
  - Read returns exact content, handles huge files, binary, etc.
  - Write handles line endings, special chars, overwrites safely
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from framework import LiveTester, TestCase, print_outcomes

ROOT = Path(__file__).resolve().parent.parent.parent
SANDBOX = ROOT / "tests" / "live" / "sandbox"


def setup_hard_fixtures():
    """Create tricky files for the hard tests."""
    SANDBOX.mkdir(exist_ok=True)

    # File with multiple occurrences (edit must be specific or fail)
    (SANDBOX / "ambiguous.py").write_text(
        "def foo():\n    return 'x'\n\ndef foo():\n    return 'y'\n", encoding="utf-8"
    )

    # File with CRLF line endings
    (SANDBOX / "crlf.txt").write_bytes(b"line1\r\nline2\r\nline3\r\n")

    # File with mixed indentation (tabs + spaces)
    (SANDBOX / "mixed_indent.py").write_text(
        "def f():\n\treturn 1\n\ndef g():\n    return 2\n", encoding="utf-8"
    )

    # Huge file (30k lines)
    (SANDBOX / "huge.txt").write_text("\n".join(f"line {i}" for i in range(30000)), encoding="utf-8")

    # File with regex special chars in content
    (SANDBOX / "regex_hell.txt").write_text(
        "price = $5.99\n(parens)\n[brackets]\n{braces}\nend.\n", encoding="utf-8"
    )

    # Nested subdir with many files for grep/glob
    (SANDBOX / "nested").mkdir(exist_ok=True)
    for i in range(50):
        (SANDBOX / "nested" / f"file_{i:03d}.py").write_text(
            f"# file {i}\nTARGET_CONSTANT = {i}\nanother_var = 'hi_{i}'\n",
            encoding="utf-8",
        )

    # File with long single lines (no newlines for 5000 chars)
    (SANDBOX / "oneliner.txt").write_text("a" * 5000 + "\n", encoding="utf-8")

    # Empty subdir
    (SANDBOX / "empty_dir").mkdir(exist_ok=True)

    return SANDBOX


def run():
    setup_hard_fixtures()
    tester = LiveTester()

    cases = [
        # ─────────── EDIT — hard cases ───────────
        TestCase(
            name="H1: Edit ambiguous match (2 occurrences of same def foo)",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Read tests/live/sandbox/ambiguous.py first. "
                "Then use Edit to replace 'def foo()' with 'def foo_ambiguous()'. "
                "The file has TWO 'def foo()' lines — Edit should either fail "
                "with a clear error OR replace both. Report what happened."
            ),
            expected_tools=["read", "edit"],
        ),
        TestCase(
            name="H2: Edit CRLF file (line endings)",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Read tests/live/sandbox/crlf.txt then Edit to replace 'line2' with 'LINE_TWO'. "
                "Verify the edit succeeded by reading the file again."
            ),
            expected_tools=["read", "edit"],
            expected_patterns=["LINE_TWO"],
        ),
        TestCase(
            name="H3: Edit mixed indent (tabs vs spaces)",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Read tests/live/sandbox/mixed_indent.py. Then use Edit to add "
                "a new line 'print(result)' after 'return 1' (inside the tab-indented function f). "
                "The indentation must match (tab-indented)."
            ),
            expected_tools=["read", "edit"],
            expected_patterns=["print(result)"],
        ),
        TestCase(
            name="H4: Edit with whitespace-trailing old_string (fuzzy must handle)",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Read tests/live/sandbox/target.py. Then call Edit with "
                "old_string='def foo():    ' (with trailing spaces) and "
                "new_string='def foo_v2():'. The original has no trailing spaces — "
                "the fuzzy matcher should handle this."
            ),
            expected_tools=["read", "edit"],
        ),

        # ─────────── READ — scale + edge cases ───────────
        TestCase(
            name="H5: Read HUGE file (30k lines)",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Read tests/live/sandbox/huge.txt. If the file is large, use offset=29900, limit=50 to read the last portion.",
            expected_tools=["read"],
            expected_patterns=["line 29"],  # lines near the end
            forbidden_errors=["memory", "too large"],
        ),
        TestCase(
            name="H6: Read single-line 5000-char file",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Read tests/live/sandbox/oneliner.txt. Report the number of characters in the content.",
            expected_tools=["read"],
        ),

        # ─────────── GREP — regex + scale ───────────
        TestCase(
            name="H7: Grep regex with escaped special chars",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Use Grep to search for the pattern '\\$\\d+\\.\\d+' "
                "(a dollar amount like $5.99) in tests/live/sandbox/regex_hell.txt"
            ),
            expected_tools=["grep"],
            expected_patterns=["$5.99"],
        ),
        TestCase(
            name="H8: Grep multiline mode (pattern spanning lines)",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Use Grep with multiline=true to find text spanning lines in "
                "packages/digitorn/modules/behavior/validator.py. "
                "Pattern: 'def\\s+\\w+.*?:\\s*\\n\\s*\"\"\"' (function with docstring)"
            ),
            expected_tools=["grep"],
        ),
        TestCase(
            name="H9: Grep scan large tree (all .py across 6000 files)",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Use Grep to find every occurrence of 'class BehaviorEngine' in the packages/ directory. "
                "Expected: exactly one match in packages/digitorn/modules/behavior/engine.py"
            ),
            expected_tools=["grep"],
            expected_patterns=["engine.py"],
            forbidden_errors=["No matches found"],
        ),
        TestCase(
            name="H10: Grep with output_mode=count",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Use Grep with output_mode='count' to count occurrences of 'TARGET_CONSTANT' "
                "in tests/live/sandbox/nested/. Expected: 50 (one per file_*.py)."
            ),
            expected_tools=["grep"],
        ),

        # ─────────── GLOB — scale + edge patterns ───────────
        TestCase(
            name="H11: Glob recursive deep",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Use Glob with pattern '**/file_0*.py' in tests/live/sandbox/nested/. "
                "Expected: 10 files (file_000.py, file_001.py, ..., file_009.py)."
            ),
            expected_tools=["glob"],
        ),
        TestCase(
            name="H12: Glob with character class",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Use Glob with pattern 'tests/live/sandbox/nested/file_01[0-9].py'. "
                "Expected: 10 files (file_010 to file_019)."
            ),
            expected_tools=["glob"],
        ),
        TestCase(
            name="H13: Glob across huge tree (thousands of files)",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Use Glob with pattern '**/*.py' in the packages/ directory. "
                "Count how many Python files are there."
            ),
            expected_tools=["glob"],
            forbidden_errors=["timeout", "too many"],
        ),

        # ─────────── WRITE — race + overwrite ───────────
        TestCase(
            name="H14: Write overwrite existing file (must preserve no data loss)",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Write tests/live/sandbox/hello.txt with content 'OVERWRITTEN'. "
                "The file previously contained 'Hello, World' — it should now be replaced."
            ),
            expected_tools=["write"],
        ),
        TestCase(
            name="H15: Write file with special chars (quotes, backslashes, unicode)",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Write tests/live/sandbox/special.txt with content: "
                "quote\"test backslash\\\\x newline\\\\n tab\\\\t unicode 日本語"
            ),
            expected_tools=["write"],
        ),

        # ─────────── COMBO — realistic workflow ───────────
        TestCase(
            name="H16: Combo — Grep then Read then Edit then verify",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Workflow: (1) Grep 'TARGET_CONSTANT = 42' in tests/live/sandbox/nested/. "
                "(2) Read the matching file. (3) Edit to change 42 to 999. "
                "(4) Grep again to verify the change."
            ),
            expected_tools=["grep", "read", "edit"],
        ),
    ]

    outcomes = []
    for tc in cases:
        print(f"  Running {tc.name}...")
        out = tester.run_case(tc)
        outcomes.append(out)
        status = "PASS" if out.passed else "FAIL"
        print(f"    [{status}] ({out.duration:.1f}s) tools={out.tools_called}")
        if not out.passed:
            for bug in out.bugs_found[:2]:
                print(f"         > {bug[:180]}")

    print_outcomes(outcomes, "filesystem-HARD")
    tester.undeploy_all()
    return outcomes


if __name__ == "__main__":
    run()
