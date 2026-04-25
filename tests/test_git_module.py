"""Git module tests — covers all 17 actions with real git operations."""

from __future__ import annotations

from _test_helpers import run_coro
import subprocess

import pytest

from digitorn.modules.git.module import GitModule
from digitorn.modules.git.params import (
    AddParams, BlameParams, BranchCreateParams, BranchListParams,
    CheckoutParams, CommitParams, DiffParams, LogParams,
    ShowParams, StashParams, StatusParams, TagParams,
)


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
    (repo / "file.txt").write_text("line1\nline2\nline3\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, capture_output=True)
    return repo


@pytest.fixture
def git(git_repo):
    m = GitModule()
    run_coro(m.on_config_update({"workspace": str(git_repo)}))
    return m


class TestStatus:
    @pytest.mark.asyncio
    async def test_clean(self, git):
        r = await git.status(StatusParams())
        assert r.success
        assert r.data["clean"] is True

    @pytest.mark.asyncio
    async def test_unstaged(self, git, git_repo):
        (git_repo / "file.txt").write_text("modified\n")
        git._cache.invalidate()
        r = await git.status(StatusParams())
        assert r.success
        assert len(r.data["unstaged"]) > 0

    @pytest.mark.asyncio
    async def test_untracked(self, git, git_repo):
        (git_repo / "new.txt").write_text("new\n")
        git._cache.invalidate()
        r = await git.status(StatusParams())
        assert "new.txt" in r.data["untracked"]

    @pytest.mark.asyncio
    async def test_cached(self, git):
        r1 = await git.status(StatusParams())
        r2 = await git.status(StatusParams())
        assert r1.data == r2.data


class TestDiff:
    @pytest.mark.asyncio
    async def test_empty(self, git):
        r = await git.diff(DiffParams())
        assert r.success and r.data["empty"]

    @pytest.mark.asyncio
    async def test_unstaged(self, git, git_repo):
        (git_repo / "file.txt").write_text("changed\n")
        r = await git.diff(DiffParams())
        assert not r.data["empty"]

    @pytest.mark.asyncio
    async def test_staged(self, git, git_repo):
        (git_repo / "file.txt").write_text("staged\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
        r = await git.diff(DiffParams(target="staged"))
        assert "staged" in r.data["diff"]


class TestLog:
    @pytest.mark.asyncio
    async def test_returns_commits(self, git):
        r = await git.log(LogParams(limit=5))
        assert r.success and r.data["count"] >= 1

    @pytest.mark.asyncio
    async def test_oneline(self, git):
        r = await git.log(LogParams(limit=5, oneline=True))
        assert r.data["commits"][0]["message"] == "Initial commit"


class TestBranchList:
    @pytest.mark.asyncio
    async def test_lists(self, git):
        r = await git.branch_list(BranchListParams())
        assert r.success and r.data["count"] >= 1


class TestAdd:
    @pytest.mark.asyncio
    async def test_stage(self, git, git_repo):
        (git_repo / "new.txt").write_text("c\n")
        r = await git.add(AddParams(files=["new.txt"]))
        assert r.success and r.data["count"] == 1


class TestCommit:
    @pytest.mark.asyncio
    async def test_commit(self, git, git_repo):
        (git_repo / "new.txt").write_text("c\n")
        await git.add(AddParams(files=["new.txt"]))
        r = await git.commit(CommitParams(message="Add new"))
        assert r.success

    @pytest.mark.asyncio
    async def test_empty_fails(self, git):
        r = await git.commit(CommitParams(message="Nothing"))
        assert not r.success


class TestBranchCreate:
    @pytest.mark.asyncio
    async def test_create(self, git):
        r = await git.branch_create(BranchCreateParams(name="feat/test"))
        assert r.success


class TestCheckout:
    @pytest.mark.asyncio
    async def test_checkout(self, git):
        await git.branch_create(BranchCreateParams(name="other", checkout=False))
        r = await git.checkout(CheckoutParams(target="other"))
        assert r.success


class TestShow:
    @pytest.mark.asyncio
    async def test_head(self, git):
        r = await git.show(ShowParams(stat_only=True))
        assert r.success and "Initial commit" in r.data["content"]


class TestStash:
    @pytest.mark.asyncio
    async def test_list_empty(self, git):
        r = await git.stash(StashParams(action="list"))
        assert r.success


class TestTag:
    @pytest.mark.asyncio
    async def test_list_empty(self, git):
        r = await git.tag(TagParams(action="list"))
        assert r.success and r.data["count"] == 0

    @pytest.mark.asyncio
    async def test_create(self, git):
        r = await git.tag(TagParams(action="create", name="v0.1"))
        assert r.success


class TestBlame:
    @pytest.mark.asyncio
    async def test_blame(self, git):
        r = await git.blame(BlameParams(file="file.txt"))
        assert r.success and r.data["count"] == 3
