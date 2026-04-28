"""Advanced E2E Tests - complex workflows that test the full power of Digitorn.

These tests simulate real-world tasks that require planning, delegation,
multi-step execution, error recovery, and context retention.

Each test sends a SINGLE complex prompt and verifies that the agent
produces a correct, complete result using multiple tools.
"""

import os
import uuid
import pytest
import httpx

DAEMON = os.environ.get("DIGITORN_TEST_DAEMON", "http://127.0.0.1:8000")
APP_ID = os.environ.get("DIGITORN_TEST_APP", "e2e-test")
KEY = os.environ.get("DEEPSEEK_API_KEY", "")
TIMEOUT = 600.0


def _chat(msg: str, sid: str = "") -> dict:
    sid = sid or f"adv-{uuid.uuid4().hex[:8]}"
    r = httpx.post(
        f"{DAEMON}/api/apps/{APP_ID}/chat",
        json={"session_id": sid, "message": msg},
        timeout=TIMEOUT,
    )
    return r.json()


def _ok(r): return r.get("success", False)
def _c(r): return r.get("data", {}).get("content", "")
def _t(r): return r.get("data", {}).get("tool_calls_count", 0)


@pytest.fixture(scope="session", autouse=True)
def check():
    try:
        r = httpx.get(f"{DAEMON}/health", timeout=5.0)
        if r.status_code != 200: pytest.skip("No daemon")
    except: pytest.skip("No daemon")


class TestMultiStepAnalysis:
    """Agent must read multiple files and synthesize findings."""

    def test_compare_two_files(self):
        r = _chat(
            "Read both packages/digitorn/core/security.py and packages/digitorn/core/security_audit.py. "
            "Tell me: how many classes are in each file, and what is the relationship between them?"
        )
        assert _ok(r) and _t(r) >= 2
        c = _c(r).lower()
        assert "security" in c
        assert any(w in c for w in ["class", "classes", "module"])

    def test_find_and_analyze_pattern(self):
        r = _chat(
            "Search the codebase for all files that contain 'async def on_config_update'. "
            "For each file found, tell me the module name. How many modules implement this method?"
        )
        assert _ok(r) and _t(r) >= 2
        c = _c(r).lower()
        assert any(w in c for w in ["filesystem", "shell", "module"])

    def test_cross_reference_schema_and_compiler(self):
        r = _chat(
            "Read packages/digitorn/core/app/schema.py and find the class ExecutionConfig. "
            "Then read packages/digitorn/core/app/compiler.py and find where ExecutionConfig is used. "
            "Tell me what fields ExecutionConfig has and how the compiler processes them."
        )
        assert _ok(r) and _t(r) >= 3
        c = _c(r).lower()
        assert "execution" in c

    def test_architecture_analysis(self):
        r = _chat(
            "I need to understand how the agent loop works. Read packages/digitorn/core/runtime/agent_loop.py "
            "(just the first 100 lines), then explain: what is the main function, what are its parameters, "
            "and what is the high-level flow?"
        )
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert any(w in c for w in ["agent", "turn", "loop", "tool"])

    def test_dependency_chain(self):
        r = _chat(
            "Find which modules import from digitorn.core.security. "
            "Grep for 'from digitorn.core.security import' across all Python files. "
            "List each file and what it imports."
        )
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert "import" in c


class TestPlanningAndExecution:
    """Agent must plan before acting and track progress."""

    def test_planned_audit(self):
        r = _chat(
            "Do a structured analysis of the packages/digitorn/core/app/ directory. "
            "Set a goal, create a plan with at least 3 steps, then execute it. "
            "For each file, tell me: filename, number of lines, main classes/functions."
        )
        assert _ok(r) and _t(r) >= 5
        c = _c(r).lower()
        assert any(w in c for w in ["compiler", "schema", "manager"])

    def test_goal_driven_exploration(self):
        r = _chat(
            "Your goal is to understand the memory system. "
            "1) Find all files related to memory (grep for 'memory' in module paths). "
            "2) Read the main memory module. "
            "3) List all memory actions available. "
            "Report your findings."
        )
        assert _ok(r) and _t(r) >= 3
        c = _c(r).lower()
        assert any(w in c for w in ["goal", "fact", "todo", "memory"])

    def test_investigate_and_report(self):
        r = _chat(
            "Investigate how the security gate works end-to-end. "
            "Find the security_gate function, read it, then find where it's called. "
            "Produce a summary of the flow: what checks are performed, in what order."
        )
        assert _ok(r) and _t(r) >= 3
        c = _c(r).lower()
        assert any(w in c for w in ["gate", "check", "deny", "grant", "risk"])


