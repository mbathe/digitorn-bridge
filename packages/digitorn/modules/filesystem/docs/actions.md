# Filesystem Module - Action Reference

Complete reference for the five actions.

Every action is designed for AI agents:
- **read** includes line numbers for precise referencing
- **edit** does surgical replacements with fuzzy matching and error recovery
- **write** auto-creates parents and uses atomic writes
- **grep** uses ripgrep for speed with smart error hints
- **glob** finds files by pattern with type filtering

Removed actions (use Bash instead): ls, mv, cp, rm, mkdir, insert, find, file_stat, undo.

---

## read

Read a file with line numbers. Auto-detects: text files, images, PDFs, notebooks.

**Permissions:** `fs.read`
**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `file_path` | string | yes | - | Absolute path to the file |
| `offset` | integer | no | - | Line number to start from (0-indexed) |
| `limit` | integer | no | - | Number of lines to read |

### Returns

```json
{
  "success": true,
  "message": "1\tdef hello():\n2\t    print('world')\n",
  "metadata": {
    "file_path": "/workspace/main.py",
    "total_lines": 10,
    "lines_read": 2,
    "offset": 1,
    "limit": 2
  }
}
```

**Behavior rules:**
- Always read before editing
- By default reads up to 2000 lines
- Use offset/limit for large files
- Auto-detects images, PDFs, notebooks

---

## write

Create or overwrite a file. Parent directories are created automatically.

**Permissions:** `fs.write`
**Risk level:** Medium

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `file_path` | string | yes | - | Absolute path to write to |
| `content` | string | yes | - | File content |

### Returns

```json
{
  "success": true,
  "message": "Written 256 bytes to /workspace/new_file.py",
  "metadata": {
    "file_path": "/workspace/new_file.py",
    "operation": "create",
    "bytes_written": 256,
    "lines": 8
  }
}
```

**Behavior rules:**
- OVERWRITES existing files
- For modifying existing files, use Edit instead
- Parent directories created automatically
- Use only for NEW files or complete rewrites

---

## edit

Find-and-replace in a file. Fuzzy matching with closest match suggestions on failure.

**Permissions:** `fs.read`, `fs.write`
**Risk level:** Medium

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `file_path` | string | yes | - | Absolute path to edit |
| `old_string` | string | no* | - | Text to replace (copy from Read output). *Not needed if using `insert_at_line` |
| `new_string` | string | yes | - | Replacement text (or insertion text) |
| `replace_all` | boolean | no | false | Replace all occurrences |
| `insert_at_line` | integer | no | - | Insert at specific line (1-based) instead of replacing |

### Returns

```json
{
  "success": true,
  "message": "Changed 2 lines\n  Line 5:\n    - print('old')\n    + print('new')",
  "metadata": {
    "file_path": "/workspace/main.py",
    "lines_changed": 2,
    "bytes_changed": 12,
    "operation": "replace"
  }
}
```

**On failure (closest matches shown):**
```json
{
  "success": false,
  "message": "old_string not found in file",
  "error": true,
  "metadata": {
    "suggestion": "Did you mean one of these?\n  1. (Lines 5-7, 85% match)...",
    "closest_matches": [
      {"line_range": "5-7", "similarity": "85%", "text": "..."}
    ]
  }
}
```

**Behavior rules:**
- old_string must be EXACT text (copy from Read output)
- Must read file first before editing
- replace_all to replace all occurrences
- insert_at_line for insertions (no old_string needed)
- On failure, closest matches shown with similarity scores

---

## grep

Search file contents for a regex pattern (powered by ripgrep).

**Permissions:** `fs.read`
**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `pattern` | string | yes | - | Regex pattern to search for |
| `path` | string | no | `.` | File or directory to search |
| `glob` | string | no | - | Glob pattern to filter files (e.g. `*.py`) |
| `output_mode` | enum | no | `content` | `content`, `files_with_matches`, or `count` |
| `context` | integer | no | 0 | Lines before/after each match (0-20) |
| `multiline` | boolean | no | false | Match across multiple lines |

### Returns

```json
{
  "success": true,
  "message": "src/main.py:10: def hello():",
  "metadata": {
    "num_matches": 3,
    "output_mode": "content",
    "applied_max_results": 250
  }
}
```

**Behavior rules:**
- ALWAYS use Grep for content search (NOT `grep` in Bash)
- Supports full regex syntax
- Use Glob for filename-based search instead
- Output modes: content (lines), files_with_matches (paths), count (numbers)

---

## glob

Find files by name pattern.

**Permissions:** `fs.read`
**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `pattern` | string | yes | - | Glob pattern (e.g. `**/*.py`, `*.md`) |
| `path` | string | no | `.` | Directory to search in |
| `type` | enum | no | - | Filter: `file` or `dir` |

### Returns

```json
{
  "success": true,
  "message": "src/main.py\nsrc/utils.py\ntests/test_main.py",
  "metadata": {
    "num_matches": 3,
    "matches": ["src/main.py", "src/utils.py", "tests/test_main.py"],
    "truncated": false
  }
}
```

**If no matches found:**
```json
{
  "success": true,
  "message": "No matches found. Suggestions:\n  • Try a broader pattern: `**/*`\n  • Check path exists: use Bash `ls -la <path>`",
  "metadata": {
    "num_matches": 0,
    "suggestion": "..."
  }
}
```

**Behavior rules:**
- Use for NAME-BASED searching (NOT content)
- Returns paths sorted by modification time
- Type filtering: `file` or `dir`
- Smart suggestions if no matches found

---

## Architecture Changes (v2.1.0)

Consolidated from 15 tools to 5.

**Removed:**
- `ls` (use Bash)
- `mv` (use Bash)
- `cp` (use Bash)
- `rm` (use Bash)
- `mkdir` (use Bash or Write auto-mkdir)
- `insert` (use Edit with insert_at_line)
- `find` (use Glob)
- `file_stat` (use Bash stat or Read metadata)
- `undo` (use Git via Bash)

**New internal features:**
- Fuzzy string matching (6 strategies)
- Closest match suggestions on Edit failure
- Error recovery hints for all failures
- Auto-detection: images, PDFs, notebooks
- Atomic writes with temp file + rename
- Rich metadata responses
