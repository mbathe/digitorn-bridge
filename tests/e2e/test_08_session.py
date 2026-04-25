"""E2E Tests: Session management — persistence, resume, multi-turn."""

import pytest
from tests.e2e.conftest import *


class TestSessionPersistence:
    def test_multi_turn_context(self, client, ensure_deployed):
        sid = f"e2e-session-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat("My name is TestUser.", session_id=sid)
        assert_success(r1)
        r2 = client.chat("What is my name?", session_id=sid)
        assert_success(r2)
        assert_content_contains(r2, "testuser")

    def test_file_context_persists(self, client, ensure_deployed):
        sid = f"e2e-ctx-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat("Read pyproject.toml", session_id=sid)
        assert_success(r1)
        r2 = client.chat("What was the project name from the file you just read?", session_id=sid)
        assert_success(r2)
        assert_content_contains(r2, "digitorn")

    def test_session_isolation(self, client, ensure_deployed):
        sid_a = f"e2e-iso-a-{__import__('uuid').uuid4().hex[:8]}"
        sid_b = f"e2e-iso-b-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat("Remember: the secret code is ALPHA123", session_id=sid_a)
        assert_success(r1)
        r2 = client.chat("What is the secret code?", session_id=sid_b)
        assert_success(r2)
        content = r2.get("data", {}).get("content", "").lower()
        assert "alpha123" not in content

    def test_session_list(self, client, ensure_deployed):
        sid = f"e2e-list-{__import__('uuid').uuid4().hex[:8]}"
        client.chat("Hello", session_id=sid)
        sessions = client.sessions()
        sids = [s["session_id"] for s in sessions]
        assert sid in sids


class TestMultiTurnWorkflow:
    def test_three_step_workflow(self, client, ensure_deployed):
        sid = f"e2e-workflow-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat("Step 1: List files in examples/", session_id=sid)
        assert_success(r1)
        assert_used_tools(r1)
        r2 = client.chat("Step 2: Read one of those YAML files", session_id=sid)
        assert_success(r2)
        assert_used_tools(r2)
        r3 = client.chat("Step 3: Tell me what type of app that YAML defines", session_id=sid)
        assert_success(r3)
        assert_has_content(r3)

    def test_error_recovery(self, client, ensure_deployed):
        sid = f"e2e-recover-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat("Read /nonexistent/file.py", session_id=sid)
        assert_success(r1)
        r2 = client.chat("That file doesn't exist. Try reading pyproject.toml instead.", session_id=sid)
        assert_success(r2)
        assert_used_tools(r2)
        assert_content_contains(r2, "digitorn")
