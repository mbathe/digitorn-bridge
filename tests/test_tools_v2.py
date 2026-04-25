"""Comprehensive tests for all 22 LLM-exposed tools.

Tests: params validation, backward compat, schema generation,
tool_prompt presence, action execution, error handling, edge cases.

Run: py -3.12 tests/test_tools_v2.py
"""
import sys
import os
import json
import asyncio
import tempfile
import shutil

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0
total_sections = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name} -- {detail}")


def section(title):
    global total_sections
    total_sections += 1
    print(f"\n=== {total_sections}. {title} ===")


# ══════════════════════════════════════════════════════════
# Setup: load modules, create temp workspace
# ══════════════════════════════════════════════════════════
import logging
logging.disable(logging.WARNING)

from digitorn.modules.context_builder.tool_schema import action_entry_to_json_schema
from digitorn.core.runtime.tool_names import to_fqn, to_short

TMPDIR = tempfile.mkdtemp(prefix="digitorn_test_")


_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

def run(coro):
    return _loop.run_until_complete(coro)


# ══════════════════════════════════════════════════════════
# PART 1: PARAMS VALIDATION & BACKWARD COMPAT
# ══════════════════════════════════════════════════════════

section("ReadParams — Claude Code naming")
from digitorn.modules.filesystem.params import ReadParams

# New style
r = ReadParams(file_path="test.py", offset=0, limit=50)
check("file_path set", r.file_path == "test.py")
check("path alias works", r.path == "test.py")
check("offset=0 → start_line=1", r.start_line == 1)
check("limit=50, offset=0 → end_line=50", r.end_line == 50)
check("offset=10, limit=20 → start_line=11", ReadParams(file_path="x", offset=10, limit=20).start_line == 11)
check("offset=10, limit=20 → end_line=30", ReadParams(file_path="x", offset=10, limit=20).end_line == 30)

# Backward compat
r2 = ReadParams.model_validate({"path": "old.py", "start_line": 5, "end_line": 15})
check("old path alias", r2.file_path == "old.py")
check("old start_line alias", r2.offset == 5)
check("old end_line alias", r2.limit == 15)

# None values
r3 = ReadParams(file_path="x.py")
check("offset None → start_line None", r3.start_line is None)
check("limit None → end_line None", r3.end_line is None)

# Schema
schema = ReadParams.model_json_schema()
check("schema has file_path", "file_path" in schema["properties"])
check("schema has limit", "limit" in schema["properties"])
check("schema has offset", "offset" in schema["properties"])
check("schema no 'path'", "path" not in schema["properties"])
check("schema no 'start_line'", "start_line" not in schema["properties"])
check("schema no 'end_line'", "end_line" not in schema["properties"])


section("WriteParams — Claude Code naming")
from digitorn.modules.filesystem.params import WriteParams

w = WriteParams(file_path="out.py", content="hello")
check("file_path set", w.file_path == "out.py")
check("path alias", w.path == "out.py")
check("content set", w.content == "hello")

w2 = WriteParams.model_validate({"path": "old.py", "content": "x"})
check("old path compat", w2.file_path == "old.py")

schema = WriteParams.model_json_schema()
check("schema has file_path", "file_path" in schema["properties"])
check("schema no 'path'", "path" not in schema["properties"])


section("EditParams — Claude Code naming")
from digitorn.modules.filesystem.params import EditParams

e = EditParams(file_path="test.py", old_string="foo", new_string="bar")
check("file_path set", e.file_path == "test.py")
check("path alias", e.path == "test.py")
check("old_string", e.old_string == "foo")
check("new_string", e.new_string == "bar")
check("replace_all default false", e.replace_all is False)

e2 = EditParams.model_validate({"path": "x.py", "old_string": "a", "new_string": "b"})
check("old path compat", e2.file_path == "x.py")

# min_length validation
try:
    EditParams(file_path="x", old_string="", new_string="b")
    check("empty old_string rejected", False, "should have raised")
