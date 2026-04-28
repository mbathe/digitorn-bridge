"""Filesystem module - parameter models for 5 ultra-powerful actions.

Design principles (inspired by Claude Code):
  1. MINIMAL visible params → LLM makes fewer mistakes
  2. POWERFUL implementation → hidden params + smart defaults
  3. CLEAR feedback → metadata + recovery hints + previews
  4. ERROR-FRIENDLY → suggestions + closest matches on failure

Param design:
  - Only essential params visible to LLM
  - Implementation details (encoding, fuzzy matching, etc.) hidden via json_schema_extra={"hidden": True}
  - Names match Claude Code exactly: file_path, limit, offset, etc.
  - Internal code uses .path alias via validation_alias for backward compat
"""

from __future__ import annotations

from pydantic import BaseModel, Field, AliasChoices

_HIDDEN = {"hidden": True}


# ============================================================================
# 5 CORE ACTIONS (ultra-simple + ultra-powerful)
# ============================================================================

class ReadParams(BaseModel):
    """Read a file with line numbers. Always read before editing."""

    file_path: str = Field(
        ...,
        validation_alias=AliasChoices("file_path", "path"),
        description="The absolute path to the file to read."
    )
    offset: int | None = Field(
        None,
        ge=0,
        validation_alias=AliasChoices("offset", "start_line"),
        description="The line number to start reading from. Only provide if the file is too large to read at once."
    )
    limit: int | None = Field(
        None,
        ge=1,
        validation_alias=AliasChoices("limit", "end_line"),
        description="The number of lines to read. Only provide if the file is too large to read at once."
    )

    # Hidden implementation details (auto-detection + recovery)
    encoding: str = Field("utf-8", json_schema_extra=_HIDDEN)
    pages: str | None = Field(None, json_schema_extra=_HIDDEN)  # PDF page ranges
    pattern: str = Field(default="", json_schema_extra=_HIDDEN)  # Content search
    max_binary_size: int = Field(1024 * 1024, json_schema_extra=_HIDDEN)

    @property
    def path(self) -> str:
        return self.file_path

    @property
    def start_line(self) -> int | None:
        """Convert Claude Code offset (0-based) to internal start_line (1-based)."""
        if self.offset is None:
            return None
        return self.offset + 1

    @property
    def end_line(self) -> int | None:
        """Convert Claude Code offset+limit to internal end_line (1-based)."""
        if self.limit is None:
            return None
        start = (self.offset or 0) + 1
        return start + self.limit - 1


class WriteParams(BaseModel):
    """Write a file. Creates parent directories automatically.

    Use Edit for small changes - Write is for NEW files or complete rewrites.
    """

    file_path: str = Field(
        ...,
        validation_alias=AliasChoices("file_path", "path"),
        description="The absolute path to the file to write. Parent directories are created automatically."
    )
    content: str = Field(
        ...,
        description="The content to write to the file."
    )

    # Hidden implementation details
    create_dirs: bool = Field(True, json_schema_extra=_HIDDEN)
    encoding: str = Field("utf-8", json_schema_extra=_HIDDEN)
    atomic: bool = Field(True, json_schema_extra=_HIDDEN)  # Write to temp first, then rename

    @property
    def path(self) -> str:
        return self.file_path


class EditParams(BaseModel):
    """Find-and-replace in a file. old_string must be EXACT text from the file.

    For insertions at a specific line, use insert_at_line instead of old_string.
    """

    file_path: str = Field(
        ...,
        validation_alias=AliasChoices("file_path", "path"),
        description="The absolute path to the file to modify."
    )
    old_string: str | None = Field(
        None,
        description="The text to replace. Copy this from Read output. Must be unique in the file. Not needed if using insert_at_line."
    )
    new_string: str = Field(
        ...,
        description="The text to replace it with (or insert if using insert_at_line)."
    )
    replace_all: bool = Field(
        False,
        json_schema_extra=_HIDDEN,
        description="Replace all occurrences of old_string.",
    )
    insert_at_line: int | None = Field(
        None,
        ge=1,
        json_schema_extra=_HIDDEN,
        description="Insert new_string at this line number (1-based). Use instead of old_string.",
    )

    # Hidden implementation details
    fuzzy_threshold: float = Field(0.85, json_schema_extra=_HIDDEN)  # 85% SequenceMatcher
    max_suggestions: int = Field(3, json_schema_extra=_HIDDEN)
    encoding: str = Field("utf-8", json_schema_extra=_HIDDEN)

    @property
    def path(self) -> str:
        return self.file_path


class GlobParams(BaseModel):
    """Find files by name pattern. Returns paths sorted by modification time.

    For regex-based content search, use Grep instead.
    """

    pattern: str = Field(
        ...,
        description="Glob pattern. Example: '**/*.py', 'src/**/*.ts', '*.md'."
    )
    path: str = Field(
        ".",
        description="Directory to search in. Defaults to current working directory."
    )
    type: str | None = Field(
        None,
        pattern="^(file|dir)$",
        json_schema_extra=_HIDDEN,
        description="Filter by type: 'file' or 'dir'.",
    )

    # Hidden implementation details
    max_results: int = Field(5000, json_schema_extra=_HIDDEN)
    include_hidden: bool = Field(False, json_schema_extra=_HIDDEN)
    follow_symlinks: bool = Field(False, json_schema_extra=_HIDDEN)


class GrepParams(BaseModel):
    """Search file contents for a regex pattern. Use before Read to find what to edit.

    Powered by ripgrep for speed. Use Glob for filename-based search.
    """

    pattern: str = Field(
        ...,
        description="The regular expression pattern to search for in file contents."
    )
    path: str = Field(
        ".",
        description="File or directory to search in. Defaults to current working directory."
    )
    glob: str | None = Field(
        None,
        validation_alias=AliasChoices("glob", "include"),
        json_schema_extra=_HIDDEN,
        description="Glob filter. Example: '*.py', '*.{ts,tsx}'.",
    )
    context: int = Field(
        0,
        ge=0,
        le=20,
        json_schema_extra=_HIDDEN,
        description="Lines of context before and after each match.",
    )

    # Hidden implementation details
    type: str | None = Field(None, json_schema_extra=_HIDDEN)
    recursive: bool = Field(True, json_schema_extra=_HIDDEN)
    max_results: int = Field(2000, json_schema_extra=_HIDDEN)
    case_sensitive: bool = Field(True, json_schema_extra=_HIDDEN)
    output_mode: str = Field("content", json_schema_extra=_HIDDEN)  # "content", "files_with_matches", "count"
    multiline: bool = Field(False, json_schema_extra=_HIDDEN)
    offset: int = Field(0, json_schema_extra=_HIDDEN)

    @property
    def include(self) -> str | None:
        return self.glob


# ============================================================================
# REMOVED ACTIONS → Use Bash instead
# ============================================================================
# - ls       → bash ls
# - mv       → bash mv
# - cp       → bash cp
# - rm       → bash rm
# - mkdir    → bash mkdir -p (or Write auto-creates parents)
# - insert   → Edit with insert_at_line
# - find     → Glob with type filtering
# - file_stat → bash stat
# - undo     → git via bash
