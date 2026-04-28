"""E2E Tests: Edge cases - empty input, very long input, special characters, error recovery."""

import pytest
from tests.e2e.conftest import *


class TestEdgeCases:
    def test_very_short_input(self, client, ensure_deployed):
        r = client.chat("Hi")
        assert_success(r)
        assert_has_content(r)

    def test_unicode_input(self, client, ensure_deployed):
        r = client.chat("Comment dit-on 'bonjour' en japonais ?")
        assert_success(r)
        assert_has_content(r)

    def test_code_in_message(self, client, ensure_deployed):
        r = client.chat("What does this code do? `for i in range(10): print(i)`")
        assert_success(r)
        assert_has_content(r)
        assert_content_contains(r, "print", "loop")

    def test_json_in_message(self, client, ensure_deployed):
        r = client.chat('Parse this JSON: {"name": "test", "value": 42}')
        assert_success(r)
        assert_has_content(r)

    def test_multiline_code(self, client, ensure_deployed):
        r = client.chat(
            "Explain this code:\n"
            "```python\n"
            "def fib(n):\n"
            "    if n <= 1: return n\n"
            "    return fib(n-1) + fib(n-2)\n"
            "```"
        )
        assert_success(r)
        assert_has_content(r)
        assert_content_contains(r, "fibonacci")

    def test_rapid_succession(self, client, ensure_deployed):
        results = []
        for i in range(3):
            r = client.chat(f"What is {i+1} + {i+1}?")
            results.append(r)
        for r in results:
            assert_success(r)
            assert_has_content(r)

    def test_contradictory_request(self, client, ensure_deployed):
        r = client.chat("Read the file pyproject.toml but do NOT use any tools.")
        assert_success(r)
        assert_has_content(r)

    def test_ambiguous_request(self, client, ensure_deployed):
        r = client.chat("Check the project.")
        assert_success(r)
        assert_has_content(r)
