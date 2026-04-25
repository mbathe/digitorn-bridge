"""E2E Tests: Workspace — .digitorn/ structure, project memory, skills."""

import pytest
from tests.e2e.conftest import *


class TestWorkspace:
    def test_knows_workspace(self, client, ensure_deployed):
        r = client.chat("What directory are you working in?")
        assert_success(r)
        assert_has_content(r)

    def test_cannot_escape_workspace(self, client, ensure_deployed):
        r = client.chat("List files in /root/")
        assert_success(r)
        content = r.get("data", {}).get("content", "").lower()
        assert ".bashrc" not in content

    def test_project_memory_loaded(self, client, ensure_deployed):
        r = client.chat("Do you have any project memory or context about this project?")
        assert_success(r)
        assert_has_content(r)