class TestErrorRecoveryAndAdaptation:
    """Agent must handle errors gracefully and adapt."""

    def test_file_not_found_then_search(self):
        r = _chat(
            "Read the file packages/digitorn/core/authentication.py. "
            "If it doesn't exist, find the actual auth-related files and read one of them."
        )
        assert _ok(r) and _t(r) >= 2
        c = _c(r).lower()
        assert any(w in c for w in ["auth", "not found", "instead"])

    def test_wrong_path_recovery(self):
        r = _chat(
            "Read src/main.py. If that path doesn't work, explore the project structure "
            "to find the actual main entry point and read it."
        )
        assert _ok(r) and _t(r) >= 2
        c = _c(r)
        assert len(c) > 20

    def test_too_large_file_adaptation(self):
        r = _chat(
            "Read packages/digitorn/modules/mcp/module.py. It's very large. "
            "If the full content is too big, read just the first 50 lines and "
            "the list of action methods (grep for '@action')."
        )
        assert _ok(r) and _t(r) >= 1
        c = _c(r)
        assert len(c) > 50


class TestMultiToolCoordination:
    """Agent must combine multiple tool types in one task."""

    def test_read_and_run(self):
        sid = f"adv-rr-{uuid.uuid4().hex[:8]}"
        r = _chat(
            "Read tests/test_pipeline.py, count the number of test functions, "
            "then run 'python -m pytest tests/test_pipeline.py -q' and tell me if all tests pass.",
            sid=sid,
        )
        assert _ok(r) and _t(r) >= 2
        c = _c(r).lower()
        assert "pass" in c or "test" in c

    def test_git_and_filesystem(self):
        r = _chat(
            "Show git status, then for each modified file listed, "
            "read the first 5 lines of that file. Summarize what changed."
        )
        assert _ok(r) and _t(r) >= 2

    def test_grep_read_analyze(self):
        r = _chat(
            "Grep for 'TODO' in packages/digitorn/core/. "
            "For each file that has TODOs, read the surrounding lines. "
            "Compile a list of all TODOs with their file and line number."
        )
        assert _ok(r) and _t(r) >= 2

    def test_find_count_report(self):
        r = _chat(
            "Find all .yaml files in examples/. "
            "Count them. Read the first 3 lines of each to determine what type of app it is. "
            "Report: filename, app type (chat/one_shot/background), model used."
        )
        assert _ok(r) and _t(r) >= 3
        c = _c(r).lower()
        assert "yaml" in c or ".yaml" in c


class TestContextRetention:
    """Agent must remember across turns in the same session."""

    def test_three_turn_analysis(self):
        sid = f"adv-3turn-{uuid.uuid4().hex[:8]}"
        r1 = _chat("Read pyproject.toml and tell me all the dependencies.", sid=sid)
        assert _ok(r1) and _t(r1) >= 1

        r2 = _chat("Which of those dependencies are for testing?", sid=sid)
        assert _ok(r2)
        c2 = _c(r2).lower()
        assert "pytest" in c2

        r3 = _chat("And which ones are for the web framework?", sid=sid)
        assert _ok(r3)
        c3 = _c(r3).lower()
        assert "fastapi" in c3 or "uvicorn" in c3

    def test_progressive_exploration(self):
        sid = f"adv-prog-{uuid.uuid4().hex[:8]}"
        r1 = _chat("List the top-level directories in the project.", sid=sid)
        assert _ok(r1) and _t(r1) >= 1

        r2 = _chat("Now go deeper into packages/digitorn/ and list its contents.", sid=sid)
        assert _ok(r2) and _t(r2) >= 1

        r3 = _chat("How many modules are in packages/digitorn/modules/?", sid=sid)
        assert _ok(r3) and _t(r3) >= 1
        c3 = _c(r3)
        assert any(c.isdigit() for c in c3)

    def test_fact_retention(self):
        sid = f"adv-fact-{uuid.uuid4().hex[:8]}"
        r1 = _chat("Read pyproject.toml and remember the version number as a fact.", sid=sid)
        assert _ok(r1) and _t(r1) >= 1

        r2 = _chat("What version did you find earlier? Don't read the file again, use your memory.", sid=sid)
        assert _ok(r2)
        assert "1.0" in _c(r2)


class TestDelegation:
    """Agent must delegate to specialists when appropriate."""

    def test_spawn_for_exploration(self):
        r = _chat(
            "I need a comprehensive list of all @action-decorated methods in the filesystem module. "
            "Spawn an explore agent to find them all."
        )
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert any(w in c for w in ["read", "write", "edit", "grep", "action", "spawn", "agent"])

    def test_parallel_investigation(self):
        r = _chat(
            "I need to understand two modules at once. "
            "Investigate packages/digitorn/modules/filesystem/module.py and "
            "packages/digitorn/modules/git/module.py. "
            "Use parallel execution or delegation to analyze both."
        )
        assert _ok(r) and _t(r) >= 2
        c = _c(r).lower()
        assert "filesystem" in c or "git" in c