except Exception:
    check("empty old_string rejected", True)

schema = EditParams.model_json_schema()
check("schema has file_path", "file_path" in schema["properties"])
check("schema no 'path'", "path" not in schema["properties"])


section("GrepParams — glob alias")
from digitorn.modules.filesystem.params import GrepParams

g = GrepParams(pattern="def.*", glob="*.py")
check("glob set", g.glob == "*.py")
check("include alias", g.include == "*.py")

g2 = GrepParams.model_validate({"pattern": "x", "include": "*.ts"})
check("old include compat", g2.glob == "*.ts")
check("include alias from old", g2.include == "*.ts")

schema = GrepParams.model_json_schema()
check("schema has glob", "glob" in schema["properties"])
check("schema no 'include'", "include" not in schema["properties"])
check("schema has context", "context" in schema["properties"])
# Hidden params should still be in raw schema (filtered by tool_schema.py)
check("schema has recursive (hidden)", "recursive" in schema["properties"])


section("BashParams — run_in_background")
from digitorn.modules.shell.params import BashParams

b = BashParams(command="echo hi")
check("command set", b.command == "echo hi")
check("run_in_background default false", b.run_in_background is False)
check("description default empty", b.description == "")

b2 = BashParams(command="npm build", run_in_background=True, description="Building")
check("run_in_background true", b2.run_in_background is True)
check("description set", b2.description == "Building")


section("BashStatusParams")
from digitorn.modules.shell.params import BashStatusParams

bs = BashStatusParams(task_id="abc123")
check("task_id set", bs.task_id == "abc123")
check("kill default false", bs.kill is False)

bs2 = BashStatusParams(task_id="x", kill=True)
check("kill true", bs2.kill is True)


section("AgentParams — unified agent")
from digitorn.modules.agent_spawn.params import AgentParams

a = AgentParams(prompt="Research auth module")
check("prompt set", a.prompt == "Research auth module")
check("description default empty", a.description == "")
check("specialist default None", a.specialist is None)
check("wait default true", a.wait is True)

a2 = AgentParams(prompt="Run tests", description="Tests", specialist="worker", wait=False)
check("all params set", a2.specialist == "worker" and not a2.wait)

schema = AgentParams.model_json_schema()
props = schema["properties"]
check("schema has prompt", "prompt" in props)
check("schema has description", "description" in props)
check("schema has specialist", "specialist" in props)
check("schema has wait", "wait" in props)
check("schema no max_turns (hidden)", "max_turns" not in props or props["max_turns"].get("hidden"))
check("schema no timeout (hidden)", "timeout" not in props or props["timeout"].get("hidden"))


section("AgentWaitAllParams")
from digitorn.modules.agent_spawn.params import AgentWaitAllParams

aw = AgentWaitAllParams()
check("agent_ids default None", aw.agent_ids is None)

aw2 = AgentWaitAllParams(agent_ids=["a1", "a2"])
check("agent_ids set", aw2.agent_ids == ["a1", "a2"])


section("TaskCreateParams — Claude Code style")
from digitorn.modules.memory.module import TaskCreateParams

tc = TaskCreateParams(subject="Fix auth bug")
check("subject set", tc.subject == "Fix auth bug")
check("description default empty", tc.description == "")

tc2 = TaskCreateParams(subject="Test", description="Run pytest")
check("description set", tc2.description == "Run pytest")


section("TaskUpdateParams — Claude Code style")
from digitorn.modules.memory.module import TaskUpdateParams

tu = TaskUpdateParams(taskId="t1", status="in_progress")
check("taskId set", tu.taskId == "t1")
check("status set", tu.status == "in_progress")


section("FetchParams — extract mode")
from digitorn.modules.web.params import FetchParams

f = FetchParams(url="https://example.com")
check("url set", f.url == "https://example.com")
check("extract default false", f.extract is False)
check("prompt default empty", f.prompt == "")

