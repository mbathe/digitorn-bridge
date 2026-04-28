"""E2E Tests: Basic conversation - no tools needed."""

import pytest
from tests.e2e.conftest import *


class TestBasicConversation:
    """Simple questions that should not require tool calls."""

    def test_simple_math(self, client, ensure_deployed):
        r = client.chat("What is 2+2? Answer with just the number.")
        assert_success(r)
        assert_has_content(r)
        assert_content_contains(r, "4")

    def test_greeting(self, client, ensure_deployed):
        r = client.chat("Hello!")
        assert_success(r)
        assert_has_content(r)

    def test_explain_concept(self, client, ensure_deployed):
        r = client.chat("Explain what a Python decorator is in one sentence.")
        assert_success(r)
        assert_has_content(r)
        assert_content_contains(r, "decorator")

    def test_code_generation(self, client, ensure_deployed):
        r = client.chat("Write a Python function that reverses a string. Just the code, nothing else.")
        assert_success(r)
        assert_has_content(r)
        assert_content_contains(r, "def")

    def test_translation(self, client, ensure_deployed):
        r = client.chat("Translate 'hello world' to French.")
        assert_success(r)
        assert_has_content(r)
        assert_has_content(r)

    def test_list_request(self, client, ensure_deployed):
        r = client.chat("List 3 Python web frameworks.")
        assert_success(r)
        assert_has_content(r)
        assert_content_contains(r, "flask", "django")

    def test_yes_no_question(self, client, ensure_deployed):
        r = client.chat("Is Python a compiled language? Answer yes or no.")
        assert_success(r)
        assert_has_content(r)
        assert_content_contains(r, "no")

    def test_comparison(self, client, ensure_deployed):
        r = client.chat("What is the difference between a list and a tuple in Python? One sentence.")
        assert_success(r)
        assert_has_content(r)

    def test_empty_response_not_allowed(self, client, ensure_deployed):
        r = client.chat("Say exactly: OK")
        assert_success(r)
        assert_has_content(r)

    def test_long_response(self, client, ensure_deployed):
        r = client.chat("Explain the SOLID principles briefly.")
        assert_success(r)
        content = r.get("data", {}).get("content", "")
        assert len(content) > 50
