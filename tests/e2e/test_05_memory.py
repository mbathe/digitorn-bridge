"""E2E Tests: Memory module - goal, plan, todos, facts, notes, checkpoints."""

import pytest
from tests.e2e.conftest import *


class TestMemoryGoalPlan:
    def test_set_goal_and_plan(self, client, ensure_deployed):
        sid = f"e2e-mem-{__import__('uuid').uuid4().hex[:8]}"
        r = client.chat(
            "I want you to analyze pyproject.toml. Set a goal and create a plan with 3 steps.",
            session_id=sid,
        )
        assert_success(r)
        assert_used_tools(r)

    def test_goal_persists_in_session(self, client, ensure_deployed):
        sid = f"e2e-mem-persist-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat("Set your goal to: Find all security issues", session_id=sid)
        assert_success(r1)
        r2 = client.chat("What is your current goal?", session_id=sid)
        assert_success(r2)
        assert_content_contains(r2, "security")


class TestMemoryTodos:
    def test_create_todo(self, client, ensure_deployed):
        sid = f"e2e-todo-{__import__('uuid').uuid4().hex[:8]}"
        r = client.chat("Add a task: Read the README file", session_id=sid)
        assert_success(r)
        assert_used_tools(r)

    def test_complete_todo(self, client, ensure_deployed):
        sid = f"e2e-todo-done-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat("Add a task: Check Python version", session_id=sid)
        assert_success(r1)
        r2 = client.chat("Run 'python --version' then mark the task as done.", session_id=sid)
        assert_success(r2)
        assert_used_tools(r2)


class TestMemoryFacts:
    def test_add_fact(self, client, ensure_deployed):
        sid = f"e2e-fact-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat("Read pyproject.toml and remember the project version as a fact.", session_id=sid)
        assert_success(r1)
        assert_used_tools(r1)

    def test_recall_fact(self, client, ensure_deployed):
        sid = f"e2e-recall-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat("Remember this fact: The database uses PostgreSQL", session_id=sid)
        assert_success(r1)
        r2 = client.chat("What database does the project use?", session_id=sid)
        assert_success(r2)
        assert_content_contains(r2, "postgresql")


class TestMemoryNotes:
    def test_sticky_note(self, client, ensure_deployed):
        sid = f"e2e-note-{__import__('uuid').uuid4().hex[:8]}"
        r = client.chat("Add a sticky note: Remember to run tests before committing", session_id=sid)
        assert_success(r)
        assert_used_tools(r)
