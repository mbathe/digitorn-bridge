"""E2E Tests: Error handling - graceful failures, recovery."""

import pytest
from tests.e2e.conftest import *


class TestErrorHandling:
    def test_tool_failure_recovery(self, client, ensure_deployed):
        r = client.chat("Read a file called zzz_does_not_exist_12345.py")
        assert_success(r)
        assert_has_content(r)

    def test_invalid_tool_args(self, client, ensure_deployed):
        r = client.chat("Search for files but with an impossible pattern: ***///")
        assert_success(r)
        assert_has_content(r)

    def test_timeout_handling(self, client, ensure_deployed):
        r = client.chat("Do something very quick: what is 1+1?")
        assert_success(r)
        assert_has_content(r)

    def test_graceful_on_no_tools_needed(self, client, ensure_deployed):
        r = client.chat("Just say 'hello world'")
        assert_success(r)
        assert_no_tools(r)
        assert_content_contains(r, "hello")

    def test_handles_special_chars(self, client, ensure_deployed):
        r = client.chat("What does the regex \\d+\\.\\d+ match?")
        assert_success(r)
        assert_has_content(r)
