"""E2E Tests: Filesystem module - read, write, edit, grep, find, ls."""

import pytest
from tests.e2e.conftest import *


class TestFilesystemRead:
    def test_read_file(self, client, ensure_deployed):
        r = client.chat("Read pyproject.toml and tell me the project name.")
        assert_success(r)
        assert_used_tools(r)
        assert_content_contains(r, "digitorn")

    def test_read_with_line_numbers(self, client, ensure_deployed):
        r = client.chat("Read the first 5 lines of pyproject.toml.")
        assert_success(r)
        assert_used_tools(r)
        assert_has_content(r)

    def test_read_nonexistent_file(self, client, ensure_deployed):
        r = client.chat("Read the file nonexistent_xyz_123.py and tell me what's in it.")
        assert_success(r)
        assert_used_tools(r)

    def test_read_specific_section(self, client, ensure_deployed):
        r = client.chat("Read packages/digitorn/core/server.py lines 1-10 and tell me the version.")
        assert_success(r)
        assert_used_tools(r)


class TestFilesystemLs:
    def test_list_root(self, client, ensure_deployed):
        r = client.chat("List the files in the current directory.")
        assert_success(r)
        assert_used_tools(r)
        assert_content_contains(r, "pyproject")

    def test_list_subdirectory(self, client, ensure_deployed):
        r = client.chat("List the files in packages/digitorn/modules/")
        assert_success(r)
        assert_used_tools(r)
        assert_content_contains(r, "filesystem")

    def test_list_nonexistent(self, client, ensure_deployed):
        r = client.chat("List files in /nonexistent/directory/")
        assert_success(r)
        assert_used_tools(r)


class TestFilesystemGrep:
    def test_grep_pattern(self, client, ensure_deployed):
        r = client.chat("Search for 'class FilesystemModule' in packages/digitorn/modules/filesystem/")
        assert_success(r)
        assert_used_tools(r)
        assert_content_contains(r, "module.py")

    def test_grep_across_files(self, client, ensure_deployed):
        r = client.chat("Search for '__version__' in packages/digitorn/core/server.py.")
        assert_success(r)
        assert_used_tools(r)

    def test_grep_no_results(self, client, ensure_deployed):
        r = client.chat("Search for 'ZZZZZ_NONEXISTENT_PATTERN_12345' in packages/")
        assert_success(r)
        assert_used_tools(r)


class TestFilesystemFind:
    def test_find_by_pattern(self, client, ensure_deployed):
        r = client.chat("Find all Python files named 'module.py' in packages/")
        assert_success(r)
        assert_used_tools(r)
        assert_content_contains(r, "module.py")

    def test_find_yaml_files(self, client, ensure_deployed):
        r = client.chat("Find all YAML files in examples/")
        assert_success(r)
        assert_used_tools(r)
        assert_content_contains(r, ".yaml")


class TestFilesystemWrite:
    def test_create_and_read_back(self, client, ensure_deployed):
        sid = f"e2e-write-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat("Create a file called /tmp/digitorn_e2e_test.txt with the content 'E2E test passed'", session_id=sid)
        assert_success(r1)
        assert_used_tools(r1)
        r2 = client.chat("Now read /tmp/digitorn_e2e_test.txt and tell me what it says.", session_id=sid)
        assert_success(r2)
        assert_used_tools(r2)
        assert_content_contains(r2, "e2e test passed")

    def test_edit_file(self, client, ensure_deployed):
        sid = f"e2e-edit-{__import__('uuid').uuid4().hex[:8]}"
        r1 = client.chat("Create /tmp/digitorn_e2e_edit.txt with content 'old value'", session_id=sid)
        assert_success(r1)
        r2 = client.chat("Edit /tmp/digitorn_e2e_edit.txt: replace 'old value' with 'new value'", session_id=sid)
        assert_success(r2)
        assert_used_tools(r2)
        r3 = client.chat("Read /tmp/digitorn_e2e_edit.txt", session_id=sid)
        assert_success(r3)
        assert_content_contains(r3, "new value")


class TestFilesystemSecurity:
    def test_cannot_read_outside_workspace(self, client, ensure_deployed):
        r = client.chat("Read /etc/passwd")
        assert_success(r)
        content = r.get("data", {}).get("content", "").lower()
        assert "root:" not in content

    def test_cannot_list_outside_workspace(self, client, ensure_deployed):
        r = client.chat("List files in /etc/")
        assert_success(r)
        content = r.get("data", {}).get("content", "").lower()
        assert "shadow" not in content
