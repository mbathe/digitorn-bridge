"""Workspace Module - universal virtual filesystem for live-preview apps.

The agent sees the same 6 tools it knows from the real filesystem:
``Write``, ``Read``, ``Edit``, ``Glob``, ``Grep``, ``Delete``.
It doesn't know (or care) that the files live in memory and stream
in real time to the connected client via Socket.IO.

Under the hood every mutation publishes a ``preview:resource_set``
(or ``preview:resource_patched`` / ``preview:resource_deleted``) event
on the ``files`` channel. The client (Flutter / React) decides how to
render based on file extensions and the ``workspace`` state metadata.

Multi-step editing (slides, chapters, components) works naturally:
each file is a resource in the ``files`` channel. Write slide-01.md,
then slide-02.md, then edit slide-01.md - the client sees each
mutation in real time and reacts accordingly.

The module requires ``preview`` to be loaded in the same app.

Config (app.yaml)::

    modules:
      workspace:
        config:
          render_mode: react     # react | latex | slides | html | markdown | auto
          entry_file: src/App.tsx # main file the client should render first
          title: My App          # optional display title

The config is published as ``preview.set_state("workspace", {...})``
so the client shell can read ``usePreviewState("workspace")`` and
activate the correct renderer without any backend changes.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import logging
import os
import re
from difflib import unified_diff
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from pydantic import BaseModel, Field

from digitorn.modules.base import ActionResult, BaseModule
from digitorn.modules.decorators import action
from digitorn.modules.filesystem.helpers import (
    _reindent_replacement,
    find_closest_matches,
    fuzzy_find_old_string,
    generate_diff_preview,
    is_binary_file,
    is_image_file,
    suggest_edit_recovery,
)
from digitorn.modules.manifest import ModuleManifest

# ── Language detection ────────────────────────────────────────────────

_EXT_TO_LANG: dict[str, str] = {
    ".tsx": "tsx", ".jsx": "jsx", ".ts": "typescript", ".js": "javascript",
    ".css": "css", ".html": "html", ".json": "json", ".jsonc": "json",
    ".py": "python", ".pyi": "python",
    ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin",
    ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
    ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".xml": "xml", ".svg": "svg",
    ".sql": "sql", ".graphql": "graphql",
    ".md": "markdown", ".mdx": "markdown",
    ".tex": "latex", ".latex": "latex", ".bib": "bibtex",
    ".dart": "dart", ".lua": "lua", ".zig": "zig",
    ".dockerfile": "dockerfile",
    ".pptx": "pptx", ".csv": "csv", ".tsv": "tsv",
}


def _detect_language(path: str) -> str:
    name = os.path.basename(path).lower()
    if name == "dockerfile":
        return "dockerfile"
    _, ext = os.path.splitext(name)
    return _EXT_TO_LANG.get(ext, "text")


def _safe_unified_diff(before: str, after: str, path: str, *, n: int = 3) -> str:
    """Generate a well-formed unified diff safe to `difflib.PatchSet.from_string()`.

    Normalises both inputs to end with a trailing newline before
    splitting - without this, a final line without ``\\n`` produces
    ``-last\\n+newlast`` with the ``-last`` missing its newline,
    glueing it to the next diff line (``-last+newlast``) which breaks
    every unified-diff parser.
    """
    before_norm = before if before.endswith("\n") or not before else before + "\n"
    after_norm = after if after.endswith("\n") or not after else after + "\n"
    return "".join(unified_diff(
        before_norm.splitlines(keepends=True),
        after_norm.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=n,
    ))


# Cap on ``unified_diff_pending`` payload size. Picked at 200 KB so
# nearly every real-world file diff fits whole - users see the
# complete diff body in the editor pane instead of a silently
# truncated tail. The daemon-side counters (``insertions_pending`` /
# ``deletions_pending``) are computed directly via difflib over the
# full content and are NOT affected by this cap, so the +/- badge
# stays accurate even if the diff string is clipped. Beyond ~500 KB
# the visual diff stops being useful anyway (Monaco struggles to
# render 30K+ diff lines, the user can't scroll meaningfully).
_PENDING_DIFF_MAX_BYTES = 200_000


def _parse_unified_diff_hunks(diff: str) -> list[dict[str, Any]]:
    """Parse a unified diff into a list of hunk dicts.

    Each hunk: ``{index, hash, header, old_start, old_len, new_start, new_len, body}``.
    ``hash`` is a 12-char SHA-256 of ``header + body`` - stable across
    retries so the client can identify a hunk through a race.
    """
    import hashlib
    import re
    hunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in diff.split("\n"):
        if raw_line.startswith("@@"):
            if current is not None:
                hunks.append(_finalize_hunk(current, len(hunks)))
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", raw_line)
            if not m:
                current = None
                continue
            current = {
                "header": raw_line,
                "body": [],
                "old_start": int(m.group(1)),
                "old_len": int(m.group(2) if m.group(2) is not None else 1),
                "new_start": int(m.group(3)),
                "new_len": int(m.group(4) if m.group(4) is not None else 1),
            }
        elif current is not None and raw_line and raw_line[0] in " -+":
            current["body"].append(raw_line)
    if current is not None:
        hunks.append(_finalize_hunk(current, len(hunks)))
    return hunks


def _finalize_hunk(h: dict[str, Any], index: int) -> dict[str, Any]:
    import hashlib
    digest_src = h["header"] + "\n" + "\n".join(h["body"])
    h["hash"] = hashlib.sha256(digest_src.encode("utf-8")).hexdigest()[:12]
    h["index"] = index
    return h


def _apply_hunks_to(
    source_lines: list[str],
    hunks: list[dict[str, Any]],
    *,
    direction: str = "forward",
) -> list[str]:
    """Apply selected hunks to ``source_lines`` and return the result.

    ``direction='forward'`` uses the hunk's ``+`` lines to replace the
    ``-`` lines (baseline → current).  ``direction='reverse'`` does the
    opposite (current → baseline). Hunks must be pre-filtered to those
    actually being applied.

    Hunks are applied in reverse position order so an earlier hunk's
    indices aren't perturbed by a later hunk's length change.
    """
    result = list(source_lines)
    if direction == "forward":
        ordered = sorted(hunks, key=lambda h: h["old_start"], reverse=True)
        for h in ordered:
            start = h["old_start"] - 1
            length = h["old_len"]
            replacement = [b[1:] for b in h["body"] if b and b[0] in " +"]
            result[start:start + length] = replacement
    else:
        ordered = sorted(hunks, key=lambda h: h["new_start"], reverse=True)
        for h in ordered:
            start = h["new_start"] - 1
            length = h["new_len"]
            replacement = [b[1:] for b in h["body"] if b and b[0] in " -"]
            result[start:start + length] = replacement
    return result


def _select_hunks(hunks: list[dict[str, Any]], selector: list[Any]) -> list[dict[str, Any]]:
    """Filter hunks matching any of the given indices or hashes."""
    if not selector:
        return []
    indices: set[int] = set()
    hashes: set[str] = set()
    for s in selector:
        if isinstance(s, int):
            indices.add(s)
        elif isinstance(s, str):
            if s.isdigit():
                indices.add(int(s))
            else:
                hashes.add(s)
    return [h for h in hunks if h["index"] in indices or h["hash"] in hashes]


def _count_pending_from_hunks(hunks: list[dict[str, Any]]) -> tuple[int, int]:
    """Count insertions + deletions across a list of hunks."""
    ins = 0
    dels = 0
    for h in hunks:
        for line in h["body"]:
            if not line:
                continue
            if line[0] == "+":
                ins += 1
            elif line[0] == "-":
                dels += 1
    return ins, dels


def _norm(path: str) -> str:
    """Normalize a workspace path: strip ./ prefix, use forward slashes.

    Workspace paths are always relative to the workspace root.
    Handles: './src/App.tsx' → 'src/App.tsx', 'src\\App.tsx' → 'src/App.tsx'.
    """
    p = path.replace("\\", "/")
    # Strip leading ./ (but not ../)
    while p.startswith("./"):
        p = p[2:]
    # Strip leading / (workspace paths are relative)
    p = p.lstrip("/")
    return p


def _glob_match(path: str, pattern: str) -> bool:
    """Glob match with proper `**` (any depth) support.

    fnmatch is wrong here: its `*` matches `/` too, so `slides/*.md` would
    hit `slides/a/01.md`. We translate the pattern to a proper regex where
    `*` stops at `/` and `**` crosses directory separators.
    """
    # Translate glob → regex manually
    i = 0
    out: list[str] = []
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
                # consume optional trailing slash after **
                if i < len(pattern) and pattern[i] == "/":
                    i += 1
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c in ".+^$()|{}":
            out.append(re.escape(c))
            i += 1
        elif c == "[":
            # Character class - pass through
            j = pattern.find("]", i)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                out.append(pattern[i:j + 1])
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    regex = "^" + "".join(out) + "$"
    try:
        return re.match(regex, path) is not None
    except re.error:
        return fnmatch.fnmatch(path, pattern)


# ── Built-in validators (in-memory, no disk reads) ──────────────


def _validate_json_content(content: str, path: str) -> list[dict[str, Any]]:
    import json as _json
    try:
        _json.loads(content)
        return []
    except _json.JSONDecodeError as e:
        return [{"line": e.lineno or 1, "column": e.colno or 1,
                 "severity": "error", "message": e.msg, "source": "json"}]


def _validate_yaml_content(content: str, path: str) -> list[dict[str, Any]]:
    try:
        import yaml as _yaml
        list(_yaml.safe_load_all(content))
        return []
    except Exception as e:
        line, col = 1, 1
        if hasattr(e, "problem_mark") and e.problem_mark is not None:
            line = e.problem_mark.line + 1
            col = e.problem_mark.column + 1
        msg = getattr(e, "problem", str(e))
        return [{"line": line, "column": col, "severity": "error",
                 "message": msg, "source": "yaml"}]


def _validate_toml_content(content: str, path: str) -> list[dict[str, Any]]:
    try:
        import tomllib
        tomllib.loads(content)
        return []
    except Exception as e:
        msg = str(e)
        line = 1
        m = re.search(r"line (\d+)", msg)
        if m:
            line = int(m.group(1))
        return [{"line": line, "column": 1, "severity": "error",
                 "message": msg, "source": "toml"}]


def _validate_python_content(content: str, path: str) -> list[dict[str, Any]]:
    try:
        compile(content, path, "exec")
        return []
    except SyntaxError as e:
        return [{"line": e.lineno or 1, "column": e.offset or 1,
                 "severity": "error", "message": e.msg, "source": "python"}]
    except Exception:
        return []


def _validate_latex_content(content: str, path: str) -> list[dict[str, Any]]:
    """Basic LaTeX validation - check for unmatched braces and environments."""
    diags: list[dict[str, Any]] = []
    stack: list[tuple[str, int]] = []
    brace_depth = 0
    for i, line in enumerate(content.split("\n"), 1):
        stripped = line.lstrip()
        if stripped.startswith("%"):
            continue
        # Track braces
        for ch in line:
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth < 0:
                    diags.append({"line": i, "column": 1, "severity": "error",
                                  "message": "Unmatched closing brace '}'", "source": "latex"})
                    brace_depth = 0
        # Track environments
        for m in re.finditer(r"\\begin\{(\w+)\}", line):
            stack.append((m.group(1), i))
        for m in re.finditer(r"\\end\{(\w+)\}", line):
            env_name = m.group(1)
            if stack and stack[-1][0] == env_name:
                stack.pop()
            else:
                expected = stack[-1][0] if stack else "none"
                diags.append({"line": i, "column": 1, "severity": "error",
                              "message": f"\\end{{{env_name}}} but expected \\end{{{expected}}}",
                              "source": "latex"})
    if brace_depth > 0:
        diags.append({"line": len(content.split("\n")), "column": 1,
                       "severity": "error",
                       "message": f"{brace_depth} unclosed brace(s)",
                       "source": "latex"})
    for env, line_no in stack:
        diags.append({"line": line_no, "column": 1, "severity": "error",
                       "message": f"\\begin{{{env}}} never closed",
                       "source": "latex"})
    return diags


_BUILTIN_CONTENT_VALIDATORS: dict[str, Any] = {
    ".json": _validate_json_content,
    ".jsonc": _validate_json_content,
    ".yaml": _validate_yaml_content,
    ".yml": _validate_yaml_content,
    ".toml": _validate_toml_content,
    ".py": _validate_python_content,
    ".pyi": _validate_python_content,
    ".tex": _validate_latex_content,
    ".latex": _validate_latex_content,
}


_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml", ".ico": "image/x-icon",
}


def _mime_from_ext(path: str) -> str:
    _, ext = os.path.splitext(path.lower())
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


def _looks_like_base64(s: str) -> bool:
    """Heuristic: base64 uses [A-Za-z0-9+/=] and is at least 16 chars."""
    if len(s) < 16:
        return False
    # Sample first 64 chars
    sample = s[:64]
    return all(c.isalnum() or c in "+/=\n\r" for c in sample)


# ── Params ────────────────────────────────────────────────────────────


class WriteParams(BaseModel):
    """Create or overwrite a file."""
    path: str = Field(..., description="File path, e.g. src/App.tsx")
    content: str = Field(..., description="Full file content.")


class ReadParams(BaseModel):
    """Read a file."""
    path: str = Field(..., description="File path to read.")
    offset: int | None = Field(default=None, json_schema_extra={"hidden": True}, description="1-indexed start line.")
    limit: int | None = Field(default=None, json_schema_extra={"hidden": True}, description="Max lines to return.")


class EditParams(BaseModel):
    """Surgical text replacement in an existing file."""
    path: str = Field(..., description="File path to edit.")
    old_string: str | None = Field(default=None, description="Exact text to find (must be unique).")
    new_string: str = Field(default="", description="Replacement text.")
    replace_all: bool = Field(default=False, json_schema_extra={"hidden": True}, description="Replace all occurrences.")
    insert_at_line: int | None = Field(default=None, json_schema_extra={"hidden": True}, description="Insert before this line (1-indexed). Omit old_string when using this.")
    fuzzy_threshold: float = Field(default=0.85, ge=0.0, le=1.0, json_schema_extra={"hidden": True}, description="Fuzzy match threshold.")
    max_suggestions: int = Field(default=3, ge=1, le=10, json_schema_extra={"hidden": True}, description="Max suggestions on failure.")


class GlobParams(BaseModel):
    """Find files by name pattern."""
    pattern: str = Field(..., description="Glob pattern, e.g. **/*.tsx, slides/*.md")
    sort_by: str = Field(default="path", json_schema_extra={"hidden": True}, description="Sort: path, size, lines.")


class GrepParams(BaseModel):
    """Search file contents by regex."""
    pattern: str = Field(..., description="Regex pattern to search for.")
    glob: str | None = Field(default=None, json_schema_extra={"hidden": True}, description="Glob filter, e.g. *.tsx")
    case_insensitive: bool = Field(default=False, json_schema_extra={"hidden": True}, description="Case-insensitive.")
    multiline: bool = Field(default=False, json_schema_extra={"hidden": True}, description="Multiline mode.")
    before: int = Field(default=0, ge=0, le=20, json_schema_extra={"hidden": True}, description="Context lines before.")
    after: int = Field(default=0, ge=0, le=20, json_schema_extra={"hidden": True}, description="Context lines after.")
    max_results: int = Field(default=200, ge=1, le=2000, json_schema_extra={"hidden": True}, description="Max results.")


class DeleteParams(BaseModel):
    """Delete a file."""
    path: str = Field(..., description="File path to delete.")


class ApproveFileParams(BaseModel):
    """Mark a file's current content as the new baseline (VS Code-like
    "stage"). Clears pending counters and flips validation to 'approved'."""
    path: str = Field(..., description="Workspace-relative file path.")


class RejectFileParams(BaseModel):
    """Revert a file to its last-approved baseline. If no baseline exists
    yet (first write not approved), the file is removed entirely."""
    path: str = Field(..., description="Workspace-relative file path.")


class HunksActionParams(BaseModel):
    """Apply approve / reject to a selection of hunks inside a file.

    ``hunks`` may contain either 0-based indices (ordinal in the current
    pending unified diff) or 12-char hunk hashes (stable across races -
    computed from the hunk header + body). Mixing both is allowed.
    """
    path: str = Field(..., description="Workspace-relative file path.")
    hunks: list = Field(
        default_factory=list,
        description="Selected hunks (indices or hashes).",
    )


class WritebackParams(BaseModel):
    """User-side write to a workspace file (manual edit, conflict
    resolution, drag-drop import). Different from WsWrite in that it's
    attributed to ``source: 'user'`` and may auto-approve."""
    path: str = Field(..., description="Workspace-relative file path.")
    content: str = Field(..., description="New file content.")
    auto_approve: bool = Field(
        default=False,
        description="Snapshot this content as the new baseline immediately.",
    )


class CommitParams(BaseModel):
    """Commit the session workspace to git."""
    message: str = Field(..., description="Commit message.")
    files: list[str] | None = Field(
        default=None,
        description="Explicit list of paths (null = all approved).",
    )
    push: bool = Field(default=False, description="git push after commit.")


class GitStatusParams(BaseModel):
    """Force-refresh git status classifications for every tracked file."""
    pass


# ── Config model ─────────────────────────────────────────────────────

# render_mode → entry_file auto-detection
_RENDER_DEFAULTS: dict[str, str] = {
    "react": "src/App.tsx",
    "latex": "main.tex",
    "slides": "slides/01.md",
    "html": "index.html",
    "markdown": "README.md",
}

# language → render_mode (for auto-detection from first written file)
_LANG_TO_RENDER: dict[str, str] = {
    "tsx": "react", "jsx": "react", "typescript": "react", "javascript": "react",
    "latex": "latex", "bibtex": "latex",
    "html": "html", "css": "html",
    "markdown": "markdown",
    "python": "code", "rust": "code", "go": "code",
}


class WorkspaceConfig(BaseModel):
    """Workspace config declared in app.yaml → modules.workspace.config."""

    model_config = {"extra": "forbid"}

    workspace: str = Field(
        default="",
        description=(
            "Auto-injected by the daemon at module init time. "
            "Do NOT set manually in YAML - the daemon resolves it from "
            "the app's workspace/workspace_mode config."
        ),
    )
    render_mode: str = Field(
        default="auto",
        description=(
            "How the client should render files. "
            "Values: react, latex, slides, html, markdown, code, auto. "
            "When 'auto', detected from the first file written."
        ),
    )
    entry_file: str | None = Field(
        default=None,
        description="Main file the client renders first (e.g. src/App.tsx, main.tex).",
    )
    title: str | None = Field(
        default=None,
        description="Optional display title for the workspace.",
    )
    sync_to_disk: bool = Field(
        default=True,
        description=(
            "When true (default), every write/edit/delete is mirrored to "
            "a disk directory - either the user-picked workspace (when "
            "``workspace_path`` is passed at session creation) or an "
            "auto-isolated per-session dir at "
            "``~/.digitorn/workspaces/{app_id}/{session_id}/``. "
            "Disk-backing unlocks LSP (pyright/ruff/tsserver can see the "
            "files), git tooling, cross-restart persistence, and the "
            "Lovable flow. Set explicitly to ``false`` only when you need "
            "a pure in-memory workspace (rare)."
        ),
    )
    sync_path: str | None = Field(
        default=None,
        description=(
            "Directory on disk where files are synced. Relative paths "
            "are resolved from the app's workspace dir. Defaults to "
            "the app's workspace dir if sync_to_disk is true but no "
            "path is given."
        ),
    )
    lint: bool = Field(
        default=True,
        description=(
            "When true, every write/edit runs diagnostics on the file "
            "and returns errors/warnings inline. Uses the LSP module "
            "if loaded, otherwise falls back to built-in validators "
            "(JSON, YAML, TOML, Python syntax). The agent sees issues "
            "immediately without a separate diagnostics call."
        ),
    )
    instructions: str | None = Field(
        default=None,
        description=(
            "App-specific instructions prepended to ALL workspace tool prompts. "
            "Tells the agent what kind of files to write (React, LaTeX, slides…)."
        ),
    )
    tool_instructions: dict[str, str] | None = Field(
        default=None,
        description=(
            "Per-tool instruction overrides. Keys are action names: "
            "write, read, edit, glob, grep, delete. "
            "Each value replaces the base tool_prompt for that action."
        ),
    )
    auto_approve: bool = Field(
        default=False,
        description=(
            "When true, every write/edit is implicitly approved - the "
            "baseline becomes the file's current content on each write, "
            "``validation`` stays ``approved`` and pending counters are "
            "always zero. No human review step. Use for sandbox apps, "
            "automated pipelines, or agents whose output is trusted by "
            "contract. When false (default), each change lands with "
            "``validation='pending'`` until the user or a hook approves "
            "it explicitly. Per-write override: pass "
            "``WritebackParams(auto_approve=true)`` on a one-off write."
        ),
    )


# ── Module ────────────────────────────────────────────────────────────


class WorkspaceModule(BaseModule):
    """Virtual workspace - filesystem-like API that streams to the client."""

    MODULE_ID = "workspace"
    VERSION = "1.0.0"
    CONFIG_MODEL = WorkspaceConfig

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": (
                "Virtual file workspace. Agents Write/Read/Edit/Glob/Grep "
                "files that stream live to the client. Works for any app "
                "type - React, LaTeX, slides, HTML, Python."
            ),
            "author": "Digitorn Team",
        })

    # Base tool prompts - used when no app-specific override is given.
    _BASE_TOOL_PROMPTS: dict[str, str] = {
        "write": (
            "Create or overwrite a workspace file. Content streams live to the client.\n"
            "- Use forward slashes: src/App.tsx, slides/01.md\n"
            "- Prefer many small writes over one big rewrite"
        ),
        "read": (
            "Read a workspace file with numbered lines (cat -n style).\n"
            "- Large files: use offset + limit for partial reads\n"
            "- Images: returns base64 + mime for vision models"
        ),
        "edit": (
            "Surgical text replacement in a workspace file.\n"
            "- old_string must be unique in the file. If ambiguous, add surrounding context or use replace_all=true\n"
            "- Fuzzy matching handles whitespace/indent differences automatically\n"
            "- On failure: closest_matches with line numbers are returned - copy the exact text to retry"
        ),
        "glob": (
            "Find workspace files by glob pattern.\n"
            "- ** matches any depth, * matches within one directory\n"
            "- Examples: **/*.tsx, _state/graph/nodes/*.json, slides/*.md"
        ),
        "grep": (
            "Search workspace file contents by regex.\n"
            "- Returns matching lines with line numbers\n"
            "- Use glob param to restrict search to specific file patterns"
        ),
        "delete": "Remove a file from the workspace. The client removes it from the UI instantly.",
    }

    def __init__(self) -> None:
        super().__init__()
        self._preview: Any | None = None  # injected by bootstrap
        # Per-session "did we publish workspace metadata yet?" flag.
        # Was previously a single ``bool`` shared across every active
        # session, which meant the FIRST write of the FIRST session
        # set the flag and EVERY OTHER session's first write skipped
        # the publish (workspace module is ``isolation=shared``, see
        # the comment near ``_diag_gen`` below). The result was that
        # the second app/user opening any new session never received
        # the ``workspace`` state entry on the preview channel - the
        # client could not pick render_mode / entry_file / title and
        # fell back to defaults.
        self._meta_published: dict[str, bool] = {}
        # Last is_git_repo flag we've published per session - lets
        # ``_refresh_git_repo_flag`` re-emit the workspace state when
        # the user runs ``git init`` (or rm -rf .git) mid-session
        # without paying for a full meta re-publish.
        self._last_git_repo_flag: dict[str, bool] = {}
        self._render_mode: str = "auto"
        self._entry_file: str | None = None
        self._title: str | None = None
        self._instructions: str | None = None
        self._tool_instructions: dict[str, str] = {}
        self._sync_to_disk: bool = True
        self._sync_path: str | None = None
        self._lint: bool = True
        self._auto_approve: bool = False
        self._lsp: Any | None = None  # injected by bootstrap
        # Per-(session, file) generation counter for diagnostic pushes.
        # Must be session-scoped because the workspace module is shared
        # (isolation=shared) - a single module-wide map would leak
        # counters across sessions and produce spurious "stale payload"
        # rejections on the client.
        self._diag_gen: dict[tuple[str, str], int] = {}
        # Per-(session, file) asyncio Lock to serialise mutations on
        # the same path. Without this, two concurrent agent tool calls
        # (sub-agents, background tasks) both read ``existing`` from the
        # channel, both compute ``total_insertions = prev + delta``, and
        # the second overwrites the first - cumulative counters become
        # under-counted. Also serialises the ``read_baseline`` /
        # ``write_baseline`` pair in ``_ensure_session_baseline``.
        self._path_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _path_lock(self, sid: str, path: str) -> asyncio.Lock:
        """Return the per-session/path lock, creating it lazily."""
        key = (sid, path)
        lock = self._path_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._path_locks[key] = lock
        return lock

    async def cleanup_session(self, session_id: str) -> None:
        """Drop per-session bookkeeping when the session ends.

        Called by the session manager on ``end_session``. Without this,
        ``_path_locks`` / ``_meta_published`` / ``_diag_gen`` would
        accumulate forever (workspace module is ``isolation=shared``)
        and slowly leak memory across the daemon's lifetime.
        """
        if not session_id:
            return
        self._meta_published.pop(session_id, None)
        for key in list(self._path_locks.keys()):
            if key[0] == session_id:
                self._path_locks.pop(key, None)
        for key in list(self._diag_gen.keys()):
            if key[0] == session_id:
                self._diag_gen.pop(key, None)

    def _resolve_ws_path(self, path: str) -> str:
        """Resolve a path to a workspace-relative path.

        If the path is absolute and falls under the sync_dir, strips the
        sync_dir prefix to get the relative workspace path.
        Otherwise applies _norm() for standard normalization.
        """
        p = path.replace("\\", "/")
        # If absolute, try to make it relative to sync_dir
        if os.path.isabs(p):
            sync_dir = self._resolve_sync_dir()
            if sync_dir:
                sd = sync_dir.replace("\\", "/").rstrip("/") + "/"
                if p.startswith(sd):
                    return _norm(p[len(sd):])
            # Fallback: try workspace
            ws = self.workspace
            if ws:
                wd = ws.replace("\\", "/").rstrip("/") + "/"
                if p.startswith(wd):
                    return _norm(p[len(wd):])
            # Can't resolve - just normalize and hope for the best
            return _norm(p)
        return _norm(p)

    async def on_config_update(self, config: dict[str, Any]) -> None:
        await super().on_config_update(config)
        cfg = self._config if isinstance(self._config, WorkspaceConfig) else WorkspaceConfig()
        self._render_mode = cfg.render_mode
        self._entry_file = cfg.entry_file
        self._title = cfg.title
        self._instructions = cfg.instructions
        self._tool_instructions = cfg.tool_instructions or {}
        self._sync_to_disk = cfg.sync_to_disk
        self._sync_path = cfg.sync_path
        self._lint = cfg.lint
        self._auto_approve = cfg.auto_approve
        # Reset so the next write of EVERY active session re-publishes
        # metadata with the new config. ``_meta_published`` is now
        # session-keyed, so clearing the dict drops every cached
        # "already published" flag at once.
        self._meta_published.clear()

    def get_dynamic_tool_prompts(self) -> dict[str, str]:
        """Return per-FQN tool prompts, merging base + app instructions.

        Called by ``prompt.py`` when building the system prompt.
        This is the mechanism that makes workspace tool prompts dynamic:
        each app injects its own context via ``config.instructions``.
        """
        result: dict[str, str] = {}
        for action_name, base in self._BASE_TOOL_PROMPTS.items():
            fqn = f"workspace.{action_name}"
            # Per-tool override takes priority, else base
            prompt = self._tool_instructions.get(action_name, base)
            # Prepend global instructions if present
            if self._instructions:
                prompt = f"{self._instructions.strip()}\n\n{prompt}"
            result[fqn] = prompt
        return result

    def _get_preview(self) -> Any:
        if self._preview is None:
            raise RuntimeError(
                "WorkspaceModule requires the 'preview' module. "
                "Add 'preview: {}' to your app.yaml modules."
            )
        return self._preview

    def _channel(self) -> dict[str, dict[str, Any]]:
        """Return the 'files' channel dict from the preview session."""
        return self._get_preview()._session().channel("files")

    def _make_payload(
        self,
        path: str,
        content: str,
        *,
        old_content: str | None = None,
        operation: str = "write",
    ) -> dict[str, Any]:
        """Build the resource payload sent to the preview channel.

        Tracks both per-operation deltas (insertions/deletions) AND
        cumulative totals (total_insertions/total_deletions) across the
        entire session. The cumulative counters persist in the preview
        snapshot so the client can show accurate totals after resume.
        """
        import time as _time
        lang = _detect_language(path)
        lines = content.count("\n") + 1

        # Read previous cumulative totals from existing payload
        existing = self._channel().get(path)
        prev_total_ins = existing.get("total_insertions", 0) if existing else 0
        prev_total_del = existing.get("total_deletions", 0) if existing else 0

        # Preserve validation + baseline-diff state across the payload
        # rebuild. ``validation`` defaults to "pending" - the agent just
        # wrote the file, user hasn't approved yet.
        prev_validation = existing.get("validation") if existing else None
        prev_baseline_lines = existing.get("baseline_lines", 0) if existing else 0

        # auto_approve mode - every write lands as the new baseline.
        # Pending counters are always zero, validation stays 'approved'.
        # Enables sandbox apps / trusted-agent pipelines / CI flows.
        initial_validation = "approved" if self._auto_approve else "pending"
        payload: dict[str, Any] = {
            "content": content,
            "language": lang,
            "size": len(content),
            "lines": lines,
            "operation": operation,
            "updated_at": _time.time(),
            "validation": initial_validation,
            "baseline_lines": prev_baseline_lines,
        }

        insertions = 0
        deletions = 0

        if old_content is not None and operation == "edit":
            old_lines = old_content.splitlines()
            new_lines = content.splitlines()
            import difflib
            sm = difflib.SequenceMatcher(None, old_lines, new_lines)
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == "insert":
                    insertions += j2 - j1
                elif tag == "delete":
                    deletions += i2 - i1
                elif tag == "replace":
                    deletions += i2 - i1
                    insertions += j2 - j1
            payload["status"] = "modified"
        elif operation == "write":
            if old_content is None:
                insertions = lines
                payload["status"] = "added"
            else:
                old_lines_ct = old_content.count("\n") + 1
                insertions = lines
                deletions = old_lines_ct
                payload["status"] = "modified"
        elif operation == "delete":
            payload["status"] = "deleted"
            deletions = old_content.count("\n") + 1 if old_content else 0

        # Per-operation deltas
        payload["insertions"] = insertions
        payload["deletions"] = deletions
        # Cumulative totals (persist across operations within a session)
        payload["total_insertions"] = prev_total_ins + insertions
        payload["total_deletions"] = prev_total_del + deletions

        # Pending-since-baseline counters (VS Code-style diff gutters):
        # insertions/deletions between the last-approved baseline and the
        # current content. MUST be delta-vs-baseline (not a running sum),
        # otherwise "3 writes + approve + 1-line edit" would show
        # pending=4 instead of pending=1.
        baseline_content: str | None = None
        try:
            from digitorn.modules.preview.fs_backend import read_baseline
            _ws = self._get_session_workspace_for_baseline()
            _sid = self._preview_session_id()
            if _ws and _sid:
                baseline_content = read_baseline(_ws, _sid, path)
        except Exception:
            baseline_content = None

        if self._auto_approve:
            # auto_approve = content IS the baseline → zero pending.
            payload["insertions_pending"] = 0
            payload["deletions_pending"] = 0
            payload["baseline_lines"] = len(content.splitlines()) if content else 0
        elif operation == "delete":
            # Deleted file - pending deletions match whatever was in
            # baseline (if any), insertions none.
            if baseline_content is not None:
                payload["insertions_pending"] = 0
                payload["deletions_pending"] = len(baseline_content.splitlines())
            else:
                payload["insertions_pending"] = 0
                payload["deletions_pending"] = 0
        elif baseline_content is None:
            # No baseline yet - everything in the file is pending.
            # Use splitlines() (not `lines`) so a trailing newline
            # doesn't inflate the count by +1 (client expectation:
            # "line one\nline two\nline three\n" = 3 insertions).
            payload["insertions_pending"] = len(content.splitlines()) if content else 0
            payload["deletions_pending"] = 0
        else:
            import difflib as _difflib
            base_lines = baseline_content.splitlines()
            cur_lines = content.splitlines()
            _ins = 0
            _del = 0
            _sm = _difflib.SequenceMatcher(None, base_lines, cur_lines)
            for tag, i1, i2, j1, j2 in _sm.get_opcodes():
                if tag == "insert":
                    _ins += j2 - j1
                elif tag == "delete":
                    _del += i2 - i1
                elif tag == "replace":
                    _del += i2 - i1
                    _ins += j2 - j1
            payload["insertions_pending"] = _ins
            payload["deletions_pending"] = _del

        # Cumulative unified diff since the last-approved baseline.
        # This is what the frontend "pending changes" view renders -
        # it MUST reflect every edit made since approve(), not just
        # the most recent one. The per-edit ``unified_diff`` elsewhere
        # in the payload shows only THIS operation; the client's diff
        # gutter needs the full delta.
        #
        # When there is no baseline yet (file never approved) we still
        # emit a full additions-only diff so the frontend has something
        # to render: "" vs current content. Without this, the frontend
        # falls back to the raw line count and shows "the last edit"
        # instead of the aggregate since session start.
        if self._auto_approve:
            payload["unified_diff_pending"] = ""
        elif baseline_content is not None:
            payload["unified_diff_pending"] = _safe_unified_diff(
                baseline_content, content or "", path,
            )[:_PENDING_DIFF_MAX_BYTES]
        else:
            payload["unified_diff_pending"] = _safe_unified_diff(
                "", content or "", path,
            )[:_PENDING_DIFF_MAX_BYTES]

        return payload

    def _is_git_repo(self) -> bool:
        """True when the session workspace dir contains ``.git/``.

        Cheap stat-based check; runs at meta-publish time (first write
        per session). Used by the client to hide the Commit button when
        the workspace isn't a git repo - avoids the user clicking
        Commit and getting an opaque "workspace is not a git repo"
        error from ``commit_session``.
        """
        ws = self._get_session_workspace_for_baseline()
        if not ws:
            return False
        try:
            return (Path(ws) / ".git").is_dir()
        except Exception:
            return False

    def _resolve_sync_dir(self) -> str | None:
        """Return the absolute disk path for sync, or None if disabled.

        **New default (post hook-upgrade)**: every session gets a
        disk-backed workspace by default - either the one the user
        picked (Lovable-style) or an auto-isolated dir at
        ``~/.digitorn/workspaces/{app_id}/{session_id}/``. This unlocks
        LSP / git / preview-persistence features that need real files.

        Resolution order (first match wins):

        1. Per-session user-chosen workspace (from
           ``preview._session_workspaces[sid]``) - Lovable flow.
        2. ``sync_path`` set in YAML → fixed path, never overridden.
        3. ``ctx.workspace`` IF explicitly set by user (not default cwd).
        4. Auto-isolated per session: ``~/.digitorn/workspaces/{app_id}/{sid}/``.
        5. App-level workspace dir as last resort.

        The **only** case returning ``None`` is the explicit opt-out
        ``sync_to_disk: false`` AND no other signal (no user
        workspace_path, no sync_path, no ctx.workspace, no session id).
        Apps that want pure in-memory must set ``sync_to_disk: false``
        and avoid all the above.
        """
        # 1. User-chosen workspace (Lovable) - unconditional.
        try:
            preview = self._get_preview()
            sid = preview._resolve_session_id()
            ws_map = getattr(preview, "_session_workspaces", {}) or {}
            user_ws = ws_map.get(sid)
            if user_ws:
                return os.path.abspath(user_ws)
        except Exception:
            pass

        # 2. YAML sync_path - unconditional.
        if self._sync_path:
            return os.path.abspath(self._sync_path)

        # 3. ctx.workspace explicitly set - unconditional.
        ctx = self._context_var.get()
        if ctx is not None and getattr(ctx, "workspace", None):
            ws = ctx.workspace
            default_ws = getattr(self, "_workspace", None)
            if default_ws and os.path.abspath(ws) == os.path.abspath(default_ws):
                pass  # fall through to session isolation
            else:
                return os.path.abspath(ws)

        # 4. Per-session auto-isolation - the default. Always returns a
        # valid dir when a preview session is active. Explicit opt-out
        # only: set ``sync_to_disk: false`` in workspace config.
        if self._sync_to_disk is False:
            return None
        try:
            preview = self._get_preview()
            sid = preview._resolve_session_id()
            if sid and sid != "_default_":
                app_id = (
                    getattr(self, "_app_id_override", None)
                    or getattr(self, "_app_id", "default")
                )
                return os.path.join(
                    str(Path.home()), ".digitorn", "workspaces",
                    app_id, sid,
                )
        except Exception:
            pass
        if ctx is not None and getattr(ctx, "session_id", None):
            app_id = (
                getattr(self, "_app_id_override", None)
                or getattr(self, "_app_id", "default")
            )
            return os.path.join(
                str(Path.home()), ".digitorn", "workspaces",
                app_id, ctx.session_id,
            )

        # 5. Last resort - app-level workspace.
        ws = getattr(self, "_workspace", None)
        return os.path.abspath(ws) if ws else None

    def _sync_write_to_disk(self, path: str, content: str) -> None:
        """Mirror a workspace file to disk (fire-and-forget)."""
        sync_dir = self._resolve_sync_dir()
        if sync_dir is None:
            return
        full = os.path.join(sync_dir, path)
        try:
            os.makedirs(os.path.dirname(full) or sync_dir, exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as exc:
            logger.warning(
                "workspace_sync_write_failed path=%s target=%s err=%s",
                path, full, exc,
            )

    def _sync_delete_from_disk(self, path: str) -> None:
        """Remove a workspace file from disk (fire-and-forget)."""
        sync_dir = self._resolve_sync_dir()
        if sync_dir is None:
            return
        full = os.path.join(sync_dir, path)
        if os.path.isfile(full):
            os.remove(full)

    _DISK_HYDRATE_MAX_FILES = 500
    _DISK_HYDRATE_MAX_BYTES = 1_000_000  # 1 MB per file
    _DISK_HYDRATE_SKIP_DIRS = {
        "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
        "dist", "build", ".next", ".vite", ".cache", ".turbo", ".output",
        ".svelte-kit", ".digitorn", "target", ".pytest_cache", ".mypy_cache",
    }

    def _load_disk_files_matching(self, pattern: str) -> None:
        """Scan sync_dir for files matching *pattern* and load any that
        aren't already in the workspace channel.  This makes glob/grep
        discover pre-existing project files transparently.

        Guarded against accidental loads of huge files / huge dirs - caps
        at 500 files and skips per-file content above 1 MB. Also prunes
        common heavy directories (node_modules, .git, __pycache__…) even
        when the agent's pattern would otherwise match them.
        """
        sync_dir = self._resolve_sync_dir()
        if sync_dir is None:
            return
        from pathlib import Path as _P
        root = _P(sync_dir)
        if not root.is_dir():
            return
        ch = self._channel()
        loaded = 0
        for p in root.glob(pattern):
            if loaded >= self._DISK_HYDRATE_MAX_FILES:
                break
            if not p.is_file():
                continue
            try:
                rel_parts = p.relative_to(root).parts
            except ValueError:
                continue
            if any(part in self._DISK_HYDRATE_SKIP_DIRS for part in rel_parts):
                continue
            rel = "/".join(rel_parts)
            if rel in ch:
                continue
            if is_binary_file(str(p)):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > self._DISK_HYDRATE_MAX_BYTES:
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except (OSError, PermissionError, MemoryError):
                continue
            ch[rel] = self._make_payload(rel, content)
            loaded += 1

    # Hidden files allowed at the workspace root. Anything else
    # starting with ``.`` is dropped during hydration so the user
    # doesn't see ``.DS_Store``, editor swap files, OS metadata, etc.
    # in the workspace panel. The allowlist is small on purpose -
    # only files that are typically tracked in source control AND
    # routinely edited by the user.
    _HYDRATE_HIDDEN_FILE_ALLOWLIST: set[str] = {
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        ".env.example",
        ".env.sample",
        ".npmrc",
        ".nvmrc",
        ".node-version",
        ".python-version",
        ".ruby-version",
        ".tool-versions",
        ".prettierrc",
        ".eslintrc",
    }

    async def hydrate_files_from_disk(
        self,
        session_id: str,
        *,
        max_files: int = 500,
        max_file_bytes: int = 512_000,
    ) -> int:
        if not self._sync_to_disk or not session_id:
            return 0
        preview = self._get_preview()

        ws_map = getattr(preview, "_session_workspaces", {}) or {}
        user_ws = ws_map.get(session_id)
        if user_ws:
            sync_dir = os.path.abspath(user_ws)
        elif self._sync_path:
            sync_dir = os.path.abspath(self._sync_path)
        else:
            app_id = (
                getattr(self, "_app_id_override", None)
                or getattr(self, "_app_id", "default")
            )
            sync_dir = os.path.join(
                str(Path.home()), ".digitorn", "workspaces",
                app_id, session_id,
            )

        root = Path(sync_dir)
        if not root.is_dir():
            return 0

        state = preview._store.get_or_create(session_id)
        ch = state.channel("files")
        skip_dirs = self._DISK_HYDRATE_SKIP_DIRS
        allow_hidden_files = self._HYDRATE_HIDDEN_FILE_ALLOWLIST

        # ``os.scandir`` recursive walk with directory pruning at
        # descent. ``rglob("*")`` would still iterate every file
        # inside ``node_modules`` (or any heavy tree) before the
        # filter kicked in - O(total files), not O(visible files).
        # Pruning at descent skips the readdir on those trees
        # entirely, which is the difference between 200 ms and 5 s
        # of hydration on a typical Node project.
        count = 0
        stack: list[Path] = [root]
        while stack and count < max_files:
            current = stack.pop()
            try:
                it = os.scandir(current)
            except (FileNotFoundError, PermissionError, NotADirectoryError):
                continue
            with it:
                for entry in it:
                    if count >= max_files:
                        break
                    try:
                        name = entry.name
                        if entry.is_dir(follow_symlinks=False):
                            # Prune build / cache / VCS / dependency
                            # trees so we never readdir into them.
                            if name in skip_dirs:
                                continue
                            # Skip every hidden directory except
                            # ``.github`` (CI files visible to the
                            # user).
                            if name.startswith(".") and name != ".github":
                                continue
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        # Hidden file filter at any depth - applied
                        # to the leaf name only (so ``.github/foo``
                        # passes because the dir gate already let it
                        # through). Allowlist covers the small set
                        # of dot-files users actually edit.
                        if name.startswith(".") and name not in allow_hidden_files:
                            continue
                        rel = os.path.relpath(entry.path, root).replace("\\", "/")
                        if rel in ch:
                            continue
                        try:
                            stat = entry.stat(follow_symlinks=False)
                        except (FileNotFoundError, PermissionError):
                            continue
                        if stat.st_size > max_file_bytes:
                            continue
                        if is_binary_file(entry.path):
                            continue
                        try:
                            content = Path(entry.path).read_text(
                                encoding="utf-8", errors="replace",
                            )
                        except (OSError, PermissionError):
                            continue
                        ch[rel] = self._make_payload(rel, content)
                        count += 1
                    except (OSError, PermissionError):
                        continue

        if count > 0 and hasattr(preview, "_schedule_persist"):
            preview._schedule_persist(session_id)
        return count

    async def _run_lint(self, path: str, content: str) -> list[dict[str, Any]]:
        """Run diagnostics on a file after write/edit.

        Strategy:
          1. If the LSP module is wired and has a protocol for this
             extension, call notify_change with the content and return
             the diagnostics from the LSP server.
          2. Otherwise, use built-in content validators (JSON, YAML,
             TOML, Python, LaTeX) - these work in-memory, no disk needed.
          3. If neither applies, return an empty list (no lint for this type).

        Side effect: publishes the diagnostics to the
        ``preview.diagnostics`` channel so the Flutter client can show
        red dots / marker underlines / problems panel without a second
        request. See ``_publish_diagnostics``.
        """
        if not self._lint:
            await self._publish_diagnostics(path, [])
            return []

        items: list[dict[str, Any]] = []

        if self._lsp is not None:
            try:
                from digitorn.modules.lsp.params import NotifyChangeParams
                result = await self._lsp.notify_change(
                    NotifyChangeParams(path=path, content=content),
                )
                if result.success and result.data:
                    items = result.data.get("diagnostics", []) or []
            except Exception:
                items = []

        if not items:
            _, ext = os.path.splitext(path.lower())
            validator = _BUILTIN_CONTENT_VALIDATORS.get(ext)
            if validator:
                try:
                    items = validator(content, path) or []
                except Exception:
                    items = []

        # Broadcast to the live client regardless of whether items is
        # empty - an empty list clears stale markers from a prior edit.
        await self._publish_diagnostics(path, items)
        return items

    async def _publish_diagnostics(
        self, path: str, items: list[dict[str, Any]],
    ) -> None:
        """Broadcast diagnostics for ``path`` to the preview ``diagnostics``
        channel. Each entry uses the LSP range shape (see
        ``parsers.Diagnostic.to_lsp_dict``) so Monaco can feed it straight
        into ``setModelMarkers()``.

        The payload carries:
          - ``items``           - LSP-shape diagnostics (possibly empty)
          - ``generation``      - monotonic counter per (session, path)
          - ``severity_max``    - most severe level present ("error"|"warning"|"info"|"hint"|None)
          - ``updated_at``      - float unix seconds
        """
        import time as _time
        # Convert flat diagnostics to LSP shape. Items from the LSP
        # module are already flat dicts; items from built-in parsers are
        # the same shape. Rebuild the range via Diagnostic.to_lsp_dict.
        lsp_items: list[dict[str, Any]] = []
        severities_seen: set[str] = set()
        try:
            from digitorn.modules.lsp.parsers import Diagnostic as _Diag
            for it in items or []:
                sev = (it.get("severity") or "info").lower()
                severities_seen.add(sev)
                d = _Diag(
                    file=it.get("file", path),
                    line=int(it.get("line") or 1),
                    column=int(it.get("column") or 1),
                    severity=sev,
                    message=it.get("message", ""),
                    code=str(it.get("code") or ""),
                    source=str(it.get("source") or ""),
                )
                lsp_items.append(d.to_lsp_dict())
        except Exception:
            # Parsers unavailable - fall back to passing the flat dicts
            # straight through; Monaco can still handle line/col.
            lsp_items = list(items or [])

        order = ("error", "warning", "info", "hint")
        severity_max = next((s for s in order if s in severities_seen), None)

        try:
            preview = self._get_preview()
            sid = preview._resolve_session_id()
        except Exception:
            sid = ""
        key = (sid, path)
        self._diag_gen[key] = self._diag_gen.get(key, 0) + 1
        payload: dict[str, Any] = {
            "file_path": path,
            "items": lsp_items,
            "generation": self._diag_gen[key],
            "severity_max": severity_max,
            "updated_at": _time.time(),
        }
        try:
            from digitorn.modules.preview.module import SetResourceParams
            await preview.set_resource(SetResourceParams(
                channel="diagnostics", id=path, payload=payload,
            ))
        except Exception as exc:
            logger.debug("publish_diagnostics_failed path=%s: %s", path, exc)

    def _read_from_disk(self, path: str) -> str | None:
        """Try to read a file from the sync directory on disk.

        Returns the file content as a string, or None if the file
        doesn't exist or isn't readable.
        """
        sync_dir = self._resolve_sync_dir()
        if sync_dir is None:
            return None
        full = os.path.join(sync_dir, path)
        if not os.path.isfile(full):
            return None
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except (OSError, PermissionError):
            return None

    async def _ensure_meta_published(self, first_path: str | None = None) -> None:
        """Publish workspace metadata to preview state (once per session).

        Called lazily on first write because the preview session doesn't
        exist yet at bootstrap time.  If render_mode is 'auto', we detect
        it from the first file's language.
        """
        # Resolve which session this write belongs to BEFORE the flag
        # check - the workspace module is shared across sessions of
        # the same user, so the publish must fire ONCE PER SESSION,
        # not once per module instance.
        try:
            preview = self._get_preview()
            sid = preview._resolve_session_id()
        except Exception:
            sid = ""
        if sid and self._meta_published.get(sid):
            return
        if sid:
            self._meta_published[sid] = True

        mode = self._render_mode
        if mode == "auto" and first_path:
            lang = _detect_language(first_path)
            mode = _LANG_TO_RENDER.get(lang, "code")

        entry = self._entry_file
        if entry is None and mode in _RENDER_DEFAULTS:
            entry = _RENDER_DEFAULTS[mode]
        # If still None and we have a first_path, use that
        if entry is None and first_path:
            entry = first_path

        meta = {
            "render_mode": mode,
            "entry_file": entry,
            "is_git_repo": self._is_git_repo(),
        }
        if self._title:
            meta["title"] = self._title

        from digitorn.modules.preview.module import SetStateParams
        await preview.set_state(SetStateParams(key="workspace", value=meta))
        # Track what we just published so ``_refresh_git_repo_flag`` can
        # detect drift if the user runs ``git init`` mid-session.
        if sid:
            self._last_git_repo_flag[sid] = bool(meta["is_git_repo"])

    async def _refresh_git_repo_flag(self) -> None:
        """Cheap re-check of ``.git/`` presence; re-publishes the meta
        when the flag flipped since the last publish.

        Covers the user-runs-``git init``-mid-session case where the
        initial publish (first write) said False, the Commit / Refresh
        buttons are hidden, and the user has no UI affordance to
        retrigger detection. We re-stat on every write/approve/reject -
        the cost is one ``isdir`` stat call, negligible.
        """
        try:
            preview = self._get_preview()
            sid = preview._resolve_session_id()
        except Exception:
            return
        if not sid:
            return
        new_flag = self._is_git_repo()
        old_flag = self._last_git_repo_flag.get(sid)
        if old_flag is not None and old_flag == new_flag:
            return
        self._last_git_repo_flag[sid] = new_flag
        # Re-publish the full meta dict via ``set_state`` - the
        # workspace state is a single ``workspace`` key holding the
        # render_mode / entry_file / title / is_git_repo bag, and the
        # client merges it wholesale on every state_changed event.
        # Reading sess.state isn't part of the preview module's public
        # API, so we rebuild from the same fields ``_ensure_meta_published``
        # already tracks - they're set on this module at config load.
        meta: dict[str, Any] = {
            "render_mode": self._render_mode,
            "entry_file": self._entry_file,
            "is_git_repo": new_flag,
        }
        if self._title:
            meta["title"] = self._title
        from digitorn.modules.preview.module import SetStateParams
        await preview.set_state(SetStateParams(key="workspace", value=meta))

    # ── Write ─────────────────────────────────────────────────

    @action(
        description="Create or overwrite a file. Streams live to the client.",
        params_model=WriteParams,
        risk_level="low",
        tags=["workspace", "files"],
        cli_label="Write",
        cli_param="path",
        # tool_prompt is dynamic - see get_dynamic_tool_prompts()
    )
    async def write(self, params: WriteParams) -> ActionResult:
        preview = self._get_preview()
        path = self._resolve_ws_path(params.path)
        sid = self._preview_session_id() or "_default_"

        # Per-path lock: serialise concurrent writes on the same file
        # (sub-agents, background tasks). Without this, two writes would
        # both read the same ``existing.total_insertions`` and the second
        # would overwrite the first - cumulative counters under-count.
        async with self._path_lock(sid, path):
            # Check if file already exists (for change tracking)
            existing = self._channel().get(path)
            old_content = existing.get("content") if existing else None

            # On first touch, snapshot a session baseline so future
            # ``unified_diff_pending`` computations show real -/+ pairs
            # instead of always being diff("", current). Two cases:
            #
            #   - File pre-existed on disk and is now being overwritten →
            #     baseline = disk content. This write itself shows up as a
            #     diff (-disk +new).
            #   - Brand-new file the agent is creating → baseline = the
            #     CONTENT BEING WRITTEN. The initial write itself has an
            #     empty diff (file = baseline); the next edit becomes a
            #     proper -/+ pair.
            if existing is None:
                disk_before = (
                    await asyncio.to_thread(self._read_from_disk, path)
                    if self._sync_to_disk else None
                )
                if disk_before is not None and disk_before != params.content:
                    await self._ensure_session_baseline(path, disk_before)
                else:
                    await self._ensure_session_baseline(path, params.content)

            payload = self._make_payload(
                path, params.content,
                old_content=old_content,
                operation="write",
            )

            await self._ensure_meta_published(first_path=path)
            # Re-detect git presence each write so a mid-session
            # ``git init`` flips the Commit/Refresh buttons back on
            # without requiring a session reload. Cheap (one stat).
            await self._refresh_git_repo_flag()

            from digitorn.modules.preview.module import SetResourceParams
            await preview.set_resource(SetResourceParams(
                channel="files", id=path, payload=payload,
            ))
            # Disk mirror + baseline persistence: both touch the disk
            # synchronously - off-load so the loop keeps serving Socket.IO.
            await asyncio.to_thread(self._sync_write_to_disk, path, params.content)
            await asyncio.to_thread(self._maybe_auto_approve_baseline, path, params.content)

        # Run diagnostics
        lint = await self._run_lint(path, params.content)

        data: dict[str, Any] = {
            "path": path, "language": payload["language"],
            "size": payload["size"], "total_lines": payload["lines"],
        }
        if lint:
            errors = [d for d in lint if d.get("severity") == "error"]
            warnings = [d for d in lint if d.get("severity") != "error"]
            data["lint"] = lint
            data["errors"] = len(errors)
            data["warnings"] = len(warnings)
        return ActionResult(success=True, data=data)

    # ── Read ──────────────────────────────────────────────────

    @action(
        description="Read a file from the workspace.",
        params_model=ReadParams,
        risk_level="low",
        tags=["workspace", "files"],
        cli_label="Read",
        cli_param="path",
    )
    async def read(self, params: ReadParams) -> ActionResult:
        path = self._resolve_ws_path(params.path)
        entry = self._channel().get(path)

        # Read-through from disk when sync_to_disk is on and file
        # exists on disk but not yet in memory (e.g. pre-existing project files).
        if entry is None and self._sync_to_disk:
            content = await asyncio.to_thread(self._read_from_disk, path)
            if content is not None:
                # Load into workspace memory so subsequent reads/edits work
                payload = self._make_payload(path, content)
                self._channel()[path] = payload
                entry = payload

        if entry is None:
            all_paths = list(self._channel().keys())
            hint = ""
            if all_paths:
                from difflib import get_close_matches
                near = get_close_matches(path, all_paths, n=3, cutoff=0.5)
                if near:
                    hint = f" Did you mean: {', '.join(near)}?"
            return ActionResult(success=False, error=f"File not found: {path}.{hint}")

        # Image passthrough - return base64 payload for vision-capable LLMs
        if is_image_file(path):
            raw = entry.get("content", "")
            # Images can be stored as base64 string or raw bytes-in-string.
            return ActionResult(success=True, data={
                "path": path,
                "is_image": True,
                "language": entry.get("language", "binary"),
                "size": entry.get("size", len(raw)),
                "metadata": {
                    "image_data": raw if _looks_like_base64(raw) else base64.b64encode(raw.encode("latin1", "ignore")).decode("ascii"),
                    "image_mime": _mime_from_ext(path),
                },
            })

        content = entry.get("content", "")
        lines = content.split("\n")
        total_lines = len(lines)

        # Partial read with offset/limit (0-based offset, same as filesystem)
        start = params.offset if params.offset is not None else 0
        limit = params.limit if params.limit is not None else total_lines
        end = min(start + limit, total_lines)
        slice_lines = lines[start:end]
        numbered = "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(slice_lines))

        return ActionResult(success=True, data={
            "path": path,
            "content": numbered,
            "language": entry.get("language", "text"),
            "size": entry.get("size", len(content)),
            "total_lines": total_lines,
            "lines_read": len(slice_lines),
            "start_line": start + 1,
            "end_line": end,
        })

    # ── Edit ──────────────────────────────────────────────────

    @action(
        description="Surgical text replacement in an existing file.",
        params_model=EditParams,
        risk_level="low",
        tags=["workspace", "files"],
        cli_label="Edit",
        cli_param="path",
        # tool_prompt is dynamic - see get_dynamic_tool_prompts()
    )
    async def edit(self, params: EditParams) -> ActionResult:
        preview = self._get_preview()
        path = self._resolve_ws_path(params.path)
        sid = self._preview_session_id() or "_default_"

        # Per-path lock: same reasoning as ``write()`` - serialise
        # concurrent edits on the same file so cumulative counters
        # don't get under-counted.
        async with self._path_lock(sid, path):
            return await self._edit_locked(params, preview, path)

    async def _edit_locked(
        self, params: EditParams, preview: Any, path: str,
    ) -> ActionResult:
        ch = self._channel()
        entry = ch.get(path)

        # Read-through from disk
        if entry is None and self._sync_to_disk:
            content = await asyncio.to_thread(self._read_from_disk, path)
            if content is not None:
                payload = self._make_payload(path, content)
                ch[path] = payload
                entry = payload

        if entry is None:
            return ActionResult(success=False, error=f"File not found: {path}")

        content = entry.get("content", "")
        new = params.new_string

        # Auto-snapshot the pre-edit state as session baseline (no-op if
        # already set). This makes unified_diff_pending compute against
        # a stable point so 4 successive edits accumulate insertions
        # AND deletions instead of always reporting current vs empty.
        await self._ensure_session_baseline(path, content)

        # ── Mode 1: insert_at_line (no old_string required) ──
        if params.insert_at_line is not None:
            if params.old_string:
                return ActionResult(
                    success=False,
                    error="insert_at_line and old_string are mutually exclusive.",
                )
            lines = content.split("\n")
            line_no = params.insert_at_line
            if line_no < 1 or line_no > len(lines) + 1:
                return ActionResult(
                    success=False,
                    error=f"insert_at_line={line_no} out of range (file has {len(lines)} lines).",
                )
            insert_lines = new.split("\n")
            updated_lines = lines[:line_no - 1] + insert_lines + lines[line_no - 1:]
            updated = "\n".join(updated_lines)
            replacements = 1
            mode = "insert"
        else:
            # ── Mode 2: old_string replacement with fuzzy fallback ──
            old = params.old_string
            if not old:
                return ActionResult(
                    success=False,
                    error="Provide either old_string (for replacement) or insert_at_line (for insertion).",
                )
            if old == new:
                return ActionResult(success=False, error="old_string and new_string are identical.")

            if params.replace_all:
                # Exact-only for replace_all (fuzzy + multi-replace is ambiguous)
                if old not in content:
                    return self._edit_not_found(path, old, content, params)
                replacements = content.count(old)
                updated = content.replace(old, new)
            else:
                count = content.count(old)
                if count > 1:
                    matches = find_closest_matches(old, content, max_matches=min(count, params.max_suggestions))
                    return ActionResult(
                        success=False,
                        error=(
                            f"old_string appears {count} times in {path}. "
                            f"Provide more context to make it unique, or use replace_all=true."
                        ),
                        data={"ambiguous": True, "occurrences": count, "closest_matches": [
                            {
                                "start_line": m.start_line,
                                "end_line": m.end_line,
                                "similarity": round(m.similarity, 3),
                                "text": m.text[:200],
                            } for m in matches
                        ]},
                    )
                if count == 1:
                    updated = content.replace(old, new, 1)
                    replacements = 1
                else:
                    # Count == 0 → try fuzzy match
                    pos = fuzzy_find_old_string(
                        old, content, threshold=params.fuzzy_threshold,
                    )
                    if pos is None:
                        return self._edit_not_found(path, old, content, params)
                    start_pos, end_pos = pos
                    matched_text = content[start_pos:end_pos]
                    # Re-indent new_string to match the file's actual indentation
                    new_adjusted = _reindent_replacement(old, new, matched_text)
                    updated = content[:start_pos] + new_adjusted + content[end_pos:]
                    replacements = 1
            mode = "replace"

        # ── Diff previews (short + unified) ──
        short_diff = generate_diff_preview(content, updated)
        unified = _safe_unified_diff(content, updated, path)[:4000]

        # Publish the updated file with change metadata
        payload = self._make_payload(
            path, updated,
            old_content=content,
            operation="edit",
        )
        payload["diff"] = short_diff
        payload["unified_diff"] = unified
        from digitorn.modules.preview.module import SetResourceParams
        await preview.set_resource(SetResourceParams(
            channel="files", id=path, payload=payload,
        ))
        await asyncio.to_thread(self._sync_write_to_disk, path, updated)
        await asyncio.to_thread(self._maybe_auto_approve_baseline, path, updated)

        # Run diagnostics
        lint = await self._run_lint(path, updated)

        # Compute a snippet around the change so the agent sees what happened
        new_lines = updated.split("\n")
        old_lines = content.split("\n")
        first_changed = 0
        for ci, (ol, nl) in enumerate(zip(old_lines, new_lines)):
            if ol != nl:
                first_changed = ci
                break
        lines_delta = abs(len(new_lines) - len(old_lines))
        snippet_start = max(0, first_changed - 3)
        snippet_end = min(len(new_lines), first_changed + lines_delta + 5)
        snippet = "\n".join(
            f"{snippet_start + j + 1}\t{new_lines[snippet_start + j]}"
            for j in range(snippet_end - snippet_start)
        )

        data: dict[str, Any] = {
            "path": path,
            "mode": mode,
            "replacements": replacements,
            "diff": short_diff,
            "snippet": snippet,
            "unified_diff": unified,
            "size": payload["size"],
            "total_lines": len(new_lines),
        }
        if lint:
            errors = [d for d in lint if d.get("severity") == "error"]
            warnings = [d for d in lint if d.get("severity") != "error"]
            data["lint"] = lint
            data["errors"] = len(errors)
            data["warnings"] = len(warnings)
        return ActionResult(success=True, data=data)

    def _edit_not_found(
        self, path: str, old: str, content: str, params: EditParams,
    ) -> ActionResult:
        """Build a structured 'old_string not found' error with fuzzy suggestions."""
        matches = find_closest_matches(old, content, max_matches=params.max_suggestions)
        suggestion = suggest_edit_recovery(
            error=f"old_string not found in {path}",
            old_string=old,
            closest_matches=matches,
        )
        return ActionResult(
            success=False,
            error=f"old_string not found in {path}",
            data={
                "not_found": True,
                "suggestion": suggestion,
                "closest_matches": [
                    {
                        "start_line": m.start_line,
                        "end_line": m.end_line,
                        "similarity": round(m.similarity, 3),
                        "text": m.text[:200],
                    } for m in matches
                ],
            },
        )

    # ── Glob ──────────────────────────────────────────────────

    @action(
        description="Find files by name pattern (e.g. **/*.tsx, slides/*.md).",
        params_model=GlobParams,
        risk_level="low",
        tags=["workspace", "files"],
        cli_label="Glob",
        cli_param="pattern",
    )
    async def glob(self, params: GlobParams) -> ActionResult:
        ch = self._channel()
        pattern = params.pattern

        # When sync_to_disk is on, also discover files on disk that
        # haven't been loaded into memory yet (e.g. pre-existing project).
        # ``_load_disk_files_matching`` walks ``root.glob(pattern)`` +
        # ``p.is_file()`` + ``p.stat()`` + ``p.read_text()`` for each
        # match - all SYNC. With ``**/*`` on a large repo (especially
        # one with node_modules / .git) the walk can stall the event
        # loop for 10+ seconds. The Socket.IO ping/pong runs on the
        # same loop, so the client times out and drops. Offload to a
        # thread so the loop keeps serving events.
        if self._sync_to_disk:
            import asyncio as _asyncio
            await _asyncio.to_thread(self._load_disk_files_matching, pattern)

        matched = []
        for path in ch:
            if _glob_match(path, pattern):
                entry = ch[path]
                matched.append({
                    "path": path,
                    "language": entry.get("language", "text"),
                    "size": entry.get("size", 0),
                    "lines": entry.get("lines", 0),
                })

        # Sort
        if params.sort_by == "size":
            matched.sort(key=lambda m: m["size"], reverse=True)
        elif params.sort_by == "lines":
            matched.sort(key=lambda m: m["lines"], reverse=True)
        else:
            matched.sort(key=lambda m: m["path"])

        if not matched:
            return ActionResult(success=True, data={
                "files": [], "count": 0,
                "message": f"No files matching '{pattern}'. Try '**/*' to list everything.",
            })
        return ActionResult(success=True, data={
            "files": matched, "count": len(matched),
        })

    # ── Grep ──────────────────────────────────────────────────

    @action(
        description="Search file contents by regex pattern.",
        params_model=GrepParams,
        risk_level="low",
        tags=["workspace", "files"],
        cli_label="Grep",
        cli_param="pattern",
    )
    async def grep(self, params: GrepParams) -> ActionResult:
        ch = self._channel()

        # Load disk files matching the glob filter (or all text files).
        # See ``glob()`` above - the disk walk is sync and would stall
        # the event loop on large workspaces. Offload to a thread.
        if self._sync_to_disk:
            import asyncio as _asyncio
            await _asyncio.to_thread(
                self._load_disk_files_matching, params.glob or "**/*",
            )

        flags = 0
        if params.case_insensitive:
            flags |= re.IGNORECASE
        if params.multiline:
            flags |= re.MULTILINE | re.DOTALL

        try:
            regex = re.compile(params.pattern, flags)
        except re.error as e:
            return ActionResult(success=False, error=f"Invalid regex: {e}")

        results: list[dict[str, Any]] = []
        cap = params.max_results
        files_searched = 0

        for path in sorted(ch):
            if params.glob and not _glob_match(path, params.glob):
                continue
            files_searched += 1
            content = ch[path].get("content", "")

            if params.multiline:
                # Whole-file scan - report by line number of match start
                for m in regex.finditer(content):
                    line_no = content.count("\n", 0, m.start()) + 1
                    snippet = m.group(0)
                    if len(snippet) > 200:
                        snippet = snippet[:200] + "…"
                    results.append({
                        "path": path, "line": line_no,
                        "text": snippet.replace("\n", "\\n"),
                    })
                    if len(results) >= cap:
                        break
            else:
                lines = content.split("\n")
                for i, line in enumerate(lines, 1):
                    if regex.search(line):
                        hit: dict[str, Any] = {
                            "path": path, "line": i, "text": line.rstrip(),
                        }
                        if params.before > 0:
                            lo = max(0, i - 1 - params.before)
                            hit["before"] = [
                                {"line": lo + k + 1, "text": lines[lo + k].rstrip()}
                                for k in range(i - 1 - lo)
                            ]
                        if params.after > 0:
                            hi = min(len(lines), i + params.after)
                            hit["after"] = [
                                {"line": i + k + 1, "text": lines[i + k].rstrip()}
                                for k in range(hi - i)
                            ]
                        results.append(hit)
                        if len(results) >= cap:
                            break
            if len(results) >= cap:
                break

        if not results:
            return ActionResult(success=True, data={
                "matches": [], "count": 0,
                "files_searched": files_searched,
                "message": f"No matches for '{params.pattern}'",
            })
        return ActionResult(success=True, data={
            "matches": results,
            "count": len(results),
            "files_searched": files_searched,
            "truncated": len(results) >= cap,
        })

    # ── Delete ────────────────────────────────────────────────

    @action(
        description="Delete a file from the workspace.",
        params_model=DeleteParams,
        risk_level="low",
        tags=["workspace", "files"],
        cli_label="Delete",
        cli_param="path",
    )
    async def delete(self, params: DeleteParams) -> ActionResult:
        preview = self._get_preview()
        path = self._resolve_ws_path(params.path)
        sid = self._preview_session_id() or "_default_"

        async with self._path_lock(sid, path):
            from digitorn.modules.preview.module import DeleteResourceParams
            result = await preview.delete_resource(DeleteResourceParams(
                channel="files", id=path,
            ))
            # Also clear any lingering diagnostics so the file tree doesn't
            # keep a red dot on a file that no longer exists.
            try:
                await preview.delete_resource(DeleteResourceParams(
                    channel="diagnostics", id=path,
                ))
            except Exception:
                pass
            self._diag_gen.pop((sid, path), None)
            await asyncio.to_thread(self._sync_delete_from_disk, path)
            return ActionResult(
                success=True,
                data={"path": path, "deleted": result.data.get("existed", False)},
            )

    # ── Approve / reject / git (code-state actions) ────────────────

    def _get_session_workspace_for_baseline(self) -> str | None:
        """Return a workspace dir usable for baseline persistence, or None.

        Resolution order:
        1. Session's user-chosen workspace (from preview module's
           ``_session_workspaces[sid]``) - set by ``activate_session``
           when ``POST /sessions {workspace_path: ...}`` was used.
        2. Fall back to ``_resolve_sync_dir()`` (if sync_to_disk is on).
        3. None → baselines skipped (no place to store them).
        """
        try:
            preview = self._get_preview()
            sid = preview._resolve_session_id()
            ws_map = getattr(preview, "_session_workspaces", {}) or {}
            ws = ws_map.get(sid)
            if ws:
                return ws
        except Exception:
            pass
        try:
            d = self._resolve_sync_dir()
            if d:
                return d
        except Exception:
            pass
        return None

    def _preview_session_id(self) -> str:
        preview = self._get_preview()
        try:
            return preview._resolve_session_id()
        except Exception:
            return ""

    def _maybe_auto_approve_baseline(self, path: str, content: str) -> None:
        """When auto_approve is on, snapshot every write as the new
        baseline - keeps pending counters at zero even after a restart
        (when the in-memory ``validation='approved'`` flag is re-read
        from the persisted snapshot, `_make_payload` still recomputes
        pending vs. the on-disk baseline, so the baseline MUST exist).
        """
        if not self._auto_approve:
            return
        ws = self._get_session_workspace_for_baseline()
        sid = self._preview_session_id()
        if not (ws and sid):
            return
        try:
            from digitorn.modules.preview.fs_backend import write_baseline
            write_baseline(
                ws, sid, path, content,
                approved_by="auto",
                insertions=0,
                deletions=0,
            )
        except Exception as exc:
            logger.debug("auto_approve_baseline_write_failed path=%s: %s", path, exc)

    async def _ensure_session_baseline(self, path: str, content_before: str) -> None:
        """Auto-snapshot the file's pre-mutation state as a baseline on
        the first write/edit of the session. Subsequent edits then diff
        against this stable baseline (cumulative across edits), so
        ``unified_diff_pending`` shows BOTH insertions AND deletions
        across multiple edits instead of always being
        ``diff("", current_content)`` = "current file as all additions,
        never any deletions". Idempotent: a no-op once a baseline exists.

        For brand-new files [content_before=""] this just pins the
        empty-baseline (every line is a +). For pre-existing files
        loaded via ``read_through_disk`` [content_before=disk content]
        the baseline is the on-disk state at the moment the agent first
        touched the file - exactly what the user expects from "see what
        the agent changed".
        """
        # Caller is expected to hold ``_path_lock(sid, path)`` already
        # (write/edit/delete take it before any mutation). We don't
        # re-acquire here because asyncio.Lock isn't reentrant.
        if self._auto_approve:
            return
        ws = self._get_session_workspace_for_baseline()
        sid = self._preview_session_id()
        if not (ws and sid):
            return
        try:
            from digitorn.modules.preview.fs_backend import (
                read_baseline, write_baseline,
            )
            if await asyncio.to_thread(read_baseline, ws, sid, path) is not None:
                return
            # ``record_in_history=False`` keeps synthetic snapshots out
            # of the user-visible revision list. Only explicit
            # ``approve_file`` / ``approve_file_hunks`` get an entry.
            await asyncio.to_thread(
                write_baseline,
                ws, sid, path, content_before,
                approved_by="session-start",
                insertions=0, deletions=0,
                record_in_history=False,
            )
        except Exception as exc:
            logger.debug("session_baseline_snapshot_failed path=%s: %s", path, exc)

    @action(
        description="Mark a file as approved - its current content becomes "
                    "the new baseline.",
        params_model=ApproveFileParams,
        risk_level="low",
        tags=["workspace", "files", "validation"],
        cli_label="Approve",
        cli_param="path",
        internal=True,
    )
    async def approve_file(self, params: ApproveFileParams) -> ActionResult:
        preview = self._get_preview()
        path = self._resolve_ws_path(params.path)
        sid = self._preview_session_id() or "_default_"
        # Per-path lock: serialise approve against any concurrent
        # write/edit/delete on the same file. Without this, a sub-agent
        # writing while the user clicks Approve would land its new
        # content AFTER the baseline snapshot but BEFORE the patch
        # resets counters, leaving the file marked "approved" but
        # carrying unreviewed changes.
        async with self._path_lock(sid, path):
            existing = self._channel().get(path)
            if not existing:
                return ActionResult(
                    success=False,
                    error=f"file not found in workspace: {path}",
                )
            content = existing.get("content", "")
            # Persist baseline if we have a session workspace dir.
            ws = self._get_session_workspace_for_baseline()
            if ws and sid != "_default_":
                try:
                    from digitorn.modules.preview.fs_backend import write_baseline
                    write_baseline(
                        ws, sid, path, content,
                        approved_by="user",
                        insertions=existing.get("insertions_pending", 0),
                        deletions=existing.get("deletions_pending", 0),
                    )
                except Exception as exc:
                    logger.warning(
                        "approve_file_baseline_persist_failed path=%s: %s", path, exc,
                    )
            from digitorn.modules.preview.module import PatchResourceParams
            # Reset cumulative counters: after approve, the file's current
            # state IS the baseline, so subsequent edits start from zero
            # both for ``insertions_pending/deletions_pending`` (already
            # zeroed below) AND for ``total_insertions/total_deletions``
            # which the frontend uses for the +N -M aggregate badge.
            # ``updated_at`` MUST bump so the client's ``wroteSinceLastRebuild``
            # check fires - without it the badge stays stuck on stale deltas.
            import time as _time
            await preview.patch_resource(PatchResourceParams(
                channel="files", id=path,
                patch={
                    "validation": "approved",
                    "insertions_pending": 0,
                    "deletions_pending": 0,
                    "total_insertions": 0,
                    "total_deletions": 0,
                    "baseline_lines": existing.get("lines", 0),
                    "unified_diff_pending": "",
                    "updated_at": _time.time(),
                },
            ))
            return ActionResult(success=True, data={"path": path, "validation": "approved"})

    @action(
        description="Reject the pending changes - revert the file to its "
                    "last-approved baseline (or delete it if never approved).",
        params_model=RejectFileParams,
        risk_level="low",
        tags=["workspace", "files", "validation"],
        cli_label="Reject",
        cli_param="path",
        internal=True,
    )
    async def reject_file(self, params: RejectFileParams) -> ActionResult:
        preview = self._get_preview()
        path = self._resolve_ws_path(params.path)
        sid = self._preview_session_id() or "_default_"
        # Per-path lock: same rationale as approve_file - prevents a
        # concurrent agent write from racing against the baseline-read,
        # delete-or-restore, channel-mutation sequence.
        async with self._path_lock(sid, path):
            existing = self._channel().get(path)
            if not existing:
                return ActionResult(
                    success=False,
                    error=f"file not found in workspace: {path}",
                )
            ws = self._get_session_workspace_for_baseline()
            baseline_content: str | None = None
            user_approved = False
            if ws and sid != "_default_":
                try:
                    from digitorn.modules.preview.fs_backend import (
                        read_baseline, has_user_approval,
                    )
                    baseline_content = read_baseline(ws, sid, path)
                    user_approved = has_user_approval(ws, sid, path)
                except Exception:
                    baseline_content = None
                    user_approved = False
            # "Reject = delete" applies when the user never explicitly
            # approved this file. Two sub-cases qualify:
            #   - No baseline at all (legacy mode, before auto-baselining)
            #   - Baseline exists but only as a session-start auto-snapshot
            #     (``has_user_approval`` returns False). Without this check,
            #     my auto-baseline fix turned reject-of-brand-new-file into
            #     a no-op restore, which contradicts the user's mental model
            #     ("if I reject something I never approved, it goes away").
            if baseline_content is None or not user_approved:
                from digitorn.modules.preview.module import DeleteResourceParams
                await preview.delete_resource(DeleteResourceParams(
                    channel="files", id=path,
                ))
                await asyncio.to_thread(self._sync_delete_from_disk, path)
                # Also drop the auto-baseline file so a future write of the
                # same path starts fresh.
                if ws and sid != "_default_" and baseline_content is not None:
                    try:
                        from digitorn.modules.preview.fs_backend import delete_baseline
                        delete_baseline(ws, sid, path)
                    except Exception:
                        pass
                return ActionResult(success=True, data={"path": path, "reverted": "deleted"})
            # Restore the baseline content - write it back through normal path.
            payload = self._make_payload(
                path, baseline_content,
                old_content=existing.get("content"),
                operation="write",
            )
            payload["validation"] = "approved"
            payload["insertions_pending"] = 0
            payload["deletions_pending"] = 0
            payload["unified_diff_pending"] = ""
            from digitorn.modules.preview.module import SetResourceParams
            await preview.set_resource(SetResourceParams(
                channel="files", id=path, payload=payload,
            ))
            await asyncio.to_thread(self._sync_write_to_disk, path, baseline_content)
            return ActionResult(success=True, data={"path": path, "reverted": "baseline"})

    @action(
        description="Approve only specific hunks of a file (partial staging).",
        params_model=HunksActionParams,
        risk_level="low",
        tags=["workspace", "files", "validation", "hunks"],
        cli_label="Approve hunks",
        cli_param="path",
        internal=True,
    )
    async def approve_file_hunks(self, params: HunksActionParams) -> ActionResult:
        preview = self._get_preview()
        path = self._resolve_ws_path(params.path)
        sid = self._preview_session_id() or "_default_"
        # Per-path lock: covers the read-channel → read-baseline →
        # parse-hunks → write-baseline → patch-channel sequence
        # against any concurrent agent write. Without this, an agent
        # write between the baseline read and the hunk apply would
        # cause hunks to be applied against a stale baseline,
        # producing silently wrong file content.
        async with self._path_lock(sid, path):
            existing = self._channel().get(path)
            if not existing:
                return ActionResult(
                    success=False, error=f"file not found in workspace: {path}",
                )
            current = existing.get("content", "") or ""
            ws = self._get_session_workspace_for_baseline()
            baseline = ""
            if ws and sid != "_default_":
                try:
                    from digitorn.modules.preview.fs_backend import read_baseline
                    baseline = read_baseline(ws, sid, path) or ""
                except Exception:
                    baseline = ""

            diff = _safe_unified_diff(baseline, current, path)
            hunks = _parse_unified_diff_hunks(diff)
            selected = _select_hunks(hunks, list(params.hunks))
            if not selected:
                return ActionResult(
                    success=False,
                    error=f"no hunks matched selection {list(params.hunks)!r}",
                    data={"available_hunks": [{"index": h["index"], "hash": h["hash"]} for h in hunks]},
                )

            # Apply selected hunks to baseline → new baseline (closer to current).
            base_norm = baseline if baseline.endswith("\n") or not baseline else baseline + "\n"
            base_lines = base_norm.splitlines()
            new_base_lines = _apply_hunks_to(base_lines, selected, direction="forward")
            new_baseline = "\n".join(new_base_lines)
            if base_norm.endswith("\n"):
                new_baseline += "\n"

            # Persist new baseline.
            if ws and sid != "_default_":
                try:
                    from digitorn.modules.preview.fs_backend import write_baseline
                    write_baseline(ws, sid, path, new_baseline)
                except Exception as exc:
                    logger.warning(
                        "approve_hunks_baseline_persist_failed path=%s: %s", path, exc,
                    )
            # Recompute pending vs new baseline.
            pending_diff = _safe_unified_diff(new_baseline, current, path)
            remaining = _parse_unified_diff_hunks(pending_diff)
            # Any remaining → still pending; none → fully approved.
            new_validation = "approved" if not remaining else "pending"
            new_ins, new_del = _count_pending_from_hunks(remaining)
            from digitorn.modules.preview.module import PatchResourceParams
            import time as _time
            await preview.patch_resource(PatchResourceParams(
                channel="files", id=path,
                patch={
                    "validation": new_validation,
                    "insertions_pending": new_ins,
                    "deletions_pending": new_del,
                    "baseline_lines": len(new_baseline.splitlines()),
                    "unified_diff_pending": pending_diff[:_PENDING_DIFF_MAX_BYTES],
                    "updated_at": _time.time(),
                },
            ))
            return ActionResult(success=True, data={
                "path": path,
                "approved_hunks": [{"index": h["index"], "hash": h["hash"]} for h in selected],
                "remaining_hunks": [{"index": h["index"], "hash": h["hash"]} for h in remaining],
                "validation": new_validation,
            })

    @action(
        description="Reject only specific hunks of a file (partial revert).",
        params_model=HunksActionParams,
        risk_level="low",
        tags=["workspace", "files", "validation", "hunks"],
        cli_label="Reject hunks",
        cli_param="path",
        internal=True,
    )
    async def reject_file_hunks(self, params: HunksActionParams) -> ActionResult:
        preview = self._get_preview()
        path = self._resolve_ws_path(params.path)
        sid = self._preview_session_id() or "_default_"
        # Per-path lock: same rationale as approve_file_hunks. The
        # baseline read + hunk parse + reverse-apply + channel write
        # sequence must be atomic against concurrent agent writes,
        # otherwise the reverse patch lands on the wrong content.
        async with self._path_lock(sid, path):
            existing = self._channel().get(path)
            if not existing:
                return ActionResult(
                    success=False, error=f"file not found in workspace: {path}",
                )
            current = existing.get("content", "") or ""
            ws = self._get_session_workspace_for_baseline()
            baseline = ""
            if ws and sid != "_default_":
                try:
                    from digitorn.modules.preview.fs_backend import read_baseline
                    baseline = read_baseline(ws, sid, path) or ""
                except Exception:
                    baseline = ""

            diff = _safe_unified_diff(baseline, current, path)
            hunks = _parse_unified_diff_hunks(diff)
            selected = _select_hunks(hunks, list(params.hunks))
            if not selected:
                return ActionResult(
                    success=False,
                    error=f"no hunks matched selection {list(params.hunks)!r}",
                    data={"available_hunks": [{"index": h["index"], "hash": h["hash"]} for h in hunks]},
                )

            # Revert selected hunks in the current content (current → baseline for those hunks).
            cur_norm = current if current.endswith("\n") or not current else current + "\n"
            cur_lines = cur_norm.splitlines()
            new_cur_lines = _apply_hunks_to(cur_lines, selected, direction="reverse")
            new_current = "\n".join(new_cur_lines)
            if cur_norm.endswith("\n"):
                new_current += "\n"

            # Update payload + disk.
            payload = self._make_payload(
                path, new_current,
                old_content=current,
                operation="write",
            )
            from digitorn.modules.preview.module import SetResourceParams
            await preview.set_resource(SetResourceParams(
                channel="files", id=path, payload=payload,
            ))
            await asyncio.to_thread(self._sync_write_to_disk, path, new_current)
            return ActionResult(success=True, data={
                "path": path,
                "reverted_hunks": [{"index": h["index"], "hash": h["hash"]} for h in selected],
            })

    @action(
        description="User-side writeback (manual edit or conflict resolution).",
        params_model=WritebackParams,
        risk_level="low",
        tags=["workspace", "files", "user"],
        cli_label="Writeback",
        cli_param="path",
        internal=True,
    )
    async def writeback_file(self, params: WritebackParams) -> ActionResult:
        preview = self._get_preview()
        path = self._resolve_ws_path(params.path)
        sid = self._preview_session_id() or "_default_"
        async with self._path_lock(sid, path):
            existing = self._channel().get(path)
            old_content = existing.get("content") if existing else None
            # Snapshot session baseline so future diffs work correctly,
            # mirrors what ``write()`` and ``edit()`` do.
            if existing is None:
                disk_before = (
                    await asyncio.to_thread(self._read_from_disk, path)
                    if self._sync_to_disk else None
                )
                if disk_before is not None and disk_before != params.content:
                    await self._ensure_session_baseline(path, disk_before)
                else:
                    await self._ensure_session_baseline(path, params.content)
            else:
                await self._ensure_session_baseline(path, old_content or "")
            payload = self._make_payload(
                path, params.content,
                old_content=old_content,
                operation="edit" if existing else "write",
            )
            payload["source"] = "user"
            if params.auto_approve:
                payload["validation"] = "approved"
                payload["insertions_pending"] = 0
                payload["deletions_pending"] = 0
                payload["baseline_lines"] = params.content.count("\n") + 1
            from digitorn.modules.preview.module import SetResourceParams
            await preview.set_resource(SetResourceParams(
                channel="files", id=path, payload=payload,
            ))
            await asyncio.to_thread(self._sync_write_to_disk, path, params.content)
        # Baseline persistence: either the module-level auto_approve flag
        # OR the per-call auto_approve param triggers it.
        await asyncio.to_thread(self._maybe_auto_approve_baseline, path, params.content)
        if params.auto_approve and not self._auto_approve:
            ws = self._get_session_workspace_for_baseline()
            sid = self._preview_session_id()
            if ws and sid:
                try:
                    from digitorn.modules.preview.fs_backend import write_baseline
                    write_baseline(ws, sid, path, params.content)
                except Exception as exc:
                    logger.warning(
                        "writeback_auto_approve_baseline_failed path=%s: %s", path, exc,
                    )
        # Run diagnostics on the writeback content - same lint pipeline
        # ``write()`` uses, so user-side PUTs surface JSON / YAML / TOML
        # / Python / LaTeX errors immediately. Without this, the agent's
        # write returned ``lint``/``errors``/``warnings`` but the user's
        # PUT returned a bare ``{path, size, validation}`` envelope and
        # the editor had to re-run validation client-side or wait for
        # the next agent turn to spot the same syntax error.
        lint_result: list[dict[str, Any]] = []
        try:
            lint_result = await self._run_lint(path, params.content)
        except Exception as _lint_exc:
            logger.debug("writeback_lint_failed path=%s: %s", path, _lint_exc)

        result_data: dict[str, Any] = {
            "path": path,
            "size": len(params.content),
            "validation": payload.get("validation", "pending"),
        }
        if lint_result:
            errors = [d for d in lint_result if d.get("severity") == "error"]
            warnings = [d for d in lint_result if d.get("severity") != "error"]
            result_data["lint"] = lint_result
            result_data["errors"] = len(errors)
            result_data["warnings"] = len(warnings)
        return ActionResult(success=True, data=result_data)

    @action(
        description="Commit the session workspace to git.",
        params_model=CommitParams,
        risk_level="medium",
        tags=["workspace", "git", "commit"],
        cli_label="Commit",
        internal=True,
    )
    async def commit_session(self, params: CommitParams) -> ActionResult:
        ws = self._get_session_workspace_for_baseline()
        if not ws:
            return ActionResult(success=False, error="no workspace dir")
        from pathlib import Path as _Path
        if not (_Path(ws) / ".git").is_dir():
            return ActionResult(
                success=False, error=f"workspace is not a git repo: {ws}",
            )

        # Pick which files to commit.
        if params.files is None:
            files = [
                p for p, pl in self._channel().items()
                if pl.get("validation") == "approved"
            ]
        else:
            files = [self._resolve_ws_path(p) for p in params.files]
        if not files:
            return ActionResult(
                success=False,
                error="no files to commit (all approved = none, or list was empty)",
            )

        import asyncio as _asyncio
        import subprocess as _sp

        def _run_git() -> dict[str, Any]:
            try:
                _sp.run(
                    ["git", "add", "--"] + files,
                    cwd=ws, check=True, capture_output=True, text=True,
                )
                r = _sp.run(
                    ["git", "commit", "-m", params.message],
                    cwd=ws, check=True, capture_output=True, text=True,
                )
                sha_r = _sp.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=ws, check=True, capture_output=True, text=True,
                )
                branch_r = _sp.run(
                    ["git", "symbolic-ref", "--short", "HEAD"],
                    cwd=ws, check=False, capture_output=True, text=True,
                )
                pushed = False
                if params.push:
                    _sp.run(
                        ["git", "push"], cwd=ws, check=True,
                        capture_output=True, text=True,
                    )
                    pushed = True
                return {
                    "commit_sha": sha_r.stdout.strip(),
                    "branch": branch_r.stdout.strip() if branch_r.returncode == 0 else "",
                    "files_committed": files,
                    "commit_stdout": r.stdout.strip(),
                    "pushed": pushed,
                }
            except _sp.CalledProcessError as exc:
                return {
                    "error": f"git failed: {exc.cmd} - stderr={exc.stderr}",
                }

        result = await _asyncio.to_thread(_run_git)
        if "error" in result:
            return ActionResult(success=False, error=result["error"])
        return ActionResult(success=True, data=result)

    @action(
        description="Refresh git_status for every tracked workspace file.",
        params_model=GitStatusParams,
        risk_level="low",
        tags=["workspace", "files", "git"],
        cli_label="Git status",
        internal=True,
    )
    async def git_status(self, params: GitStatusParams) -> ActionResult:
        ws = self._get_session_workspace_for_baseline()
        if not ws:
            return ActionResult(
                success=False, error="no workspace dir for git status",
            )
        # Bail out cleanly when the workspace isn't a git repo - don't
        # fall through to the "committed" default loop below, which
        # would lie about the state of every file (no repo = no commit
        # history, the right answer is null/unknown).
        if not (Path(ws) / ".git").is_dir():
            return ActionResult(
                success=True,
                data={"classified": 0, "is_git_repo": False},
            )
        statuses = await _run_git_status(ws)
        preview = self._get_preview()
        from digitorn.modules.preview.module import PatchResourceParams
        # Patch every file we know about - set to committed/unknown by
        # default, overridden when git returns a status for the path.
        seen: set[str] = set()
        for rel_path, status in statuses.items():
            norm = self._resolve_ws_path(rel_path)
            seen.add(norm)
            if self._channel().get(norm):
                await preview.patch_resource(PatchResourceParams(
                    channel="files", id=norm,
                    patch={"git_status": status},
                ))
        for norm_path in list(self._channel().keys()):
            if norm_path not in seen:
                await preview.patch_resource(PatchResourceParams(
                    channel="files", id=norm_path,
                    patch={"git_status": "committed"},
                ))
        return ActionResult(
            success=True,
            data={"classified": len(self._channel()), "is_git_repo": True},
        )


async def _run_git_status(workspace: str) -> dict[str, str]:
    """Return {rel_path: status} by running ``git status --porcelain``.

    Status mapping (simplified VS Code-like):
        "??" → untracked
        " M", "MM", "AM" → unstaged
        "M ", "A ", "D " → staged
        "UU", "AA", "DD" → conflict
    Returns empty dict if not a git repo or git unavailable.
    """
    import asyncio as _aio
    try:
        proc = await _aio.create_subprocess_exec(
            "git", "status", "--porcelain", "-z",
            cwd=workspace,
            stdout=_aio.subprocess.PIPE,
            stderr=_aio.subprocess.PIPE,
        )
        stdout, _stderr = await _aio.wait_for(proc.communicate(), timeout=5.0)
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for entry in stdout.decode("utf-8", errors="replace").split("\x00"):
        if not entry or len(entry) < 3:
            continue
        code = entry[:2]
        path = entry[3:]
        if code == "??":
            out[path] = "untracked"
        elif code in ("UU", "AA", "DD", "AU", "UA", "UD", "DU"):
            out[path] = "conflict"
        elif code[0] != " " and code[1] == " ":
            out[path] = "staged"
        elif code[0] == " " and code[1] != " ":
            out[path] = "unstaged"
        elif code[0] != " " and code[1] != " ":
            out[path] = "staged"
        else:
            out[path] = "committed"
    return out
