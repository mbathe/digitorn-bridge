"""Filesystem helpers - error recovery, fuzzy matching, smart feedback."""

from __future__ import annotations

import logging
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

class FuzzyMatch(NamedTuple):
    """A fuzzy-matched string from file content."""
    text: str
    start_line: int
    end_line: int
    similarity: float

class EditResult(NamedTuple):
    """Result of an edit operation with metadata."""
    success: bool
    file_path: str
    lines_changed: int
    bytes_changed: int
    before: str | None  # For preview (before content)
    after: str | None  # For preview (after content)
    error: str | None
    suggestion: str | None  # Recovery hint for LLM
    closest_matches: list[FuzzyMatch] | None  # If fuzzy match failed

class ReadResult(NamedTuple):
    """Result of a read operation with metadata."""
    content: str
    encoding: str
    lines: int
    bytes: int
    is_binary: bool
    is_image: bool
    is_pdf: bool
    is_notebook: bool
    file_exists: bool
    error: str | None

def _detect_indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]

def _reindent_replacement(old: str, new: str, matched: str) -> str:
    old_lines = old.split("\n")
    matched_lines = matched.split("\n")
    new_lines = new.split("\n")

    if not old_lines or not matched_lines:
        return new

    # Detect the indentation shift: compare first non-empty line
    old_indent = ""
    matched_indent = ""
    for ol, ml in zip(old_lines, matched_lines):
        if ol.strip():
            old_indent = _detect_indent(ol)
            matched_indent = _detect_indent(ml)
            break

    # No shift needed
    if old_indent == matched_indent:
        return new

    # Compute the delta: how many chars to add/remove
    # Strategy: for each line in new, if it starts with old_indent pattern,
    # replace that prefix with matched_indent pattern.
    result_lines = []
    for nl in new_lines:
        nl_indent = _detect_indent(nl)
        if nl_indent.startswith(old_indent):
            # Replace old base indent with matched base indent
            extra = nl_indent[len(old_indent):]
            result_lines.append(matched_indent + extra + nl.lstrip())
        elif not nl.strip():
            # Empty/whitespace-only line - keep as-is
            result_lines.append(nl)
        else:
            # Line has less indent than old_indent - just prepend the shift
            result_lines.append(matched_indent + nl.lstrip())

    return "\n".join(result_lines)

def fuzzy_find_old_string(
    old: str,
    content: str,
    threshold: float = 0.85,
    max_suggestions: int = 3,
) -> tuple[int, int] | None:
    """Find exact match or fuzzy match of old_string in content."""
    # Strategy 1: Exact match
    start = content.find(old)
    if start != -1:
        return (start, start + len(old))

    # Strategy 2: Per-line trailing whitespace normalization
    result = _match_strip_trailing_per_line(old, content)
    if result is not None:
        return result

    # Strategy 3: CRLF vs LF
    old_lf = old.replace("\r\n", "\n").replace("\r", "\n")
    if old_lf != old:
        start = content.find(old_lf)
        if start != -1:
            return (start, start + len(old_lf))

    # Strategy 4: Whitespace collapse (tabs→spaces, multi-space→single)
    result = _match_whitespace_collapsed(old, content)
    if result is not None:
        return result

    # Strategy 5: Indentation-agnostic matching
    result = _match_indentation_agnostic(old, content)
    if result is not None:
        return result

    # Strategy 6: Fuzzy block matching (multiline SequenceMatcher)
    return _fuzzy_block_match(old, content, threshold)

def _match_strip_trailing_per_line(old: str, content: str) -> tuple[int, int] | None:
    old_lines = old.split("\n")
    old_stripped = [line.rstrip() for line in old_lines]
    content_lines = content.split("\n")
    content_stripped = [line.rstrip() for line in content_lines]

    old_stripped_joined = "\n".join(old_stripped)
    content_stripped_joined = "\n".join(content_stripped)

    pos = content_stripped_joined.find(old_stripped_joined)
    if pos == -1:
        return None

    # Map position back to original content.
    # Count how many lines precede `pos` in stripped content.
    prefix = content_stripped_joined[:pos]
    start_line = prefix.count("\n")

    # The match spans len(old_lines) lines starting at start_line.
    # Compute char offset in original content.
    char_start = sum(len(content_lines[i]) + 1 for i in range(start_line))
    n_match_lines = len(old_lines)
    char_end = char_start + sum(
        len(content_lines[start_line + i]) + 1
        for i in range(n_match_lines)
    )
    # Remove the trailing \n from the last matched line (we matched N lines,
    # not N lines + a trailing newline).
    char_end -= 1

    if char_start <= len(content) and char_end <= len(content):
        return (char_start, char_end)
    return None