f2 = FetchParams(url="https://x.com", extract=True, prompt="pricing")
check("extract true", f2.extract is True)
check("prompt set", f2.prompt == "pricing")


# ══════════════════════════════════════════════════════════
# PART 2: TOOL NAME RESOLUTION
# ══════════════════════════════════════════════════════════

section("Tool name resolution — new names")
name_tests = {
    "Agent": "agent_spawn.agent",
    "AgentWaitAll": "agent_spawn.agent_wait_all",
    "Bash": "shell.bash",
    "BashStatus": "shell.bash_status",
    "Remember": "memory.remember",
    "TaskCreate": "memory.task_create",
    "TaskUpdate": "memory.task_update",
    "Read": "filesystem.read",
    "Write": "filesystem.write",
    "Edit": "filesystem.edit",
    "Grep": "filesystem.grep",
    "Glob": "filesystem.glob",
    "Ls": "filesystem.ls",
    "WebSearch": "web.search",
    "WebFetch": "web.fetch",
}
for short, expected in name_tests.items():
    got = to_fqn(short)
    check(f"{short} → {expected}", got == expected, f"got {got}")


section("Tool name resolution — old names must NOT resolve")
old_names = [
    "AgentWait", "AgentResult", "AgentStatus", "AgentCancel",
    "AgentList", "AgentReassign", "BashBackground",
    "SetGoal", "Recall", "Forget", "TodoAdd", "TodoUpdate",
    "Delete", "WebExtract",
]
for name in old_names:
    fqn = to_fqn(name)
    check(f"{name} not mapped", fqn == name, f"still maps to {fqn}")


section("Reverse resolution — FQN to short")
reverse_tests = {
    "agent_spawn.agent": "Agent",
    "shell.bash": "Bash",
    "memory.remember": "Remember",
    "memory.task_create": "TaskCreate",
    "filesystem.read": "Read",
    "filesystem.edit": "Edit",
    "web.fetch": "WebFetch",
}
for fqn, expected in reverse_tests.items():
    got = to_short(fqn)
    check(f"{fqn} → {expected}", got == expected, f"got {got}")


# ══════════════════════════════════════════════════════════
# PART 3: SCHEMA GENERATION (what LLM sees)
# ══════════════════════════════════════════════════════════

section("Schema generation — hidden params filtered")
from digitorn.modules.registry import ModuleRegistry
from digitorn.core.loader import load_modules

registry = ModuleRegistry()
load_modules(registry, load_all=True)

TOOLS = {
    "filesystem": ["read", "write", "edit", "ls", "grep", "glob"],
    "shell": ["bash", "bash_status"],
    "memory": ["remember", "task_create", "task_update"],
    "agent_spawn": ["agent", "agent_wait_all"],
    "web": ["search", "fetch"],
    "context_builder": ["ask_user", "run_parallel", "search_tools", "execute_tool",
                        "background_run", "background_status"],
    "database": ["sql"],
}

for mod_id, actions in TOOLS.items():
    mod = registry.get(mod_id)
    if not mod:
        check(f"{mod_id} loaded", False, "module not found")
        continue
    reg = getattr(mod, "_action_registry", {})
    for action_name in actions:
        entry = reg.get(action_name)
        if not entry:
            check(f"{mod_id}.{action_name} found", False, "action not found")
            continue
        schema = action_entry_to_json_schema(entry)
        props = schema.get("properties", {})
        hidden = [k for k, v in props.items() if isinstance(v, dict) and v.get("hidden")]
        check(f"{mod_id}.{action_name} no hidden leak", len(hidden) == 0,
              f"leaked: {hidden}")


