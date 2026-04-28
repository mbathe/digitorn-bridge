"""E2E Tests: Web module - search, fetch, extract."""

import pytest
from tests.e2e.conftest import *


class TestWebSearch:
    def test_search(self, client, ensure_deployed):
        r = client.chat("Search the web for 'Python asyncio tutorial'")
        assert_success(r)
        assert_used_tools(r)

    def test_search_and_read(self, client, ensure_deployed):
        r = client.chat("Search for 'FastAPI documentation' and fetch the first result.")
        assert_success(r)
        assert_used_tools(r)


class TestWebFetch:
    def test_fetch_docs(self, client, ensure_deployed):
        r = client.chat("Fetch https://docs.python.org/3/ and tell me the Python version mentioned.")
        assert_success(r)
        assert_used_tools(r)

    def test_fetch_with_extraction(self, client, ensure_deployed):
        r = client.chat("Extract the main content from https://example.com")
        assert_success(r)
        assert_used_tools(r)
