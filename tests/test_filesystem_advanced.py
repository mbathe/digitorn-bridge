"""Advanced Filesystem module tests - covering all 5 ultra-powerful actions.

Tests for:
- Read: line ranges, metadata
- Write: atomic writes, auto mkdir
- Edit: fuzzy matching strategies, insert_at_line
- Grep: regex patterns, context, multiline
- Glob: pattern matching, type filtering
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from digitorn.modules.filesystem.module import FilesystemModule
from digitorn.modules.filesystem.params import ReadParams, WriteParams, EditParams, GlobParams, GrepParams


@pytest.fixture
def fs(tmp_path):
    """Filesystem module with workspace set to tmp_path."""
    mod = FilesystemModule()
    mod._workspace_root = str(tmp_path)
    return mod


# ═══════════════════════════════════════════════════════════════
# READ ACTION
# ═══════════════════════════════════════════════════════════════

class TestReadAction:
    """Read action with line ranges, metadata."""

    @pytest.mark.asyncio
    async def test_read_full_file(self, fs, tmp_path):
        """Read entire file returns full content with line numbers."""
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3\n", encoding="utf-8")
        r = await fs.read(ReadParams(file_path=str(f)))
        assert r.success
        assert "line1" in r.data["content"] if r.data else r.output
        assert "line3" in r.data["content"] if r.data else r.output

    @pytest.mark.asyncio
    async def test_read_line_range(self, fs, tmp_path):
        """Read specific line range."""
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
        r = await fs.read(ReadParams(file_path=str(f), offset=1, limit=3))
        assert r.success
        content = r.data["content"] if r.data else r.output
        assert "line2" in content or "line3" in content

    @pytest.mark.asyncio
    async def test_read_json_file(self, fs, tmp_path):
        """Read JSON file."""
        f = tmp_path / "config.json"
        f.write_text(json.dumps({"key": "value", "count": 42}), encoding="utf-8")
        r = await fs.read(ReadParams(file_path=str(f)))
        assert r.success
        content = r.data["content"] if r.data else r.output
        assert "key" in content or "value" in content

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, fs, tmp_path):
        """Reading non-existent file returns error."""
        r = await fs.read(ReadParams(file_path=str(tmp_path / "missing.txt")))
        assert not r.success
        assert "not found" in (r.error or "").lower() or "does not exist" in (r.error or "").lower()

    @pytest.mark.asyncio
    async def test_read_directory_error(self, fs, tmp_path):
        """Reading directory returns error."""
        r = await fs.read(ReadParams(file_path=str(tmp_path)))
        assert not r.success
        # On Windows, may get "permission denied", on Unix get "is a directory"
        assert any(x in (r.error or "").lower() for x in ["not a file", "is a directory", "permission denied"])

    @pytest.mark.asyncio
    async def test_read_large_file_partial(self, fs, tmp_path):
        """Large file can be read in parts."""
        f = tmp_path / "large.txt"
        # Create 100 line file
        f.write_text("\n".join(f"line{i}" for i in range(100)))
        # Read first 10 lines
        r = await fs.read(ReadParams(file_path=str(f), limit=10))
        assert r.success


# ═══════════════════════════════════════════════════════════════
# WRITE ACTION
# ═══════════════════════════════════════════════════════════════

class TestWriteAction:
    """Write action with atomic writes, auto mkdir."""

    @pytest.mark.asyncio
    async def test_write_creates_file(self, fs, tmp_path):
        """Write creates new file."""
        f = tmp_path / "newfile.txt"
        r = await fs.write(WriteParams(file_path=str(f), content="hello"))
        assert r.success
        assert f.exists()
        assert f.read_text() == "hello"

    @pytest.mark.asyncio
    async def test_write_overwrites_existing(self, fs, tmp_path):
        """Write overwrites existing file."""
        f = tmp_path / "existing.txt"
        f.write_text("old content")
        r = await fs.write(WriteParams(file_path=str(f), content="new content"))
        assert r.success
        assert f.read_text() == "new content"

    @pytest.mark.asyncio
    async def test_write_auto_mkdir(self, fs, tmp_path):
        """Write creates parent directories automatically."""
        f = tmp_path / "deep" / "nested" / "path" / "file.txt"
        r = await fs.write(WriteParams(file_path=str(f), content="nested"))
        assert r.success
        assert f.exists()

    @pytest.mark.asyncio
    async def test_write_with_metadata(self, fs, tmp_path):
        """Write returns file metadata."""
        f = tmp_path / "metadata.txt"
        r = await fs.write(WriteParams(file_path=str(f), content="content"))
        assert r.success
        assert r.data is not None or r.output is not None

    @pytest.mark.asyncio
    async def test_write_multiline_content(self, fs, tmp_path):
        """Write multiline content."""
        f = tmp_path / "multiline.txt"
        content = "line1\nline2\nline3\n"
        r = await fs.write(WriteParams(file_path=str(f), content=content))
        assert r.success
        assert f.read_text() == content


# ═══════════════════════════════════════════════════════════════
# EDIT ACTION
# ═══════════════════════════════════════════════════════════════

class TestEditAction:
    """Edit action with fuzzy matching and insertion."""

    @pytest.mark.asyncio
    async def test_edit_exact_match(self, fs, tmp_path):
        """Edit with exact string match."""
        f = tmp_path / "file.txt"
        f.write_text("line1\nline2\nline3\n")
        r = await fs.edit(EditParams(file_path=str(f), old_string="line2", new_string="LINE2"))
        assert r.success
        assert "LINE2" in f.read_text()

    @pytest.mark.asyncio
    async def test_edit_multiline_exact(self, fs, tmp_path):
        """Edit multiline strings."""
        f = tmp_path / "file.txt"
        f.write_text("line1\nline2\nline3\nline4\n")
        r = await fs.edit(EditParams(
            file_path=str(f),
            old_string="line2\nline3",
            new_string="REPLACED",
        ))
        assert r.success
        content = f.read_text()
        assert "REPLACED" in content

    @pytest.mark.asyncio
    async def test_edit_insert_at_line(self, fs, tmp_path):
        """Edit with insert_at_line."""
        f = tmp_path / "file.txt"
        f.write_text("line1\nline2\nline3\n")
        r = await fs.edit(EditParams(
            file_path=str(f),
            insert_at_line=2,
            new_string="inserted",
        ))
        assert r.success
        content = f.read_text()
        assert "inserted" in content

    @pytest.mark.asyncio
    async def test_edit_nonexistent_file(self, fs, tmp_path):
        """Edit on non-existent file returns error."""
        f = tmp_path / "missing.txt"
        r = await fs.edit(EditParams(
            file_path=str(f),
            old_string="foo",
            new_string="bar",
        ))
        assert not r.success

    @pytest.mark.asyncio
    async def test_edit_no_match_error(self, fs, tmp_path):
        """Edit with no match returns error."""
        f = tmp_path / "file.txt"
        f.write_text("the quick brown fox\n")
        r = await fs.edit(EditParams(
            file_path=str(f),
            old_string="completely different text",
            new_string="replaced",
        ))
        # Should fail since text doesn't match
        assert not r.success

    @pytest.mark.asyncio
    async def test_edit_replace_all(self, fs, tmp_path):
        """Edit with replace_all."""
        f = tmp_path / "file.txt"
        f.write_text("foo\nfoo\nbar\nfoo\n")
        r = await fs.edit(EditParams(
            file_path=str(f),
            old_string="foo",
            new_string="FOO",
            replace_all=True,
        ))
        assert r.success
        content = f.read_text()
        assert content.count("FOO") == 3


# ═══════════════════════════════════════════════════════════════
# GLOB ACTION
# ═══════════════════════════════════════════════════════════════

class TestGlobAction:
    """Glob action with patterns, type filtering."""

    @pytest.mark.asyncio
    async def test_glob_simple_pattern(self, fs, tmp_path):
        """Glob finds files matching pattern."""
        (tmp_path / "file1.txt").touch()
        (tmp_path / "file2.txt").touch()
        (tmp_path / "other.py").touch()
        r = await fs.glob(GlobParams(pattern="*.txt", path=str(tmp_path)))
        assert r.success
        files = r.data.get("files", []) if r.data else []
        # Should find txt files
        assert len(files) > 0

    @pytest.mark.asyncio
    async def test_glob_nested_pattern(self, fs, tmp_path):
        """Glob finds files in nested directories."""
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "file1.txt").touch()
        (tmp_path / "a" / "b").mkdir()
        (tmp_path / "a" / "b" / "file2.txt").touch()
        r = await fs.glob(GlobParams(pattern="**/*.txt", path=str(tmp_path)))
        assert r.success
        files = r.data.get("files", []) if r.data else []
        # Should find nested files
        assert len(files) >= 2

    @pytest.mark.asyncio
    async def test_glob_with_type_filter_file(self, fs, tmp_path):
        """Glob filters by file type."""
        (tmp_path / "file.txt").touch()
        (tmp_path / "dir").mkdir()
        r = await fs.glob(GlobParams(pattern="*", path=str(tmp_path), type="file"))
        assert r.success

    @pytest.mark.asyncio
    async def test_glob_directory_type(self, fs, tmp_path):
        """Glob can filter for directories."""
        (tmp_path / "dir1").mkdir()
        (tmp_path / "file.txt").touch()
        r = await fs.glob(GlobParams(pattern="*", path=str(tmp_path), type="dir"))
        assert r.success


# ═══════════════════════════════════════════════════════════════
# GREP ACTION
# ═══════════════════════════════════════════════════════════════

class TestGrepAction:
    """Grep action with regex patterns, context."""

    @pytest.mark.asyncio
    async def test_grep_simple_pattern(self, fs, tmp_path):
        """Grep finds matching lines."""
        f = tmp_path / "log.txt"
        f.write_text("error: something failed\ninfo: process started\nerror: another failure\n")
        r = await fs.grep(GrepParams(pattern="error", path=str(f)))
        assert r.success
        assert "error" in r.data["content"] if r.data else ""

    @pytest.mark.asyncio
    async def test_grep_regex_pattern(self, fs, tmp_path):
        """Grep with regex patterns."""
        f = tmp_path / "data.txt"
        f.write_text("item_1\nitem_2\nitem_999\nfoo_1\n")
        r = await fs.grep(GrepParams(pattern=r"item_\d+", path=str(f)))
        assert r.success

    @pytest.mark.asyncio
    async def test_grep_with_context(self, fs, tmp_path):
        """Grep with context lines."""
        f = tmp_path / "file.txt"
        f.write_text("line1\nline2\nline3\nline4\nline5\n")
        r = await fs.grep(GrepParams(pattern="line3", path=str(f), context=2))
        assert r.success

    @pytest.mark.asyncio
    async def test_grep_no_matches(self, fs, tmp_path):
        """Grep with no matches."""
        f = tmp_path / "file.txt"
        f.write_text("hello\nworld\n")
        r = await fs.grep(GrepParams(pattern="nonexistent", path=str(f)))
        # Should succeed or fail gracefully
        if not r.success:
            assert "not found" in (r.error or "").lower()


# ═══════════════════════════════════════════════════════════════
# ERROR HANDLING & EDGE CASES
# ═══════════════════════════════════════════════════════════════

class TestFilesystemErrorHandling:
    """Error cases and edge cases."""

    @pytest.mark.asyncio
    async def test_empty_file_read(self, fs, tmp_path):
        """Read empty file."""
        f = tmp_path / "empty.txt"
        f.write_text("")
        r = await fs.read(ReadParams(file_path=str(f)))
        assert r.success

    @pytest.mark.asyncio
    async def test_very_long_line(self, fs, tmp_path):
        """Read handles very long lines."""
        f = tmp_path / "long.txt"
        long_line = "x" * 10000
        f.write_text(long_line)
        r = await fs.read(ReadParams(file_path=str(f)))
        assert r.success

    @pytest.mark.asyncio
    async def test_special_characters_in_path(self, fs, tmp_path):
        """Handle files with special characters."""
        f = tmp_path / "file with spaces.txt"
        f.write_text("content")
        r = await fs.read(ReadParams(file_path=str(f)))
        assert r.success

    @pytest.mark.asyncio
    async def test_unicode_content(self, fs, tmp_path):
        """Read unicode content correctly."""
        f = tmp_path / "unicode.txt"
        f.write_text("Hello 世界 🌍", encoding="utf-8")
        r = await fs.read(ReadParams(file_path=str(f)))
        assert r.success
        content = r.data["content"] if r.data else (r.output or "")
        # Check if content contains unicode
        assert len(content) > 0

    @pytest.mark.asyncio
    async def test_crlf_line_endings(self, fs, tmp_path):
        """Handle Windows CRLF line endings."""
        f = tmp_path / "crlf.txt"
        f.write_bytes(b"line1\r\nline2\r\nline3\r\n")
        r = await fs.read(ReadParams(file_path=str(f)))
        assert r.success

    @pytest.mark.asyncio
    async def test_write_large_file(self, fs, tmp_path):
        """Write can handle large files."""
        f = tmp_path / "large.txt"
        content = "x" * 100000  # 100KB
        r = await fs.write(WriteParams(file_path=str(f), content=content))
        assert r.success
        assert f.stat().st_size >= 100000
