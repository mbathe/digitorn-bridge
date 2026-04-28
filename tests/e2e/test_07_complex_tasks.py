"""E2E Tests: Complex multi-step tasks - planning, execution, verification."""

import pytest
from tests.e2e.conftest import *


class TestCodeAnalysis:
    def test_analyze_file(self, client, ensure_deployed):
        r = client.chat("Read packages/digitorn/core/security.py and explain what it does.")
        assert_success(r)
        assert_used_tools(r)
        assert_content_contains(r, "security")

    def test_find_function(self, client, ensure_deployed):
        r = client.chat("Find the function 'security_gate' in the codebase and show me its signature.")
        assert_success(r)
        assert_used_tools(r)
        assert_content_contains(r, "security_gate")

    def test_count_lines(self, client, ensure_deployed):
        r = client.chat("How many Python files are in packages/digitorn/modules/? Just give me the count.")
        assert_success(r)
        assert_used_tools(r)

    def test_multi_file_analysis(self, client, ensure_deployed):
        r = client.chat(
            "Read packages/digitorn/core/server.py and packages/digitorn/core/config.py. "
            "Tell me what port the server listens on by default."
        )
        assert_success(r)
        assert_used_tools(r, min_calls=2)
        assert_content_contains(r, "8000")


class TestBugFixing:
    def test_identify_bug(self, client, ensure_deployed):
        sid = f"e2e-bug-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat(
            "Create /tmp/e2e_buggy.py with this content:\n"
            "def divide(a, b):\n    return a / b\n\n"
            "result = divide(10, 0)\nprint(result)",
            session_id=sid,
        )
        assert_success(r1)
        r2 = client.chat("Read /tmp/e2e_buggy.py and identify the bug.", session_id=sid)
        assert_success(r2)
        assert_content_contains(r2, "zero", "division")


class TestProjectExploration:
    def test_project_structure(self, client, ensure_deployed):
        r = client.chat("Explore the project structure and tell me the main directories.")
        assert_success(r)
        assert_used_tools(r)
        assert_content_contains(r, "packages")

    def test_readme_summary(self, client, ensure_deployed):
        r = client.chat("Read README.md and summarize the project in 2 sentences.")
        assert_success(r)
        assert_used_tools(r)
        assert_content_contains(r, "digitorn")

    def test_dependency_check(self, client, ensure_deployed):
        r = client.chat("Read pyproject.toml and list the main dependencies.")
        assert_success(r)
        assert_used_tools(r)
        assert_content_contains(r, "fastapi")

    def test_test_discovery(self, client, ensure_deployed):
        r = client.chat("How many test files are in the tests/ directory?")
        assert_success(r)
        assert_used_tools(r)


class TestCodeGeneration:
    def test_generate_and_run(self, client, ensure_deployed):
        sid = f"e2e-gen-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat(
            "Create /tmp/e2e_hello.py that prints 'E2E_SUCCESS'",
            session_id=sid,
        )
        assert_success(r1)
        r2 = client.chat("Run 'python /tmp/e2e_hello.py' and tell me the output.", session_id=sid)
        assert_success(r2)
        assert_content_contains(r2, "e2e_success")

    def test_generate_test(self, client, ensure_deployed):
        sid = f"e2e-gentest-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat(
            "Create /tmp/e2e_add.py with: def add(a, b): return a + b",
            session_id=sid,
        )
        assert_success(r1)
        r2 = client.chat(
            "Create /tmp/test_e2e_add.py with a pytest test for the add function. Then run it.",
            session_id=sid,
        )
        assert_success(r2)
        assert_used_tools(r2, min_calls=2)
