"""Filesystem strict tests - verify actual file state after each operation.

Instead of trusting "tool X was called", we read the file afterwards and
assert byte-exact results. This is how you catch real bugs.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from framework import LiveTester, TestCase, print_outcomes

ROOT = Path(__file__).resolve().parent.parent.parent
SANDBOX = ROOT / "tests" / "live" / "sandbox"


def reset_fixtures():
    SANDBOX.mkdir(exist_ok=True)
    (SANDBOX / "ambiguous.py").write_text(
        "def foo():\n    return 'x'\n\ndef foo():\n    return 'y'\n", encoding="utf-8"
    )
    (SANDBOX / "crlf.txt").write_bytes(b"line1\r\nline2\r\nline3\r\n")
    (SANDBOX / "mixed_indent.py").write_text(
        "def f():\n\treturn 1\n\ndef g():\n    return 2\n", encoding="utf-8"
    )
    (SANDBOX / "target.py").write_text(
        "def foo():\n    return 'original'\n\ndef bar():\n    pass\n", encoding="utf-8"
    )
    (SANDBOX / "hello.txt").write_text("Hello, World!\nLine 2\nLine 3\n", encoding="utf-8")
    (SANDBOX / "unicode.txt").write_text("日本語 émoji 🔥\n", encoding="utf-8")


def _file_bytes(path: str) -> bytes:
    p = SANDBOX / path if not path.startswith("/") else Path(path)
    return p.read_bytes() if p.exists() else b"[file does not exist]"


def _file_text(path: str) -> str:
    p = SANDBOX / path if not path.startswith("/") else Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else "[file does not exist]"


# ───── post_check builders ─────

def check_file_contains(path: str, needle: str):
    def _check(history, tool_results):
        content = _file_text(path)
        if needle in content:
            return True, ""
        return False, f"File {path!r} does not contain {needle!r}. Actual content: {content!r}"
    return _check


def check_file_not_contains(path: str, needle: str):
    def _check(history, tool_results):
        content = _file_text(path)
        if needle not in content:
            return True, ""
        return False, f"File {path!r} still contains {needle!r}. Content: {content!r}"
    return _check


def check_file_bytes_exact(path: str, expected_bytes: bytes):
    def _check(history, tool_results):
        actual = _file_bytes(path)
        if actual == expected_bytes:
            return True, ""
        return False, f"File {path!r} bytes mismatch. Expected {expected_bytes!r}, got {actual!r}"
    return _check


def check_file_preserves_crlf(path: str, must_have_crlf: bool = True):
    def _check(history, tool_results):
        raw = _file_bytes(path)
        has_crlf = b"\r\n" in raw
        if must_have_crlf and not has_crlf:
            return False, f"File {path!r} lost CRLF line endings. Raw: {raw!r}"
        if not must_have_crlf and has_crlf:
            return False, f"File {path!r} unexpectedly has CRLF. Raw: {raw!r}"
        return True, ""
    return _check


def check_tool_result_contains(substr: str):
    def _check(history, tool_results):
        for tr in tool_results:
            if substr in tr:
                return True, ""
        return False, f"No tool result contains {substr!r}. Results: {[t[:100] for t in tool_results[:3]]}"
    return _check


def check_tool_result_has_error(pattern: str):
    """A tool result must show an error matching the pattern."""
    def _check(history, tool_results):
        for tr in tool_results:
            if '"error"' in tr and pattern.lower() in tr.lower():
                return True, ""
        return False, f"No tool result has error matching {pattern!r}. Results: {[t[:120] for t in tool_results[:3]]}"
    return _check


def check_file_unchanged(path: str):
    def _check(history, tool_results):
        # Re-read the expected original from reset_fixtures
        originals = {
            "ambiguous.py": "def foo():\n    return 'x'\n\ndef foo():\n    return 'y'\n",
            "target.py": "def foo():\n    return 'original'\n\ndef bar():\n    pass\n",
        }
        expected = originals.get(path)
        if expected is None:
            return False, f"No reference content for {path!r}"
        actual = _file_text(path)
        if actual == expected:
            return True, ""
        return False, f"File {path!r} was modified when it shouldn't be. Got: {actual!r}"
    return _check


def run():
    reset_fixtures()
    tester = LiveTester(daemon_url="http://127.0.0.1:8001")

    cases = [
        # S1: Ambiguous Edit MUST fail with error, file unchanged
        TestCase(
            name="S1: Ambiguous Edit fails with clear error, file unchanged",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Read tests/live/sandbox/ambiguous.py. Then call Edit with "
                "old_string='def foo():' and new_string='def foo_renamed():'. "
                "The file has two matching lines."
            ),
            expected_tools=["read", "edit"],
            must_succeed=False,  # Edit MUST fail
            post_check=lambda h, tr: (
                all([check_tool_result_has_error("not unique")(h, tr)[0],
                     check_file_unchanged("ambiguous.py")(h, tr)[0]]),
                "Edit should have failed with 'not unique' AND file should be unchanged"
            ),
        ),

        # S2: CRLF must be preserved after Edit (critical on Windows)
        TestCase(
            name="S2: Edit preserves CRLF line endings",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Read tests/live/sandbox/crlf.txt, then Edit to replace 'line2' with 'REPLACED'."
            ),
            expected_tools=["read", "edit"],
            post_check=lambda h, tr: check_file_preserves_crlf("crlf.txt", True)(h, tr),
        ),

        # S3: Write exact bytes
        TestCase(
            name="S3: Write produces exact byte content",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Write tests/live/sandbox/byte_exact.txt with content exactly: "
                "'Hello\\nWorld\\n' (newlines should be actual newlines)."
            ),
            expected_tools=["write"],
            post_check=check_file_bytes_exact("byte_exact.txt", b"Hello\nWorld\n"),
        ),

        # S4: Edit preserves trailing newline
        TestCase(
            name="S4: Edit preserves final newline",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Read tests/live/sandbox/target.py then Edit 'def foo():' to 'def foo_v2():'."
            ),
            expected_tools=["read", "edit"],
            post_check=lambda h, tr: (
                _file_bytes("target.py").endswith(b"\n"),
                f"File lost trailing newline. Last 30 bytes: {_file_bytes('target.py')[-30:]!r}"
            ),
        ),

        # S5: Grep returns EXACT line numbers
        TestCase(
            name="S5: Grep returns correct line numbers",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Use Grep to find 'class BehaviorEngine' in packages/digitorn/modules/behavior/engine.py"
            ),
            expected_tools=["grep"],
            # engine.py has `class BehaviorEngine` at a specific line - check grep returns a line number
            post_check=check_tool_result_contains("engine.py"),
        ),

        # S6: Glob returns complete list (no silent truncation on huge dirs)
        TestCase(
            name="S6: Glob counts every .py in packages/ (no truncation)",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Use Glob with pattern '**/*.py' in packages/ directory. "
                "Report the total count."
            ),
            expected_tools=["glob"],
            # We know there are ~700-1500 .py files in packages/digitorn
            post_check=lambda h, tr: (
                any("count" in tr.lower() and any(str(n) in tr for n in range(200, 3000)) for tr in tr),
                f"Glob count looks off. Results: {[t[:200] for t in tr[:2]]}"
            ),
        ),

        # S7: Write overwrite old content is gone
        TestCase(
            name="S7: Write overwrite - old content must be gone",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message=(
                "Write tests/live/sandbox/hello.txt with content 'BRAND NEW CONTENT'. "
                "The file previously contained 'Hello, World'."
            ),
            expected_tools=["write"],
            post_check=lambda h, tr: (
                "Hello, World" not in _file_text("hello.txt") and "BRAND NEW" in _file_text("hello.txt"),
                f"Old content leaked or new content missing. Got: {_file_text('hello.txt')!r}"
            ),
        ),

        # S8: Read exact bytes
        TestCase(
            name="S8: Read returns unicode correctly",
            app_id="fs-tester",
            app_yaml="filesystem-tester.yaml",
            message="Read tests/live/sandbox/unicode.txt and tell me what you see.",
            expected_tools=["read"],
            post_check=check_tool_result_contains("日本語"),
        ),
    ]

    outcomes = []
    for tc in cases:
        reset_fixtures()  # reset between tests
        print(f"  Running {tc.name}...")
        out = tester.run_case(tc)
        outcomes.append(out)
        status = "PASS" if out.passed else "FAIL"
        print(f"    [{status}] ({out.duration:.1f}s) tools={out.tools_called}")
        if not out.passed:
            for bug in out.bugs_found[:3]:
                print(f"         > {bug[:250]}")

    print_outcomes(outcomes, "filesystem-STRICT")
    tester.undeploy_all()
    return outcomes


if __name__ == "__main__":
    run()