section("Tool prompts — all 22 have substantial prompts")
for mod_id, actions in TOOLS.items():
    mod = registry.get(mod_id)
    if not mod:
        continue
    reg = getattr(mod, "_action_registry", {})
    for action_name in actions:
        entry = reg.get(action_name)
        if not entry:
            continue
        tp = entry.spec.tool_prompt or ""
        check(f"{mod_id}.{action_name} has tool_prompt ({len(tp)} chars)",
              len(tp) > 20, f"only {len(tp)} chars")


# ══════════════════════════════════════════════════════════
# PART 4: FILESYSTEM ACTIONS — real execution
# ══════════════════════════════════════════════════════════

section("Filesystem Read — real execution")

fs = registry.get("filesystem")
# Initialize workspace so _check_path allows our temp dir
fs._workspace_root = TMPDIR

# Create test file
test_file = os.path.join(TMPDIR, "test_read.py")
with open(test_file, "w", encoding="utf-8") as f:
    f.write("line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10\n")

result = run(fs.read(ReadParams(file_path=test_file)))
check("read success", result.success)
check("read has content", "content" in result.data)
check("read has total_lines", result.data.get("total_lines") == 10)

# Read with offset + limit (Claude Code style)
result2 = run(fs.read(ReadParams(file_path=test_file, offset=2, limit=3)))
check("read offset+limit success", result2.success)
content = result2.data.get("content", "")
check("read starts at line 3", "line3" in content)
check("read includes line 5", "line5" in content)
check("read excludes line 6", "line6" not in content)

# Read with pattern
result3 = run(fs.read(ReadParams(file_path=test_file, pattern="line5")))
check("read pattern success", result3.success)
check("read pattern finds line5", "line5" in result3.data.get("content", ""))

# Read non-existent
result4 = run(fs.read(ReadParams(file_path=os.path.join(TMPDIR, "nope.py"))))
check("read non-existent fails", not result4.success)

# Backward compat
result5 = run(fs.read(ReadParams.model_validate({"path": test_file, "start_line": 1, "end_line": 2})))
check("read old params compat", result5.success)


section("Filesystem Write — real execution")

write_file = os.path.join(TMPDIR, "test_write.py")
result = run(fs.write(WriteParams(file_path=write_file, content="hello world\nsecond line\n")))
check("write success", result.success)
check("file created", os.path.exists(write_file))
check("content correct", open(write_file).read() == "hello world\nsecond line\n")

# Write subdirectory (auto-create — parent dirs are created by the action)
deep_file = os.path.join(TMPDIR, "sub", "dir", "deep.txt")
result2 = run(fs.write(WriteParams.model_validate(
    {"file_path": deep_file, "content": "deep content", "create_dirs": True}
)))
check("write auto-mkdir", result2.success and os.path.exists(deep_file),
      f"success={result2.success}, error={getattr(result2, 'error', '')}")

# Write overwrite
result3 = run(fs.write(WriteParams(file_path=write_file, content="overwritten")))
check("write overwrite", result3.success)
check("content overwritten", open(write_file).read() == "overwritten")


section("Filesystem Edit — real execution")

edit_file = os.path.join(TMPDIR, "test_edit.py")
with open(edit_file, "w") as f:
    f.write("def hello():\n    return 'world'\n\ndef goodbye():\n    return 'bye'\n")

# Must read first
fs._read_files.add(str(edit_file))

result = run(fs.edit(EditParams(file_path=edit_file, old_string="return 'world'", new_string="return 'universe'")))
check("edit success", result.success)
check("edit has diff", "diff" in result.data)
check("edit replacements=1", result.data.get("replacements") == 1)
check("file content updated", "universe" in open(edit_file).read())

# Edit non-unique
dup_file = os.path.join(TMPDIR, "test_dup.py")
with open(dup_file, "w") as f:
    f.write("x = 1\nx = 1\nx = 1\n")
fs._read_files.add(str(dup_file))

result2 = run(fs.edit(EditParams(file_path=dup_file, old_string="x = 1", new_string="x = 2")))
check("edit non-unique fails", not result2.success)
check("edit mentions 3 times", "3 times" in result2.error or "3" in result2.error)

