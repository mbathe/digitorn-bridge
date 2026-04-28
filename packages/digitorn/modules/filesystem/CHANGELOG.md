# Changelog - Filesystem Module

## [2.1.0] - 2026-04-15

### Consolidation: 15 Tools → 5 Ultra-Powerful Tools

**Goal**: Reduce LLM noise (fewer tool choices) while increasing power.

### Removed Actions (Use Bash instead)
- `ls` → `bash ls`
- `mv` → `bash mv`
- `cp` → `bash cp`
- `rm` → `bash rm`
- `mkdir` → `bash mkdir -p` (or Write auto-creates parents)
- `insert` → Edit with `insert_at_line` parameter
- `find` → Glob with type filtering
- `file_stat` → `bash stat` or Read metadata
- `undo` → git via bash

### Kept & Enhanced
- **read** - now with fuzzy matching, auto-detects images/PDFs/notebooks
- **write** - atomic writes, auto mkdir -p, rich metadata
- **edit** - fuzzy string matching (6 strategies), insert_at_line support, closest matches on failure
- **grep** - ripgrep-powered, multiline, context, smart error hints
- **glob** - type filtering, suggestions if no matches

### New Features
- Fuzzy string matching with 6 fallback strategies
- Closest match suggestions on Edit failure
- Recovery hints for all errors
- Rich metadata responses (file_size, lines_changed, bytes_changed, etc.)
- Smart error suggestions (e.g., "Permission denied" → suggests bash alternatives)
- Atomic writes with temp file + rename
- Auto-detection: text files, images, PDFs, Jupyter notebooks

### Internal
- Introduced `helpers.py` - fuzzy matching, error recovery, file detection
- Reduced module.py from 1953 to 679 lines (65% reduction)
- Tool prompts now include behavioral rules (like Claude Code)
- All 8 removed actions have zero trace in active code

---

## [2.0.0] - 2026-03-09

### Breaking Changes

- Complete rewrite from scratch for agent-optimized operations.
- All action names changed to short form (`read`, `write`, `edit`, etc.).
- Old actions removed: `read_file`, `write_file`, `append_file`, `copy_file`,
  `move_file`, `delete_file`, `create_directory`, `list_directory`,
  `search_files`, `get_file_info`, `create_archive`, `extract_archive`,
  `compute_checksum`, `watch_path`.

### Added

- **`edit`** - Surgical text replacement (old_string → new_string) with preview.
- **`insert`** - Insert text at a specific line number with preview.
- **`grep`** - Regex search using native tools (rg → grep → Python fallback).
- **`find`** - File search by glob pattern with type filtering.
- **Constraint system** - `paths` (restrict allowed directories) and
  `max_file_size` (limit file sizes) enforced at action level.
- **Line-numbered output** - `read` returns content with `N│` prefixes so agents
  can reference exact lines.
- **Performance** - Sync I/O for files under 512 KB, subprocess for grep.
- **Side effects** declared on all write/delete actions.
- **`estimate_cost()`** for resource-aware scheduling.
- **`declared_permissions`** in manifest (`fs.read`, `fs.write`, `fs.delete`, `fs.list`).

### Removed

- `append_file` - Use `edit` or `insert` instead.
- `create_archive` / `extract_archive` - Not needed for agent workflows.
- `compute_checksum` - Not needed for agent workflows.
- `watch_path` - Not needed for agent workflows.

## [1.0.0] - 2026-01-15

### Added

- Initial implementation with 14 file operations.
- Symlink resolution for write-through-symlink attack prevention.
- Platform support: Linux, macOS, Windows.
