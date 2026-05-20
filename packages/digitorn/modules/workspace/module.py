"""Workspace Module - universal virtual filesystem for live-preview apps."""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import logging
import os
import re
import time
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
    before_norm = before if before.endswith("\n") or not before else before + "\n"
    after_norm = after if after.endswith("\n") or not after else after + "\n"
    return "".join(unified_diff(
        before_norm.splitlines(keepends=True),
        after_norm.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=n,
    ))

_PENDING_DIFF_MAX_BYTES = 200_000

def _parse_unified_diff_hunks(diff: str) -> list[dict[str, Any]]:
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
    p = path.replace("\\", "/")
    # Strip leading ./ (but not ../)
    while p.startswith("./"):
        p = p[2:]
    # Strip leading / (workspace paths are relative)
    p = p.lstrip("/")
    return p

def _glob_match(path: str, pattern: str) -> bool:
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
    if len(s) < 16:
        return False
    # Sample first 64 chars
    sample = s[:64]
    return all(c.isalnum() or c in "+/=\n\r" for c in sample)

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
    """Mark a file's current content as the new baseline (VS Code-like."""
    path: str = Field(..., description="Workspace-relative file path.")

class RejectFileParams(BaseModel):
    """Revert a file to its last-approved baseline. If no baseline exists."""
    path: str = Field(..., description="Workspace-relative file path.")

class HunksActionParams(BaseModel):
    """Apply approve / reject to a selection of hunks inside a file."""
    path: str = Field(..., description="Workspace-relative file path.")
    hunks: list = Field(
        default_factory=list,
        description="Selected hunks (indices or hashes).",
    )