# Edit with replace_all
result3 = run(fs.edit(EditParams(file_path=dup_file, old_string="x = 1", new_string="x = 2", replace_all=True)))
check("edit replace_all success", result3.success)
check("edit replace_all count=3", result3.data.get("replacements") == 3)

# Edit not found
result4 = run(fs.edit(EditParams(file_path=edit_file, old_string="NOTHERE", new_string="x")))
check("edit not found fails", not result4.success)
check("edit not found has hint", "not found" in result4.error.lower() or "closest" in result4.error.lower())

# Backward compat
fs._read_files.add(str(edit_file))
result5 = run(fs.edit(EditParams.model_validate(
    {"path": edit_file, "old_string": "return 'universe'", "new_string": "return 'cosmos'"}
)))
check("edit old path compat", result5.success)


section("Filesystem Grep — real execution")

# Create files to grep
grep_dir = os.path.join(TMPDIR, "grep_test")
os.makedirs(grep_dir, exist_ok=True)
with open(os.path.join(grep_dir, "a.py"), "w") as f:
    f.write("def login():\n    pass\n")
with open(os.path.join(grep_dir, "b.py"), "w") as f:
    f.write("def logout():\n    pass\n")
with open(os.path.join(grep_dir, "c.txt"), "w") as f:
    f.write("no functions here\n")

result = run(fs.grep(GrepParams(pattern="def log", path=grep_dir)))
check("grep success", result.success)
grep_str = json.dumps(result.data)
check("grep finds login", "login" in grep_str)
check("grep finds logout", "logout" in grep_str)

# Grep with glob filter
result2 = run(fs.grep(GrepParams(pattern="def log", path=grep_dir, glob="*.txt")))
check("grep glob filter", result2.success)
check("grep glob no match", result2.data.get("numMatches", 0) == 0)

# Backward compat
result3 = run(fs.grep(GrepParams.model_validate({"pattern": "login", "path": grep_dir, "include": "*.py"})))
check("grep old include compat", result3.success)


section("Filesystem Glob — real execution")
from digitorn.modules.filesystem.params import GlobParams

result = run(fs.glob(GlobParams(pattern="*.py", path=grep_dir)))
check("glob success", result.success)
files = result.data.get("files", result.data.get("matches", []))
check("glob finds 2 py files", len(files) == 2, f"got {len(files)}")

result2 = run(fs.glob(GlobParams(pattern="*.txt", path=grep_dir)))
check("glob finds 1 txt", len(result2.data.get("files", result2.data.get("matches", []))) == 1)


section("Filesystem Ls — real execution")
from digitorn.modules.filesystem.params import LsParams

result = run(fs.ls(LsParams(path=grep_dir)))
check("ls success", result.success)
entries = result.data.get("entries", [])
check("ls finds 3 files", len(entries) == 3, f"got {len(entries)}: {entries}")


# ══════════════════════════════════════════════════════════
# PART 5: SHELL ACTIONS — real execution
# ══════════════════════════════════════════════════════════

section("Shell Bash — real execution")

shell = registry.get("shell")
# Initialize shell with workspace
try:
    run(shell.on_config_update({"workspace": TMPDIR}))
except Exception:
    pass  # may already be configured

result = run(shell.bash(BashParams(command="echo hello_test_123")))
check("bash success", result.success)
check("bash stdout", "hello_test_123" in result.data.get("stdout", ""))

# Bash with exit code
result2 = run(shell.bash(BashParams(command="exit 42")))
check("bash exit code", not result2.success or result2.data.get("exit_code") == 42)

# Bash run_in_background dispatches
result3 = run(shell.bash(BashParams(command="echo bg_test", run_in_background=True)))
check("bash bg returns task_id", result3.success and "task_id" in result3.data)


section("Shell BashStatus — real execution")

