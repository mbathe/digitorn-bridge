"""Unit tests for content: "..." wrapper stripping."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from digitorn.core.runtime.messages import _strip_content_wrapper


def _check(label, inp, expected):
    got = _strip_content_wrapper(inp)
    ok = got == expected
    print(f"{'OK' if ok else 'FAIL'}: {label}")
    if not ok:
        print(f"  input:    {inp!r}")
        print(f"  expected: {expected!r}")
        print(f"  got:      {got!r}")
    return ok


def main() -> int:
    passed = 0
    total = 0

    cases = [
        (
            "simple wrapper with Windows path (model-style escapes)",
            r'content: "Mon workspace actuel est C:\\Users\\ASUS\\workspace."',
            r"Mon workspace actuel est C:\Users\ASUS\workspace.",
        ),
        ("plain text untouched", "Hello world", "Hello world"),
        ("wrapper with extra space", 'content : "hi"', "hi"),
        ("wrapper with escaped newline", r'content: "a\nb"', "a\nb"),
        ("empty input", "", ""),
        (
            "wrapper with escaped quotes inside",
            r'content: "escaped \"quote\" ok"',
            'escaped "quote" ok',
        ),
        ("wrapper not at the start untouched", 'blah content: "x"', 'blah content: "x"'),
        ("unterminated wrapper untouched", 'content: "half', 'content: "half'),
        (
            "wrapper followed by tool_calls still unwraps",
            r'content: "done", tool_calls: [{"name": "X"}]',
            "done",
        ),
    ]

    for label, inp, expected in cases:
        total += 1
        if _check(label, inp, expected):
            passed += 1

    print(f"\n{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
