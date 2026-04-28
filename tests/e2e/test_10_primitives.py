"""E2E Tests: Context builder primitives - run_parallel, background, schedule, watch."""

import pytest
from tests.e2e.conftest import *


class TestRunParallel:
    def test_parallel_reads(self, client, ensure_deployed):
        r = client.chat("Read pyproject.toml and README.md in parallel and compare their first lines.")
        assert_success(r)
        assert_used_tools(r)

    def test_parallel_greps(self, client, ensure_deployed):
        r = client.chat("Search for 'import' in both packages/digitorn/core/server.py and packages/digitorn/core/config.py at the same time.")
        assert_success(r)
        assert_used_tools(r)


class TestSearchTools:
    def test_search_by_description(self, client, ensure_deployed):
        r = client.chat("Search for a tool that can read files.")
        assert_success(r)
        assert_used_tools(r)
        assert_content_contains(r, "read")

    def test_list_categories(self, client, ensure_deployed):
        r = client.chat("List all available tool categories.")
        assert_success(r)
        assert_used_tools(r)
        assert_content_contains(r, "filesystem")

    def test_browse_category(self, client, ensure_deployed):
        r = client.chat("Show me all tools in the filesystem category.")
        assert_success(r)
        assert_used_tools(r)
        assert_content_contains(r, "read", "write")

    def test_get_tool_info(self, client, ensure_deployed):
        r = client.chat("Get detailed info about the filesystem.grep tool.")
        assert_success(r)
        assert_used_tools(r)


class TestSkills:
    def test_list_skills(self, client, ensure_deployed):
        r = client.chat("What skills/commands are available?")
        assert_success(r)
        assert_has_content(r)