class WritebackParams(BaseModel):
    """User-side write to a workspace file (manual edit, conflict."""
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
            "`workspace_path` is passed at session creation) or an "
            "auto-isolated per-session dir at "
            "`~/.digitorn/workspaces/{app_id}/{session_id}/`. "
            "Disk-backing unlocks LSP (pyright/ruff/tsserver can see the "
            "files), git tooling, cross-restart persistence, and the "
            "Lovable flow. Set explicitly to `false` only when you need "
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
            "`validation` stays `approved` and pending counters are "
            "always zero. No human review step. Use for sandbox apps, "
            "automated pipelines, or agents whose output is trusted by "
            "contract. When false (default), each change lands with "
            "`validation='pending'` until the user or a hook approves "
            "it explicitly. Per-write override: pass "
            "`WritebackParams(auto_approve=true)` on a one-off write."
        ),
    )
    hidden_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Glob patterns of files HIDDEN from the agent's view. The "
            "files exist on disk and the SDK iframe / web client can "
            "read+write them via HTTP, but agent tools (WsRead, WsGlob, "
            "WsGrep, WsEdit, WsDelete) treat them as if they didn't "
            "exist. Use for app-private state the SDK manages and the "
            "agent must not touch (auth caches, user prefs, telemetry). "
            "Patterns use POSIX glob with `**` for recursion.\n\n"
            "Defaults already-hidden (always, even with no config):\n"
            "  - `__sdk__/**`       (SDK-private namespace)\n"
            "  - `.app/**`          (app-private dotdir)\n"
            "  - `.digitorn/**`     (daemon metadata)\n\n"
            "Examples:\n"
            "  hidden_paths:\n"
            "    - 'auth/**'\n"
            "    - '*.secret'\n"
            "    - 'cache/**'"
        ),
    )
    agent_root: str = Field(
        default="",
        description=(
            "When non-empty, the agent's view of the workspace is "
            "scoped to this single subdirectory. Every path that "
            "doesn't start with `agent_root` is treated as hidden "
            "(WsRead returns 'file not found', WsGlob skips it, etc.). "
            "The SDK iframe and HTTP endpoints stay unconstrained - "
            "this is purely an agent-side guardrail.\n\n"
            "Used by the chat attachments tool-mode to lock the agent "
            "to the `attachments/` directory so it can't accidentally "
            "read app-private state via `..` or absolute paths.\n\n"
            "Example: `agent_root: \"attachments\"` → the agent can "
            "WsRead anything under `attachments/` but `WsRead("
            "\"config.json\")` returns not-found."
        ),
    )

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
        # Per-session flag: did we publish workspace metadata yet?
        # Must be per-session because the workspace module is isolation=shared.
        self._meta_published: dict[str, bool] = {}
        # Per-session last published is_git_repo flag; re-publishes on git init/rm.
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
        self._hidden_globs: list[str] = []
        # Single agent-facing directory whitelist. Empty = no scope
        # restriction. Set to e.g. "attachments" to lock the agent's
        # view to that one subdir (chat attachments tool-mode).
        self._agent_root: str = ""
        # Per-(session, file) diagnostic-push generation counter.
        self._diag_gen: dict[tuple[str, str], int] = {}
        # Per-(session, file) mutation lock; LRU-evicted when oversized.
        self._path_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._path_lock_last_used: dict[tuple[str, str], float] = {}

    _PATH_LOCKS_MAX: int = 50_000

    def _path_lock(self, sid: str, path: str) -> asyncio.Lock:
        import time as _time
        key = (sid, path)
        lock = self._path_locks.get(key)
        now = _time.monotonic()
        if lock is None:
            if len(self._path_locks) >= self._PATH_LOCKS_MAX:
                self._evict_path_locks(target=self._PATH_LOCKS_MAX // 10)
            lock = asyncio.Lock()
            self._path_locks[key] = lock
        self._path_lock_last_used[key] = now
        return lock

    def _evict_path_locks(self, target: int) -> int:
        # Sort by last-used time ascending (oldest first).
        sorted_keys = sorted(
            self._path_lock_last_used.items(), key=lambda kv: kv[1],
        )
        evicted = 0
        for key, _ in sorted_keys:
            if evicted >= target:
                break
            lock = self._path_locks.get(key)
            if lock is None or lock.locked():
                continue
            self._path_locks.pop(key, None)
            self._path_lock_last_used.pop(key, None)
            evicted += 1
        return evicted

    async def cleanup_session(self, session_id: str) -> None:
        """Drop per-session bookkeeping when the session ends."""
        if not session_id:
            return
        self._meta_published.pop(session_id, None)
        for key in list(self._path_locks.keys()):
            if key[0] == session_id:
                self._path_locks.pop(key, None)
                self._path_lock_last_used.pop(key, None)
        for key in list(self._diag_gen.keys()):
            if key[0] == session_id:
                self._diag_gen.pop(key, None)

    def _resolve_ws_path(self, path: str) -> str:
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
            # Out-of-workdir absolute path: enforce the sandbox when
            # one is in scope, otherwise normalize and let the caller
            # see the path (legacy CLI / test compatibility).
            ctx = self._context_var.get()
            policy = getattr(ctx, "path_policy", None) if ctx else None
            if policy is not None:
                # Raises PermissionDeniedError when out-of-sandbox.
                policy.enforce(p)
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
        # Hidden globs: always cover the SDK-private namespaces and
        # merge in any app-declared extras.
        _DEFAULT_HIDDEN = ["__sdk__/**", ".app/**", ".digitorn/**"]
        self._hidden_globs = list(_DEFAULT_HIDDEN) + list(cfg.hidden_paths or [])
        # `agent_root` whitelist for tool-mode attachments.
        self._agent_root = (cfg.agent_root or "").strip().strip("/")
        # Force every active session to republish meta on next write.
        self._meta_published.clear()

    def get_dynamic_tool_prompts(self) -> dict[str, str]:
        """Return per-FQN tool prompts, merging base + app instructions."""
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
        return self._get_preview()._session().channel("files")

    async def register_attachment(
        self,
        session_id: str,
        name: str,
        text: str,
        *,
        mime: str = "text/plain",
        file_id: str = "",
        target_dir: str = "attachments",
    ) -> str | None:
        """Mirror an extracted attachment / source's text into the workspace."""
        if self._preview is None or not text:
            return None
        # Whitelist target dirs (no traversal, no nested paths).
        if target_dir not in ("attachments", "sources"):
            logger.warning(
                "register_attachment_invalid_target session=%s target=%s",
                session_id[:8], target_dir,
            )
            return None

        from digitorn.modules.preview.module import SetResourceParams

        # Sanitise the filename; this also defeats path traversal.
        safe = re.sub(r"[^A-Za-z0-9._\-() ]+", "_", name).strip("._ ") or "file"
        path = f"{target_dir}/{safe}"

        lines = text.count("\n") + 1
        payload: dict[str, Any] = {
            "content": text,
            "language": "text",
            "size": len(text),
            "lines": lines,
            "operation": "write",
            "updated_at": time.time(),
            "validation": "approved",
            "baseline_lines": lines,
            "source": "attachment" if target_dir == "attachments" else "source",
            "mime": mime,
            "file_id": file_id,
        }

        # Idempotent per-task; binds `_channel` / `set_resource` to
        # the right `PreviewSessionState` outside an agent turn.
        self._preview.set_active_session(session_id)
        try:
            self._channel()[path] = payload
            try:
                await self._preview.set_resource(SetResourceParams(
                    channel="files", id=path, payload=payload,
                ))
            except Exception as exc:
                logger.debug(
                    "register_attachment_publish_failed path=%s: %s",
                    path, exc,
                )
        except Exception as exc:
            logger.warning(
                "register_attachment_failed session=%s name=%s err=%s",
                session_id[:8], name, exc,
            )
            return None
        return path

    def _is_hidden_from_agent(self, path: str) -> bool:
        norm = path.replace("\\", "/").lstrip("/") if path else ""
        # `agent_root` whitelist takes priority over `hidden_paths`.
        root = (getattr(self, "_agent_root", "") or "").replace("\\", "/").strip("/")
        if root:
            if not norm:
                return True
            if norm != root and not norm.startswith(root + "/"):
                return True
        if not self._hidden_globs or not path:
            return False
        from fnmatch import fnmatch
        for pat in self._hidden_globs:
            p = pat.replace("\\", "/").lstrip("/")
            # `**` recursive match: `foo/**` matches foo/anything,
            # foo/a/b, foo/.x, etc. Translate to a prefix check.
            if p.endswith("/**"):
                prefix = p[:-3]
                if not prefix or norm == prefix or norm.startswith(prefix + "/"):
                    return True
                continue
            if "**" in p:
                # Generic ** anywhere - flatten by replacing with *.
                # Lossy but covers 90% of cases (most patterns end in /**).
                if fnmatch(norm, p.replace("**", "*")):
                    return True
                continue
            if fnmatch(norm, p):
                return True
        return False

    def _make_payload(
        self,
        path: str,
        content: str,
        *,
        old_content: str | None = None,
        operation: str = "write",
    ) -> dict[str, Any]:
        import time as _time
        lang = _detect_language(path)
        lines = content.count("\n") + 1

        # Read previous cumulative totals from existing payload
        existing = self._channel().get(path)
        prev_total_ins = existing.get("total_insertions", 0) if existing else 0
        prev_total_del = existing.get("total_deletions", 0) if existing else 0

        # Preserve validation + baseline-diff state across the payload
        # rebuild. `validation` defaults to "pending" - the agent just
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

        # Pending-since-baseline counters (delta vs last-approved
        # baseline, not a running sum) for VS Code-style diff gutters.
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
            # No baseline yet: everything is pending. `splitlines`
            # avoids the trailing-newline off-by-one.
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

    # Cache per-workspace for `_GIT_REPO_TTL_S` so the sync `is_dir`
    # stat doesn't stall the loop on slow disks.
    _GIT_REPO_TTL_S: float = 5.0

    def _is_git_repo(self) -> bool:
        ws = self._get_session_workspace_for_baseline()
        if not ws:
            return False
        # Lazy-init cache on the instance so subclasses don't need it.
        cache = getattr(self, "_git_repo_cache", None)
        if cache is None:
            cache = {}
            setattr(self, "_git_repo_cache", cache)
        import time as _time
        now = _time.monotonic()
        cached = cache.get(ws)
        if cached is not None and (now - cached[1]) < self._GIT_REPO_TTL_S:
            return cached[0]
        try:
            flag = (Path(ws) / ".git").is_dir()
        except Exception:
            flag = False
        cache[ws] = (flag, now)
        # Cap at 1000 distinct workspaces, FIFO-trim 200 at a time.
        if len(cache) > 1000:
            for k in list(cache)[:200]:
                cache.pop(k, None)
        return flag

    def _resolve_app_id(self) -> str:
        try:
            ctx = self._context_var.get()
        except Exception:
            ctx = None
        ctx_app = getattr(ctx, "app_id", "") if ctx is not None else ""
        if ctx_app:
            return ctx_app
        return (
            getattr(self, "_app_id_override", None)
            or getattr(self, "_app_id", "default")
        )

    def _resolve_sync_dir(self) -> str | None:
        # 1. User-chosen workspace (Lovable) - unconditional.
        try:
            preview = self._get_preview()
            sid = preview._resolve_session_id()
            ws_map = getattr(preview, "_session_workspaces", {}) or {}
            user_ws = ws_map.get(sid)
            if user_ws:
                return os.path.abspath(user_ws)
        except Exception as exc:
            logger.debug("module best-effort block failed: %s", exc)

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
        # only: set `sync_to_disk: false` in workspace config.
        if self._sync_to_disk is False:
            return None
        try:
            preview = self._get_preview()
            sid = preview._resolve_session_id()
            if sid and sid != "_default_":
                app_id = self._resolve_app_id()
                return os.path.join(
                    str(Path.home()), ".digitorn", "workspaces",
                    app_id, sid,
                )
        except Exception as exc:
            logger.debug("module best-effort block failed: %s", exc)
        if ctx is not None and getattr(ctx, "session_id", None):
            app_id = self._resolve_app_id()
            return os.path.join(
                str(Path.home()), ".digitorn", "workspaces",
                app_id, ctx.session_id,
            )

        # 5. Last resort - app-level workspace.
        ws = getattr(self, "_workspace", None)
        return os.path.abspath(ws) if ws else None

    def _resolve_daemon_dir(self) -> str | None:
        try:
            preview = self._get_preview()
            sid = preview._resolve_session_id()
            daemon_map = getattr(preview, "_session_daemon_dirs", {}) or {}
            daemon = daemon_map.get(sid)
            if daemon:
                return os.path.abspath(daemon)
        except Exception as exc:
            logger.debug("module best-effort block failed: %s", exc)
        # Fallback: derive from app_id + active session_id. Same shape
        # as the auto-isolated path used when no user workdir was set.
        try:
            preview = self._get_preview()
            sid = preview._resolve_session_id()
            if sid and sid != "_default_":
                app_id = self._resolve_app_id()
                return os.path.join(
                    str(Path.home()), ".digitorn", "workspaces",
                    app_id, sid,
                )
        except Exception as exc:
            logger.debug("module best-effort block failed: %s", exc)
        return None

    def _resolve_disk_dir_for(self, path: str) -> str | None:
        if self._is_hidden_from_agent(path):
            return self._resolve_daemon_dir()
        return self._resolve_sync_dir()

    @staticmethod
    def _join_inside(sync_dir: str, rel_path: str) -> str | None:
        from pathlib import Path as _Path
        full = _Path(os.path.join(sync_dir, rel_path)).resolve(strict=False)
        try:
            full.relative_to(_Path(sync_dir).resolve(strict=False))
        except ValueError:
            return None
        return str(full)

    def _sync_write_to_disk(self, path: str, content: str) -> None:
        sync_dir = self._resolve_disk_dir_for(path)
        if sync_dir is None:
            return
        full = self._join_inside(sync_dir, path)
        if full is None:
            logger.warning(
                "workspace_sync_write_blocked path=%s sync_dir=%s "
                "reason=path-escapes-sync-dir",
                path, sync_dir,
            )
            return
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
        sync_dir = self._resolve_disk_dir_for(path)
        if sync_dir is None:
            return
        full = self._join_inside(sync_dir, path)
        if full is None:
            logger.warning(
                "workspace_sync_delete_blocked path=%s sync_dir=%s "
                "reason=path-escapes-sync-dir",
                path, sync_dir,
            )
            return
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
            app_id = self._resolve_app_id()
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

        # Recursive `os.scandir` with descent-time pruning: skipping
        # `node_modules` etc. before readdir is the difference between
        # 200 ms and 5 s on a typical Node project.
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
                            # `.github` (CI files visible to the
                            # user).
                            if name.startswith(".") and name != ".github":
                                continue
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        # Leaf-name dot-file filter (dir gate already
                        # vetted the path); allowlist covers user-edited
                        # dot-files like `.env`.
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
        if not self._lint:
            await self._publish_diagnostics(path, [])
            return []

        items: list[dict[str, Any]] = []

        if self._lsp is not None:
            try:
                from digitorn.modules.lsp.params import NotifyChangeParams
                # Linter / compiler subprocesses read from disk, so hand
                # them the resolved on-disk path when `sync_to_disk`
                # is on. LSP server mode accepts either shape.
                lsp_path = path
                try:
                    disk_dir = self._resolve_disk_dir_for(path)
                    if disk_dir:
                        full = self._join_inside(disk_dir, path)
                        if full:
                            lsp_path = full
                except Exception as exc:
                    logger.debug("module best-effort block failed: %s", exc)

                from digitorn.modules.base import (
                    BaseModule as _BaseModule,
                    ExecutionContext as _ExecutionContext,
                )
                _own_app_id = getattr(self, "_app_id_override", None) or getattr(
                    self, "_app_id", None,
                )
                _existing_ctx = _BaseModule._context_var.get()
                _ctx_token = None
                if _own_app_id and (
                    _existing_ctx is None
                    or not getattr(_existing_ctx, "app_id", None)
                ):
                    _ctx_token = _BaseModule._context_var.set(
                        _ExecutionContext(
                            plan_id=getattr(_existing_ctx, "plan_id", "") or "workspace:lint",
                            action_id=getattr(_existing_ctx, "action_id", "") or "workspace.lint",
                            app_id=_own_app_id,
                            session_id=getattr(_existing_ctx, "session_id", None) or self._preview_session_id() or None,
                            user_id=getattr(_existing_ctx, "user_id", "") or "local",
                            workspace=getattr(_existing_ctx, "workspace", None) or self._workspace or None,
                        ),
                    )
                try:
                    result = await self._lsp.notify_change(
                        NotifyChangeParams(path=lsp_path, content=content),
                    )
                finally:
                    if _ctx_token is not None:
                        _BaseModule._context_var.reset(_ctx_token)
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
        sync_dir = self._resolve_disk_dir_for(path)
        if sync_dir is None:
            return None
        full = os.path.join(sync_dir, path)
        if not os.path.isfile(full):
            # Fallback: try the alternate dir for SDK apps that may
            # have written under the wrong root.
            alt_dir = (
                self._resolve_sync_dir() if self._is_hidden_from_agent(path)
                else self._resolve_daemon_dir()
            )
            if alt_dir and alt_dir != sync_dir:
                alt_full = os.path.join(alt_dir, path)
                if os.path.isfile(alt_full):
                    full = alt_full
                else:
                    return None
            else:
                return None
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except (OSError, PermissionError):
            return None

    # Disk paths the IDE view never needs to see; skipped during
    # recursive sync after Bash commands.
    _DISK_SYNC_IGNORE_DIRS: frozenset[str] = frozenset({
        # VCS
        ".git", ".svn", ".hg",
        # Internal
        ".digitorn", ".cache", ".parcel-cache",
        # Node
        "node_modules", ".pnpm-store",
        # Build outputs (kept-as-source if user explicitly writes there)
        "dist", "build", "out", "target",
        ".next", ".nuxt", ".svelte-kit", ".turbo", ".vite", ".astro",
        ".vercel", ".netlify",
        # Python
        "__pycache__", ".pytest_cache", ".mypy_cache", ".tox",
        ".venv", "venv", "env", ".env-venv",
        # Coverage / artefacts
        "coverage", ".nyc_output",
        # iOS / native
        "Pods", "DerivedData",
        # PHP / Ruby
        "vendor",
    })

    # Per-file size cap. Above this, only metadata is sent (the IDE
    # can lazy-load on click). Prevents huge JSON / SVG / minified JS
    # from saturating the SSE pipe.
    _DISK_SYNC_MAX_FILE_BYTES: int = 512 * 1024  # 512 KB

    # Ceiling on files synced per call to keep the runtime bounded
    # even in pathological cases (huge repos, agent oversights).
    _DISK_SYNC_MAX_FILES: int = 5000

    async def _sync_from_disk(self) -> dict[str, int]:
        if self._sync_to_disk is False:
            return {"added": 0, "modified": 0, "deleted": 0, "skipped": 0}
        sync_dir = self._resolve_sync_dir()
        if sync_dir is None or not os.path.isdir(sync_dir):
            return {"added": 0, "modified": 0, "deleted": 0, "skipped": 0}

        try:
            preview = self._get_preview()
        except RuntimeError:
            return {"added": 0, "modified": 0, "deleted": 0, "skipped": 0}

        from digitorn.modules.preview.module import (
            SetResourceParams,
            DeleteResourceParams,
        )

        existing_channel = dict(self._channel())  # snapshot
        seen: set[str] = set()
        added = modified = skipped = 0
        ignore = self._DISK_SYNC_IGNORE_DIRS
        max_files = self._DISK_SYNC_MAX_FILES
        max_bytes = self._DISK_SYNC_MAX_FILE_BYTES

        # Prune `node_modules` / `.git` / etc. at descent time so
        # `npm install` repos stay fast. Dot-files at the workspace
        # root are filtered below at the file level.
        for dirpath, dirnames, filenames in os.walk(sync_dir):
            dirnames[:] = [d for d in dirnames if d not in ignore and not d.startswith(".")]
            for fname in filenames:
                if added + modified > max_files:
                    skipped += 1
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    rel = os.path.relpath(full, sync_dir).replace("\\", "/")
                except ValueError:
                    continue
                # File-level skip: never expose .digitorn metadata, OS
                # detritus (.DS_Store, Thumbs.db).
                if rel.startswith(".digitorn/") or rel.startswith(".git/"):
                    continue
                if fname in (".DS_Store", "Thumbs.db"):
                    continue

                seen.add(rel)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                size = st.st_size

                existing = existing_channel.get(rel)

                # Skip oversized files for content but still register
                # them in the channel (metadata-only) so they appear
                # in the IDE tree.
                if size > max_bytes:
                    import time as _time
                    payload = {
                        "size": size,
                        "language": _detect_language(rel),
                        "operation": "disk_sync",
                        "validation": "approved",
                        "truncated": True,
                        "updated_at": _time.time(),
                    }
                    if existing and existing.get("size") == size:
                        continue  # no change, skip emit
                    try:
                        await preview.set_resource(SetResourceParams(
                            channel="files", id=rel, payload=payload,
                        ))
                    except Exception as exc:
                        logger.debug("module best-effort block failed: %s", exc)
                    if existing is None:
                        added += 1
                    else:
                        modified += 1
                    continue

                # Cheap change detection: size + mtime is good enough.
                disk_mtime = st.st_mtime
                cached_size = existing.get("size") if existing else None
                cached_mtime = existing.get("disk_mtime") if existing else None
                if (
                    existing is not None
                    and cached_size == size
                    and cached_mtime == disk_mtime
                ):
                    continue  # unchanged, no emit

                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                except OSError:
                    continue

                payload = self._make_payload(
                    rel, content,
                    old_content=existing.get("content") if existing else None,
                    operation="disk_sync",
                )
                payload["disk_mtime"] = disk_mtime
                # Disk-synced files arrive already-on-disk so they're
                # implicitly "approved" in the validation flow.
                payload["validation"] = "approved"

                try:
                    await preview.set_resource(SetResourceParams(
                        channel="files", id=rel, payload=payload,
                    ))
                except Exception as exc:
                    logger.debug("module best-effort block failed: %s", exc)

                if existing is None:
                    added += 1
                else:
                    modified += 1

        # Detect deletes: files in channel but not on disk anymore.
        deleted = 0
        for rel in list(existing_channel.keys()):
            if rel in seen:
                continue
            try:
                await preview.delete_resource(DeleteResourceParams(
                    channel="files", id=rel,
                ))
            except Exception as exc:
                logger.debug("module best-effort block failed: %s", exc)
            deleted += 1

        # Workspace meta might need a refresh when a brand-new project
        # appears (e.g. `npm create vite` with index.html). Cheap.
        if added > 0:
            try:
                await self._ensure_meta_published()
                await self._refresh_git_repo_flag()
            except Exception as exc:
                logger.debug("module best-effort block failed: %s", exc)

        return {
            "added": added,
            "modified": modified,
            "deleted": deleted,
            "skipped": skipped,
        }

    async def _ensure_meta_published(self, first_path: str | None = None) -> None:
        # Resolve the session before checking the flag; the workspace
        # module is shared so the publish must fire once per session.
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
        # Track what we just published so `_refresh_git_repo_flag` can
        # detect drift if the user runs `git init` mid-session.
        if sid:
            self._last_git_repo_flag[sid] = bool(meta["is_git_repo"])

    async def _refresh_git_repo_flag(self) -> None:
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
        # Rebuild + republish the `workspace` state bag (clients merge
        # it wholesale; preview has no per-field setter).
        meta: dict[str, Any] = {
            "render_mode": self._render_mode,
            "entry_file": self._entry_file,
            "is_git_repo": new_flag,
        }
        if self._title:
            meta["title"] = self._title
        from digitorn.modules.preview.module import SetStateParams
        await preview.set_state(SetStateParams(key="workspace", value=meta))

    @action(
        description="Create or overwrite a file. Streams live to the client.",
        params_model=WriteParams,
        risk_level="low",
        tags=["workspace", "files"],
        cli_label="Write",
        cli_param="path",
    )
    async def write(self, params: WriteParams) -> ActionResult:
        preview = self._get_preview()
        path = self._resolve_ws_path(params.path)
        sid = self._preview_session_id() or "_default_"

        # Refuse writes to SDK-private namespaces (`__sdk__/`,
        # `.app/`, ...); HTTP writeback bypasses this gate.
        if self._is_hidden_from_agent(path):
            return ActionResult(
                success=False,
                error=(
                    f"Refused: '{path}' is in a hidden namespace "
                    f"(reserved for SDK / app-internal state). The "
                    f"agent must not write here. Pick a different path."
                ),
            )

        # Per-path lock serialises concurrent writes on the same file.
        async with self._path_lock(sid, path):
            existing = self._channel().get(path)
            old_content = existing.get("content") if existing else None

            # First-touch baseline so `unified_diff_pending` has a
            # meaningful anchor: existing disk content if any, else the
            # content being written.
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
            # Re-detect `git init` so the Commit/Refresh buttons flip
            # back on mid-session.
            await self._refresh_git_repo_flag()

            from digitorn.modules.preview.module import SetResourceParams
            await preview.set_resource(SetResourceParams(
                channel="files", id=path, payload=payload,
            ))
            # Off-load the sync disk writes so Socket.IO keeps flowing.
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
        # Hidden-from-agent: pretend the file doesn't exist. The SDK
        # iframe can still read it via HTTP - this gate only affects
        # the agent's tool surface.
        if self._is_hidden_from_agent(path):
            return ActionResult(
                success=False,
                error=f"File not found: {path}",
            )
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

    @action(
        description="Surgical text replacement in an existing file.",
        params_model=EditParams,
        risk_level="low",
        tags=["workspace", "files"],
        cli_label="Edit",
        cli_param="path",
    )
    async def edit(self, params: EditParams) -> ActionResult:
        preview = self._get_preview()
        path = self._resolve_ws_path(params.path)
        sid = self._preview_session_id() or "_default_"

        # Hidden-from-agent: refuse, same as read().
        if self._is_hidden_from_agent(path):
            return ActionResult(
                success=False, error=f"File not found: {path}",
            )

        # Per-path lock: same reasoning as `write()` - serialise
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

        # Snapshot the pre-edit content as the session baseline so
        # successive edits accumulate against a stable anchor.
        await self._ensure_session_baseline(path, content)

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

        # Off-load the sync `glob` walk when syncing to disk; a deep
        # `**/*` over a large repo would stall Socket.IO.
        if self._sync_to_disk:
            import asyncio as _asyncio
            await _asyncio.to_thread(self._load_disk_files_matching, pattern)

        matched = []
        for path in ch:
            if self._is_hidden_from_agent(path):
                continue
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
        # See `glob()` above - the disk walk is sync and would stall
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
            if self._is_hidden_from_agent(path):
                continue
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

        # Hidden-from-agent: refuse, same as read/edit.
        if self._is_hidden_from_agent(path):
            return ActionResult(
                success=False, error=f"File not found: {path}",
            )

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
            except Exception as exc:
                logger.debug("module best-effort block failed: %s", exc)
            self._diag_gen.pop((sid, path), None)
            await asyncio.to_thread(self._sync_delete_from_disk, path)
            return ActionResult(
                success=True,
                data={"path": path, "deleted": result.data.get("existed", False)},
            )

    def _get_session_workspace_for_baseline(self) -> str | None:
        try:
            preview = self._get_preview()
            sid = preview._resolve_session_id()
            daemon_map = getattr(preview, "_session_daemon_dirs", {}) or {}
            ws = daemon_map.get(sid)
            if ws:
                return ws
            ws_map = getattr(preview, "_session_workspaces", {}) or {}
            ws = ws_map.get(sid)
            if ws:
                return ws
        except Exception as exc:
            logger.debug("module best-effort block failed: %s", exc)
        try:
            d = self._resolve_sync_dir()
            if d:
                return d
        except Exception as exc:
            logger.debug("module best-effort block failed: %s", exc)
        return None

    def _preview_session_id(self) -> str:
        preview = self._get_preview()
        try:
            return preview._resolve_session_id()
        except Exception:
            return ""

    def _maybe_auto_approve_baseline(self, path: str, content: str) -> None:
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
        # Caller is expected to hold `_path_lock(sid, path)` already
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
            # `record_in_history=False` keeps synthetic snapshots out
            # of the user-visible revision list. Only explicit
            # `approve_file` / `approve_file_hunks` get an entry.
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
        # Per-path lock: serialise against concurrent writes so the
        # approve snapshot + counter reset stay atomic.
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
            # Reset cumulative counters: the approved content IS the
            # new baseline. `updated_at` must bump so the client's
            # `wroteSinceLastRebuild` check fires.
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
        # Per-path lock against concurrent agent writes during reject.
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
            # Reject-as-delete when the user never explicitly approved
            # this path: either no baseline or only the session-start
            # auto-snapshot exists.
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
                    except Exception as exc:
                        logger.debug("module best-effort block failed: %s", exc)
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
        # Per-path lock so hunks always apply against the current
        # baseline, even under concurrent agent writes.
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
        # Per-path lock: same atomicity requirement as
        # `approve_file_hunks` against concurrent agent writes.
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
            # mirrors what `write()` and `edit()` do.
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
        # Re-run lint on the writeback so PUTs report the same
        # `lint` / `errors` / `warnings` as `write()`.
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
        if not (Path(ws) / ".git").is_dir():
            return ActionResult(
                success=True,
                data={"classified": 0, "is_git_repo": False},
            )
        statuses = await _run_git_status(ws)
        preview = self._get_preview()
        from digitorn.modules.preview.module import PatchResourceParams
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