if result3.success and "task_id" in result3.data:
    import time
    time.sleep(0.5)  # Let bg task finish
    task_id = result3.data["task_id"]
    result4 = run(shell.bash_status(BashStatusParams(task_id=task_id)))
    check("bash_status success", result4.success)
    check("bash_status has status", "status" in result4.data)
else:
    check("bash_status skipped (no bg task)", True)

# BashStatus not found
result5 = run(shell.bash_status(BashStatusParams(task_id="nonexistent")))
check("bash_status not found fails", not result5.success)


# ══════════════════════════════════════════════════════════
# PART 6: MEMORY ACTIONS — real execution
# ══════════════════════════════════════════════════════════

section("Memory Remember — real execution")

mem = registry.get("memory")
# Initialize memory with default config
try:
    run(mem.on_config_update({}))
except Exception:
    pass
from digitorn.modules.memory.module import RememberParams

result = run(mem.remember(RememberParams(content="Auth bug is in validate.py:42")))
check("remember success", result.success)
check("remember has id", "id" in result.data)

# Deduplication
result2 = run(mem.remember(RememberParams(content="Auth bug is in validate.py:42")))
check("remember dedup", result2.success and result2.data.get("action") == "already_stored")


section("Memory TaskCreate — real execution")

result = run(mem.task_create(TaskCreateParams(subject="Fix auth bug", description="In validate.py")))
check("task_create success", result.success)
check("task_create has todos", "todos" in result.data)
check("task_create content correct", any("Fix auth bug" in str(t) for t in result.data.get("todos", [])))


section("Memory TaskUpdate — real execution")

todos = result.data.get("todos", [])
if todos:
    todo_id = todos[0].get("id", "t1")
    result2 = run(mem.task_update(TaskUpdateParams(taskId=todo_id, status="in_progress")))
    check("task_update success", result2.success)
    # Check status changed
    updated = [t for t in result2.data.get("todos", []) if t.get("id") == todo_id]
    check("task_update status changed", len(updated) > 0 and updated[0].get("status") == "in_progress",
          f"got {updated}")

    # Complete it
    result3 = run(mem.task_update(TaskUpdateParams(taskId=todo_id, status="completed")))
    check("task_update completed", result3.success)

    # Invalid status
    result4 = run(mem.task_update(TaskUpdateParams(taskId=todo_id, status="invalid_status")))
    check("task_update invalid status fails", not result4.success)
else:
    check("task_update skipped (no todos)", False, "task_create didn't create todos")

# Not found
result5 = run(mem.task_update(TaskUpdateParams(taskId="nonexistent_999", status="done")))
check("task_update not found fails", not result5.success)


# ══════════════════════════════════════════════════════════
# PART 7: FINE-TUNING FILES COHERENCE
# ══════════════════════════════════════════════════════════

section("Fine-tuning files coherence")

with open("docs/tools_for_finetuning.json", encoding="utf-8") as f:
    tools_ft = json.load(f)
with open("docs/system_prompts_for_finetuning.json", encoding="utf-8") as f:
    prompts_ft = json.load(f)

tools_fqns = {t["fqn"] for t in tools_ft["tools"]}
prompt_fqns = set(prompts_ft["tool_prompts"].keys())

check("22 tools in tools_for_finetuning", len(tools_fqns) == 22, f"got {len(tools_fqns)}")
check("22 tools in system_prompts", len(prompt_fqns) == 22, f"got {len(prompt_fqns)}")
check("tools match prompts", tools_fqns == prompt_fqns,
      f"diff: {tools_fqns.symmetric_difference(prompt_fqns)}")

# Tool prompt sync
tp_mismatches = []
for t in tools_ft["tools"]:
    if t["fqn"] in prompts_ft["tool_prompts"]:
        if t["tool_prompt"] != prompts_ft["tool_prompts"][t["fqn"]]["tool_prompt"]:
            tp_mismatches.append(t["fqn"])
check("tool_prompt sync 22/22", len(tp_mismatches) == 0, f"mismatches: {tp_mismatches}")

