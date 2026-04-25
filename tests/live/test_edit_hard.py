"""Live test: can Edit handle complex multi-location fixes in a big file?

Setup: big_buggy.py has 5 distinct bugs in 5 different functions.
Task: ask a local LLM (llama3.1:8b) to fix them ALL via Edit tool calls.
Verify: file after == expected fixed version, byte-exact.
"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from digitorn.testing.client import DevClient

TARGET = ROOT / "tests" / "live" / "sandbox" / "big_buggy.py"
ORIGINAL = TARGET.read_text(encoding="utf-8")


BUGS_AND_FIXES = [
    # (bug description, expected_substring_after_fix, must_not_contain_after_fix)
    (
        "Config.max_tokens default is 1024, should be 8192",
        "max_tokens: int = 8192",
        "max_tokens: int = 1024",
    ),
    (
        "Client.connect() always returns False, should return True",
        "connect(self) -> bool",  # sanity still there
        None,  # handled by logic check below
    ),
    (
        "Worker.run_once() returns None on success, should return True",
        "return True",
        None,
    ),
    (
        "Parser.parse() joined = self._buffer (list), should be 'self._buffer' joined to string",
        "joined",
        "joined = self._buffer\n",
    ),
]


def reset_target():
    TARGET.write_text(ORIGINAL, encoding="utf-8")


def check_connect_returns_true(content: str) -> tuple[bool, str]:
    """connect() must have a return True path."""
    # find the connect function body
    start = content.find("def connect(self)")
    if start == -1:
        return False, "connect() method not found"
    # find next def or class after it
    end = content.find("\n    def ", start + 10)
    if end == -1:
        end = len(content)
    body = content[start:end]
    if "return False" in body and "return True" not in body:
        return False, "connect() still always returns False"
    if "return True" not in body:
        return False, "connect() doesn't return True anywhere"
    return True, ""


def check_run_once_returns_true(content: str) -> tuple[bool, str]:
    """run_once should return True on success path."""
    start = content.find("def run_once(self")
    if start == -1:
        return False, "run_once() not found"
    end = content.find("\n    def ", start + 10)
    if end == -1:
        end = len(content)
    body = content[start:end]
    # Must not have `return None` literally in success path
    if "return None" in body:
        return False, "run_once() still has 'return None' in body"
    # Should have return True somewhere (the success path)
    if "return True" not in body:
        return False, "run_once() doesn't return True on success"
    return True, ""


def check_parser_joins(content: str) -> tuple[bool, str]:
    """parse() must join self._buffer into a string before splitting."""
    start = content.find("def parse(self)")
    if start == -1:
        return False, "parse() not found"
    end = content.find("\n    def ", start + 10)
    if end == -1:
        end = len(content)
    body = content[start:end]
    # The bug was `joined = self._buffer` — must now be a real join
    # Accept either `"".join(self._buffer)` or `"\n".join(...)` or similar.
    if 'joined = self._buffer\n' in body:
        return False, "parse() still assigns the list directly to 'joined'"
    if ".join(" not in body and "splitlines" not in body:
        return False, "parse() doesn't appear to join the buffer"
    return True, ""


def check_max_tokens(content: str) -> tuple[bool, str]:
    if "max_tokens: int = 8192" in content:
        return True, ""
    if "max_tokens: int = 1024" in content:
        return False, "max_tokens still 1024 (bug not fixed)"
    # Any other value means the model did something weird
    import re
    m = re.search(r"max_tokens:\s*int\s*=\s*(\d+)", content)
    if m:
        return False, f"max_tokens was changed to {m.group(1)} (expected 8192)"
    return False, "max_tokens default not found"


CHECKS = [
    ("bug1_max_tokens_8192", check_max_tokens),
    ("bug2_connect_returns_true", check_connect_returns_true),
    ("bug3_run_once_returns_true", check_run_once_returns_true),
    ("bug4_parser_joins_buffer", check_parser_joins),
]


def main():
    reset_target()
    print("=" * 70)
    print("  EDIT HARD TEST — can local LLM fix 4 bugs in a big file?")
    print("=" * 70)
    print(f"Target: {TARGET.relative_to(ROOT)}  ({len(ORIGINAL)} chars, {ORIGINAL.count(chr(10))} lines)")
    print()

    client = DevClient(daemon_url="http://127.0.0.1:8001", auto_approve=True, timeout=300)

    # Deploy the Ollama-based app
    print("Deploying fs-edit-hard...")
    app = client.deploy(
        ROOT / "tests" / "live" / "apps" / "fs-edit-hard.yaml",
        force=True, wait=5,
    )
    print(f"  status={app.status} tools={app.total_tools}")

    session = client.create_session("fs-edit-hard", workspace=str(ROOT))

    # Detailed instructions — tell the model exactly what to do
    message = """You are going to fix a file SEQUENTIALLY. Workspace is the current directory. File is at tests/live/sandbox/big_buggy.py (relative path).

