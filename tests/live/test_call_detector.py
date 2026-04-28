"""Unit tests for the unified call detector.

All formats must parse correctly. Run:
    py -3.12 tests/live/test_call_detector.py
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "packages"))

from digitorn.core.runtime.call_detector import (
    extract_all_calls,
    find_balanced_close,
    parse_call_object,
    try_parse_json_relaxed,
)


CASES = [
    # (name, content, expected_n_calls, expected_first_name, expected_text_before)
    (
        "tool_call tag standard",
        '<tool_call>{"name": "Read", "arguments": {"file_path": "a.py"}}</tool_call>',
        1, "Read", "",
    ),
    (
        "tool_call tag unterminated",
        'Let me check.\n<tool_call>{"name": "Read", "arguments": {"file_path": "a.py"}}',
        1, "Read", "Let me check.",
    ),
    (
        "tool_calls: label with array",
        'content: "Going to list files."\ntool_calls: [{"name": "glob", "arguments": {"pattern": "*.py"}}]',
        1, "glob", 'Going to list files.',
    ),
    (
        "tool_calls: label with array + inner tags",
        'tool_calls: [<tool_call>{"name": "read", "arguments": {"file_path": "x"}}</tool_call>]',
        1, "read", "",
    ),
    (
        "run_parallel with actions=",
        'Je vais explorer.\nrun_parallel(actions=[\n'
        '  {"name": "glob", "params": {"pattern": "**/*.py"}},\n'
        '  {"name": "grep", "params": {"pattern": "def main"}}\n'
        '])',
        2, "glob", "Je vais explorer.",
    ),
    (
        "Chinese 工具调用 marker",
        'content: "我已分析项目。"\n工具调用:\n<tool_call>{"name": "read", "arguments": {"file_path": "x.py"}}</tool_call>',
        1, "read", '我已分析项目。',
    ),
    (
        "multiple calls in tool_calls: array",
        'tool_calls: [\n'
        '  {"name": "glob", "arguments": {"pattern": "*.py"}},\n'
        '  {"name": "read", "arguments": {"file_path": "x.py"}}\n'
        ']',
        2, "glob", "",
    ),
    (
        "bare JSON object no wrapper",
        'Let me check.\n{"name": "Grep", "arguments": {"pattern": "foo"}}',
        1, "Grep", "Let me check.",
    ),
    (
        "args with nested brackets (must balance)",
        '<tool_call>{"name": "edit", "arguments": {"old_string": "[1, 2]", "new_string": "{3: 4}"}}</tool_call>',
        1, "edit", "",
    ),
    (
        "no tool calls at all",
        "This is just plain text. No calls here.",
        0, None, None,
    ),
    (
        "params instead of arguments (qwen style)",
        'run_parallel(actions=[{"name": "read", "params": {"file_path": "x"}}])',
        1, "read", "",
    ),
    (
        "args with Windows path",
        '<tool_call>{"name": "read", "arguments": {"file_path": "C:\\\\Users\\\\test\\\\file.py"}}</tool_call>',
        1, "read", "",
    ),
    (
        "multiple extractors - should pick earliest match",
        # tool_calls: comes BEFORE run_parallel in content
        'tool_calls: [{"name": "glob", "arguments": {}}]\nrun_parallel(actions=[{"name": "read", "params": {}}])',
        1, "glob", "",
    ),
]


def run():
    passed = 0
    failed = []
    for name, content, expected_n, expected_name, expected_text in CASES:
        try:
            result = extract_all_calls(content)
            if expected_n == 0:
                if result is None:
                    passed += 1
                    print(f"  [PASS] {name}")
                    continue
                text, calls = result
                if not calls:
                    passed += 1
                    print(f"  [PASS] {name}")
                    continue
                failed.append((name, f"expected no calls, got {len(calls)}"))
                print(f"  [FAIL] {name}: expected 0 calls, got {len(calls)}")
                continue

            if result is None:
                failed.append((name, "returned None"))
                print(f"  [FAIL] {name}: parser returned None")
                continue

            text, calls = result
            ok = True
            if len(calls) != expected_n:
                failed.append((name, f"expected {expected_n} calls, got {len(calls)}"))
                print(f"  [FAIL] {name}: expected {expected_n} calls, got {len(calls)}")
                ok = False
            elif calls[0][0].lower() != expected_name.lower():
                failed.append((name, f"expected name={expected_name!r}, got {calls[0][0]!r}"))
                print(f"  [FAIL] {name}: name {calls[0][0]!r} != {expected_name!r}")
                ok = False
            elif expected_text is not None and text.strip() != expected_text.strip():
                failed.append((name, f"text mismatch: expected {expected_text!r}, got {text!r}"))
                print(f"  [FAIL] {name}: text {text!r} != {expected_text!r}")
                ok = False
            if ok:
                passed += 1
                print(f"  [PASS] {name} ({len(calls)} calls, name={calls[0][0]!r})")
        except Exception as e:
            failed.append((name, f"crash: {type(e).__name__}: {e}"))
            print(f"  [CRASH] {name}: {type(e).__name__}: {e}")

    print(f"\n{passed}/{len(CASES)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
