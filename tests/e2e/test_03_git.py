"""E2E Tests: Git module — status, log, diff, blame, branches."""

import pytest
from tests.e2e.conftest import *


class TestGitRead:
    def test_git_status(self, client, ensure_deployed):
        r = client.chat("Show the git status of the project.")
        assert_success(r)
        assert_used_tools(r)
        assert_has_content(r)

    def test_git_log(self, client, ensure_deployed):
        r = client.chat("Show the last 3 git commits.")
        assert_success(r)
        assert_used_tools(r)
        assert_has_content(r)

    def test_git_diff(self, client, ensure_deployed):
        r = client.chat("Show the current git diff.")
        assert_success(r)
        assert_used_tools(r)

    def test_git_branches(self, client, ensure_deployed):
        r = client.chat("List all git branches.")
        assert_success(r)
        assert_used_tools(r)
        assert_content_contains(r, "main")

    def test_git_blame(self, client, ensure_deployed):
        r = client.chat("Show git blame for pyproject.toml lines 1-5.")
        assert_success(r)
        assert_used_tools(r)

    def test_git_show_commit(self, client, ensure_deployed):
        r = client.chat("Show details of the latest commit.")
        assert_success(r)
        assert_used_tools(r)