CRITICAL RULES:
- Call tools ONE AT A TIME. Wait for each result before calling the next.
- After Read, use the EXACT text from the file (with type annotations, indentation).
- If an Edit fails, read the error message, fix old_string, retry.

STEP 1: Call Read with file_path="tests/live/sandbox/big_buggy.py"
STEP 2: Wait for the Read result. Look at the EXACT source text.
STEP 3: Call Edit ONCE to fix bug #1 (Config.max_tokens = 1024 → 8192)
STEP 4: Wait for the result. If error, retry with a better old_string.
STEP 5: Call Edit ONCE for bug #2 (Client.connect returns False → True)
STEP 6: Wait for result.
STEP 7: Call Edit ONCE for bug #3 (Parser.parse, joined = self._buffer)
STEP 8: Wait for result.
STEP 9: Call Edit ONCE for bug #4 (Worker.run_once returns None → True)

BUGS DETAILS:
1. Config.max_tokens line 33: has `max_tokens: int = 1024` (WITH type annotation), change the `1024` to `8192`
2. Client.connect line 54: replace `return False` — but it's not unique, use surrounding context: `logger.info("connecting to %s:%d", self._config.host, self._config.port)\\n        # BUG #2: returns False even on success — always reports failure\\n        return False` → replace this whole block so you include enough context
3. Parser.parse ~line 99: exact text `joined = self._buffer` → `joined = "\\n".join(self._buffer).split("\\n")`
4. Worker.run_once line 121: the `return None` that follows `self.stats["processed"] += 1`

DO ONE TOOL CALL PER TURN. NEVER BATCH."""

    print(f"\nSending task to llama3.1:8b...\n")
    t0 = time.monotonic()
    result = client.send(session, message, timeout=280)
    duration = time.monotonic() - t0
    print(f"Duration: {duration:.1f}s")
    print(f"Tool calls: {len(result.tool_calls)}")
    for tc in result.tool_calls:
        print(f"  -> {tc.name}({str(tc.arguments)[:120]})")
    print()

    # Verify the file
    new_content = TARGET.read_text(encoding="utf-8")
    print(f"File after: {len(new_content)} chars (original was {len(ORIGINAL)})")
    print()

    print("=" * 70)
    print("  CHECKS")
    print("=" * 70)
    passed = 0
    for name, check_fn in CHECKS:
        ok, reason = check_fn(new_content)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
        if not ok:
            print(f"         {reason}")
        if ok:
            passed += 1

    print()
    print(f"RESULT: {passed}/{len(CHECKS)} bugs fixed")

    if passed < len(CHECKS):
        # Show diff for context
        print()
        print("=" * 70)
        print("  DIFF (original → fixed)")
        print("=" * 70)
        import difflib
        diff = list(difflib.unified_diff(
            ORIGINAL.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            lineterm="",
            n=2,
        ))
        for line in diff[:80]:
            print(line.rstrip())
        if len(diff) > 80:
            print(f"... ({len(diff) - 80} more lines)")

    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
