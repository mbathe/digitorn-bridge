"""Tests for read-before-edit enforcement in the filesystem module."""

import pytest
from _test_helpers import run_coro

from digitorn.modules.filesystem.module import FilesystemModule
from digitorn.modules.filesystem.params import ReadParams, EditParams, WriteParams


@pytest.fixture
def fs(tmp_path):
    m = FilesystemModule()
    m._workspace_root = str(tmp_path)
    run_coro(
        m.on_config_update({"checkpoint": True})
    )
    # Create a test file
    f = tmp_path / "test.py"
    f.write_text("line1\nline2\nline3\n")
    return m, f


class TestReadBeforeEdit:
    def test_edit_without_read_fails(self, fs):
        m, f = fs
        r = run_coro(
            m.edit(EditParams(path=str(f), old_string="line1", new_string="changed"))
        )
        assert not r.success
        assert "not read" in r.error.lower() or "read the file first" in r.error.lower()

    def test_edit_after_read_passes(self, fs):
        m, f = fs
        # Read first
        run_coro(
            m.read(ReadParams(path=str(f)))
        )
        # Now edit should work
        r = run_coro(
            m.edit(EditParams(path=str(f), old_string="line1", new_string="changed"))
        )
        assert r.success

    def test_edit_wrong_old_string_shows_closest(self, fs):
        m, f = fs
        # Read first
        run_coro(
            m.read(ReadParams(path=str(f)))
        )
        # Edit with wrong old_string
        r = run_coro(
            m.edit(EditParams(path=str(f), old_string="lien1", new_string="changed"))
        )
        assert not r.success
        # Should show closest match
        assert "closest" in r.error.lower() or "similar" in r.error.lower() or "line" in r.error.lower()

    def test_write_does_not_require_read(self, fs):
        m, f = fs
        new_file = f.parent / "new.py"
        r = run_coro(
            m.write(WriteParams(path=str(new_file), content="new content"))
        )
        assert r.success


class TestCheckpoint:
    def test_write_creates_checkpoint(self, fs):
        m, f = fs
        # Read first (required by stale guard for existing files)
        run_coro(
            m.read(ReadParams(path=str(f)))
        )
        # Write over existing file
        r = run_coro(
            m.write(WriteParams(path=str(f), content="overwritten"))
        )
        assert r.success
        assert "checkpoint" in r.data

    def test_undo_restores(self, fs):
        m, f = fs
        original = f.read_text()
        # Read first (required by stale guard for existing files)
        run_coro(
            m.read(ReadParams(path=str(f)))
        )
        # Write over
        run_coro(
            m.write(WriteParams(path=str(f), content="overwritten"))
        )
        assert f.read_text() == "overwritten"
        # Undo
        from digitorn.modules.filesystem.params import UndoParams
        r = run_coro(
            m.undo(UndoParams(path=str(f)))
        )
        assert r.success
        assert f.read_text() == original

    def test_undo_without_checkpoint_fails(self, fs):
        m, f = fs
        from digitorn.modules.filesystem.params import UndoParams
        new_file = f.parent / "never_modified.py"
        new_file.write_text("original")
        r = run_coro(
            m.undo(UndoParams(path=str(new_file)))
        )
        assert not r.success
        assert "no checkpoint" in r.error.lower()

    def test_checkpoint_disabled(self, tmp_path):
        m = FilesystemModule()
        m._workspace_root = str(tmp_path)
        run_coro(
            m.on_config_update({"checkpoint": False})
        )
        f = tmp_path / "test.py"
        f.write_text("original")
        # Read first (required by stale guard for existing files)
        run_coro(
            m.read(ReadParams(path=str(f)))
        )
        r = run_coro(
            m.write(WriteParams(path=str(f), content="overwritten"))
        )
        assert r.success
        assert "checkpoint" not in r.data