# Schema sync
sc_mismatches = []
for t in tools_ft["tools"]:
    if t["fqn"] in prompts_ft["tool_prompts"]:
        if t["parameters"] != prompts_ft["tool_prompts"][t["fqn"]]["schema"]:
            sc_mismatches.append(t["fqn"])
check("schema sync 22/22", len(sc_mismatches) == 0, f"mismatches: {sc_mismatches}")

# All have tool_prompt
no_tp = [t["fqn"] for t in tools_ft["tools"] if len(t.get("tool_prompt", "")) < 20]
check("all tools have tool_prompt", len(no_tp) == 0, f"missing: {no_tp}")

# No old tool names
old_tool_names = {"AgentWait", "AgentResult", "AgentStatus", "AgentCancel",
                  "AgentList", "AgentReassign", "BashBackground",
                  "SetGoal", "Recall", "Forget", "TodoAdd", "TodoUpdate",
                  "Insert", "Delete", "WebExtract", "GetTool", "BackgroundResult"}
current_names = {t["short_name"] for t in tools_ft["tools"]}
leaked = current_names & old_tool_names
check("no old tool names in export", len(leaked) == 0, f"leaked: {leaked}")


# ══════════════════════════════════════════════════════════
# PART 8: CLAUDE CODE PARAM ALIGNMENT
# ══════════════════════════════════════════════════════════

section("Claude Code param alignment")

cc_expected = {
    "filesystem.read":  {"file_path", "limit", "offset"},
    "filesystem.write": {"file_path", "content"},
    "filesystem.edit":  {"file_path", "old_string", "new_string", "replace_all"},
    "filesystem.grep":  {"pattern", "path", "glob", "context"},
    "filesystem.glob":  {"pattern", "path"},
    "shell.bash":       {"command", "description", "run_in_background"},
    "shell.bash_status": {"task_id", "kill"},
    "memory.remember":  {"content"},
    "memory.task_create": {"subject", "description"},
    "memory.task_update": {"taskId", "status"},
    "agent_spawn.agent": {"prompt", "description", "specialist", "wait"},
    "web.fetch":        {"url", "prompt", "extract"},
    "web.search":       {"query"},
}

for t in tools_ft["tools"]:
    fqn = t["fqn"]
    if fqn in cc_expected:
        actual = set(t["parameters"].get("properties", {}).keys())
        expected = cc_expected[fqn]
        missing = expected - actual
        check(f"{fqn} has CC params", len(missing) == 0, f"missing: {missing}")


# ══════════════════════════════════════════════════════════
# PART 9: SYSTEM PROMPT — Rules section present
# ══════════════════════════════════════════════════════════

section("Production system prompt — Rules section")
from digitorn.modules.context_builder.types import ToolIndex
from digitorn.modules.context_builder.prompt import build_system_prompt

idx = ToolIndex()
prompt = build_system_prompt(
    agent_id="test", role="You are a coding assistant.",
    user_prompt="Fix a bug", index=idx,
)
check("prompt has # Rules", "# Rules" in prompt)
check("prompt mentions TaskCreate", "TaskCreate" in prompt)
check("prompt mentions Edit", "Edit" in prompt)
check("prompt mentions Read", "Read" in prompt)
check("prompt no SetGoal", "SetGoal" not in prompt)
check("prompt no TodoAdd", "TodoAdd" not in prompt)


# ══════════════════════════════════════════════════════════
# CLEANUP & SUMMARY
# ══════════════════════════════════════════════════════════

try:
    shutil.rmtree(TMPDIR)
except Exception:
    pass

print("\n" + "=" * 55)
print(f"  PASSED: {passed}")
print(f"  FAILED: {failed}")
print(f"  TOTAL:  {passed + failed}")
print(f"  SECTIONS: {total_sections}")
print("=" * 55)

if failed > 0:
    sys.exit(1)