def _match_whitespace_collapsed(old: str, content: str) -> tuple[int, int] | None:
    old_collapsed = re.sub(r"[ \t]+", " ", old)
    content_collapsed = re.sub(r"[ \t]+", " ", content)

    pos = content_collapsed.find(old_collapsed)
    if pos == -1:
        return None

    # Map collapsed position back to original content position.
    # Walk content char-by-char, counting collapsed chars.
    collapsed_idx = 0
    original_start = None
    i = 0
    while i < len(content) and collapsed_idx < pos + len(old_collapsed):
        if collapsed_idx == pos and original_start is None:
            original_start = i
        # If we're in a whitespace run in original, it maps to 1 char in collapsed
        if content[i] in " \t":
            if collapsed_idx >= pos + len(old_collapsed):
                break
            # Skip the whole whitespace run in original
            ws_start = i
            while i < len(content) and content[i] in " \t":
                i += 1
            collapsed_idx += 1  # The whole run = 1 space in collapsed
        else:
            if collapsed_idx >= pos + len(old_collapsed):
                break
            collapsed_idx += 1
            i += 1

    if original_start is not None:
        return (original_start, i)
    return None

def _match_indentation_agnostic(old: str, content: str) -> tuple[int, int] | None:
    old_lines = old.split("\n")
    old_stripped = [line.strip() for line in old_lines]
    content_lines = content.split("\n")
    n = len(old_lines)

    if n == 0:
        return None

    # Slide window of n lines across content
    for i in range(len(content_lines) - n + 1):
        candidate = [content_lines[i + j].strip() for j in range(n)]
        if candidate == old_stripped:
            # Found! Compute char positions in original content.
            char_start = sum(len(content_lines[k]) + 1 for k in range(i))
            char_end = char_start + sum(
                len(content_lines[i + j]) + 1 for j in range(n)
            ) - 1  # -1: no trailing \n
            return (char_start, char_end)

    return None

def _fuzzy_block_match(
    old: str,
    content: str,
    threshold: float = 0.85,
) -> tuple[int, int] | None:
    content_lines = content.split("\n")
    old_lines = old.split("\n")
    n = len(old_lines)

    if n == 0 or n > len(content_lines):
        return None

    best_ratio = 0.0
    best_start_line = -1

    for i in range(len(content_lines) - n + 1):
        candidate = "\n".join(content_lines[i:i + n])
        ratio = SequenceMatcher(None, old, candidate).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start_line = i

    if best_ratio >= threshold and best_start_line != -1:
        char_start = sum(len(content_lines[j]) + 1 for j in range(best_start_line))
        char_end = char_start + sum(
            len(content_lines[best_start_line + j]) + 1
            for j in range(n)
        ) - 1  # -1: no trailing \n after last matched line
        if char_end <= len(content):
            return (char_start, char_end)

    return None

def find_closest_matches(
    old: str,
    content: str,
    max_matches: int = 3,
) -> list[FuzzyMatch]:
    """Find up to N closest matches for error recovery suggestions."""
    lines = content.split("\n")
    old_lines = old.split("\n")
    old_len = len(old_lines)

    matches: list[tuple[float, int, int]] = []

    for i in range(len(lines) - old_len + 1):
        candidate = "\n".join(lines[i : i + old_len])
        ratio = SequenceMatcher(None, old, candidate).ratio()
        if ratio > 0.5:  # At least 50% similar
            matches.append((ratio, i, i + old_len))

    # Sort by similarity, descending
    matches.sort(reverse=True)

    result: list[FuzzyMatch] = []
    for ratio, start_line, end_line in matches[:max_matches]:
        text = "\n".join(lines[start_line:end_line])
        result.append(FuzzyMatch(
            text=text,
            start_line=start_line + 1,  # 1-indexed
            end_line=end_line,
            similarity=ratio,
        ))

    return result

_BINARY_EXTENSIONS = frozenset({
    ".bin", ".exe", ".dll", ".so", ".dylib", ".a",
    ".o", ".obj", ".lib",
    ".pyc", ".pyo", ".class", ".jar",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp",
    ".mp3", ".mp4", ".wav", ".flac", ".m4a", ".aac",
    ".pdf",  # Often binary
})

_IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg", ".tiff", ".tif",
})

def is_binary_file(path: str, content_sample: bytes | None = None) -> bool:
    """Detect if a file is binary by extension or content."""
    ext = Path(path).suffix.lower()
    if ext in _BINARY_EXTENSIONS:
        return True

    if content_sample:
        return b"\x00" in content_sample[:512]

    return False