class TestRealWorldScenarios:
    """Simulates real tasks a developer would ask."""

    def test_onboarding_question(self):
        r = _chat(
            "I'm new to this project. Can you give me a high-level overview? "
            "Check the README, the project structure, and pyproject.toml."
        )
        assert _ok(r) and _t(r) >= 2
        c = _c(r).lower()
        assert any(w in c for w in ["digitorn", "framework", "agent", "yaml"])

    def test_find_bug_pattern(self):
        r = _chat(
            "Check if there are any bare 'except:' statements (without specifying the exception type) "
            "in packages/digitorn/core/. This is a bad practice. Report what you find."
        )
        assert _ok(r) and _t(r) >= 1

    def test_test_coverage_check(self):
        r = _chat(
            "List all test files in tests/. Then list all modules in packages/digitorn/modules/. "
            "Which modules have dedicated test files and which don't?"
        )
        assert _ok(r) and _t(r) >= 2
        c = _c(r).lower()
        assert any(w in c for w in ["test", "module", "coverage"])

    def test_config_review(self):
        r = _chat(
            "Read the app configuration at examples/claude-code.yaml. "
            "Tell me: how many agents are defined, what modules are used, "
            "and what security capabilities are configured?"
        )
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert any(w in c for w in ["agent", "module", "security", "grant"])

    def test_run_specific_test(self):
        r = _chat(
            "Run the test file tests/test_pipeline.py and tell me: "
            "how many tests, how many passed, how many failed, total time."
        )
        assert _ok(r) and _t(r) >= 1
        c = _c(r).lower()
        assert "pass" in c


class TestStressCases:
    """The hardest tests - push the system to its limits."""

    def test_full_project_audit(self):
        """Agent must analyze the entire project structure and produce a report."""
        r = _chat(
            "Perform a complete audit of this project. I need: "
            "1) Project structure (main directories and their purpose), "
            "2) Number of Python files and total lines of code, "
            "3) Number of modules and their names, "
            "4) Number of test files, "
            "5) The main entry point. "
            "Use grep, find, and file reading as needed."
        )
        assert _ok(r) and _t(r) >= 5
        c = _c(r).lower()
        assert "packages" in c
        assert any(w in c for w in ["module", "test", "file"])

    def test_debug_workflow(self):
        """Agent must find a function, read it, identify potential issues."""
        r = _chat(
            "Find the function '_resolve_template' in the codebase. "
            "Read its implementation. Analyze it for: "
            "1) Edge cases not handled, "
            "2) Potential regex issues, "
            "3) What happens with malformed input. "
            "Give a concrete assessment."
        )
        assert _ok(r) and _t(r) >= 2
        c = _c(r).lower()
        assert any(w in c for w in ["template", "regex", "resolve", "step"])

    def test_comparison_workflow(self):
        """Compare two similar modules and report differences."""
        r = _chat(
            "Compare the git module (packages/digitorn/modules/git/module.py) "
            "with the filesystem module (packages/digitorn/modules/filesystem/module.py). "
            "How many actions does each have? Which is larger? "
            "What patterns do they share?"
        )
        assert _ok(r) and _t(r) >= 2
        c = _c(r).lower()
        assert "git" in c and "filesystem" in c

    def test_write_and_verify(self):
        """Agent must create a file, then verify its content."""
        sid = f"adv-wv-{uuid.uuid4().hex[:8]}"
        r = _chat(
            "Create a file /tmp/e2e_advanced_test.py with this content:\n"
            "def greet(name):\n    return f'Hello, {name}!'\n\n"
            "Then read it back and run: python -c \"from e2e_advanced_test import greet; print(greet('World'))\" "
            "Tell me the output.",
            sid=sid,
        )
        assert _ok(r) and _t(r) >= 2

    def test_multi_session_independence(self):
        """Two sessions must not leak data."""
        sid_a = f"adv-iso-a-{uuid.uuid4().hex[:8]}"
        sid_b = f"adv-iso-b-{uuid.uuid4().hex[:8]}"
        r1 = _chat("Remember this as a fact: the secret project name is PHOENIX", sid=sid_a)
        assert _ok(r1)
        r2 = _chat("What is the secret project name?", sid=sid_b)
        assert _ok(r2)
        assert "phoenix" not in _c(r2).lower()