def is_image_file(path: str) -> bool:
    """Detect if file is an image."""
    return Path(path).suffix.lower() in _IMAGE_EXTENSIONS

def is_pdf_file(path: str) -> bool:
    """Detect if file is a PDF."""
    return Path(path).suffix.lower() == ".pdf"

def is_notebook_file(path: str) -> bool:
    """Detect if file is a Jupyter notebook."""
    return Path(path).suffix.lower() == ".ipynb"

def suggest_edit_recovery(
    error: str,
    old_string: str | None,
    closest_matches: list[FuzzyMatch] | None,
) -> str:
    """Generate recovery hint for failed edit operations."""
    if "not unique" in error.lower():
        return (
            f"old_string appears multiple times in file. "
            f"Either make it more unique by adding context, or use replace_all: true to replace all instances."
        )

    if "not found" in error.lower():
        hint = "old_string not found in file. "
        if closest_matches:
            hint += f"Did you mean one of these?\n"
            for i, match in enumerate(closest_matches, 1):
                hint += f"  {i}. (Lines {match.start_line}-{match.end_line}, {match.similarity*100:.0f}% match)\n"
                hint += f"     {match.text[:80]}"
                if len(match.text) > 80:
                    hint += "..."
                hint += "\n"
        hint += "Try copying the exact text from Read output, or use Grep to find it first."
        return hint

    if "encoding" in error.lower():
        return (
            f"Encoding issue. This file might not be UTF-8. Try reading it with Bash: "
            f"`file -i <path>` to detect encoding."
        )

    if "permission" in error.lower():
        return f"Permission denied. Try via Bash: `sudo nano <path>` or adjust file permissions with `chmod`."

    return f"Edit failed: {error}. Try reading the file again with Read first."

def suggest_read_recovery(
    path: str,
    error: str,
) -> str:
    """Generate recovery hint for failed read operations."""
    if "does not exist" in error.lower():
        return f"File not found: {path}. Use Glob to find similar files first."

    if "permission" in error.lower():
        return f"Permission denied on {path}. Try: `ls -la $(dirname {path})`  via Bash to check permissions."

    if "binary" in error.lower():
        return f"{path} is a binary file. Use Bash to inspect: `file {path}`, `xxd {path} | head`, or `hexdump -C {path}`."

    if "encoding" in error.lower():
        return f"Encoding issue. Detect encoding: `file -i {path}` via Bash."

    return f"Read failed: {error}."

def suggest_glob_recovery(
    pattern: str,
    count: int,
) -> str | None:
    """Generate suggestion if Glob finds 0 results."""
    if count > 0:
        return None

    suggestions = [
        f"Try a broader pattern: `**/*` to see all files",
        f"Check path exists: use Bash `ls -la <path>`",
        f"Try case-insensitive: `**/*.PY` if looking for `.py` files (might be uppercase)",
        f"Check for typos in the pattern",
    ]

    return (
        f"No matches for pattern '{pattern}'. Suggestions:\n" +
        "\n".join(f"  - {s}" for s in suggestions)
    )

def generate_diff_preview(before: str, after: str, context_lines: int = 3) -> str:
    """Generate a simple diff preview (not full unified diff, just key changes)."""
    before_lines = before.split("\n")
    after_lines = after.split("\n")

    # For simplicity, just show before/after if significantly different
    if len(before_lines) != len(after_lines):
        return f"Changed {abs(len(after_lines) - len(before_lines))} lines"

    changed_lines = []
    for i, (b, a) in enumerate(zip(before_lines, after_lines)):
        if b != a:
            changed_lines.append((i + 1, b, a))

    if not changed_lines:
        return "No changes detected"

    if len(changed_lines) <= 5:
        preview = "Changes:\n"
        for line_no, before_text, after_text in changed_lines:
            preview += f"  Line {line_no}:\n"
            preview += f"    - {before_text[:60]}\n"
            preview += f"    + {after_text[:60]}\n"
        return preview

    return f"Changed {len(changed_lines)} lines"

def gather_file_metadata(path: str) -> dict[str, Any]:
    """Gather metadata about a file for frontend display."""
    try:
        stat = os.stat(path)
        return {
            "file_path": path,
            "file_name": os.path.basename(path),
            "file_size": stat.st_size,
            "modified_time": stat.st_mtime,
            "is_readable": os.access(path, os.R_OK),
            "is_writable": os.access(path, os.W_OK),
        }
    except Exception as e:
        return {
            "file_path": path,
            "file_name": os.path.basename(path),
            "error": str(e),
        }
