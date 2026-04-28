"""Filesystem module - 5 ultra-powerful actions for AI agents.

Design:
  1. MINIMAL params → LLM makes fewer mistakes
  2. POWERFUL implementations → fuzzy matching, auto-detection, smart errors
  3. RICH feedback → metadata, diffs, recovery hints for preview/frontend
  4. ERROR-FRIENDLY → closest matches, suggestions on failure

Actions:
  - Read: text, images, PDFs, notebooks with auto-detection
  - Write: creates parent dirs automatically, atomic writes
  - Edit: fuzzy matching + insert_at_line + recovery hints
  - Grep: ripgrep-powered search with multiline + context
  - Glob: pattern-based file finding with type filtering

Removed (use Bash instead):
  - ls, mv, cp, rm, mkdir, insert, find, file_stat, undo
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from digitorn.modules.base import ActionResult, BaseModule, Platform, ResourceEstimate
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ConstraintSpec, ModuleManifest

from .params import ReadParams, WriteParams, EditParams, GlobParams, GrepParams
from .helpers import (
    _reindent_replacement,
    fuzzy_find_old_string,
    find_closest_matches,
    is_binary_file,
    is_image_file,
    is_pdf_file,
    is_notebook_file,
    suggest_edit_recovery,
    suggest_read_recovery,
    suggest_glob_recovery,
    generate_diff_preview,
    gather_file_metadata,
)

_SYNC_THRESHOLD = 512 * 1024
_MAX_PDF_PAGES_PER_REQUEST = 20
_MAX_LINES_TO_READ = 2000


# BUG-077 + BUG-083: secrets that would let a caller forge tokens,
# decrypt every user's credentials, or impersonate the admin. These
# files MUST NOT be reachable from any agent-facing tool. The check is
# applied at the lowest layer (``_resolve_path``) so it catches
# ``filesystem.read``, ``filesystem.write``, ``filesystem.grep``, and
# ``filesystem.glob`` in one place. ``shell`` has its own equivalent.
def _assert_not_daemon_secret(abs_path: str) -> None:
    from digitorn.modules.exceptions import PermissionDeniedError

    _norm = abs_path.replace("\\", "/").lower()
    _home = os.path.expanduser("~").replace("\\", "/").lower()
    _sensitive = (
        f"{_home}/.digitorn/jwt.key",
        f"{_home}/.digitorn/master.key",
        f"{_home}/.digitorn/server.key",
        f"{_home}/.digitorn/digitorn.db",
        f"{_home}/.digitorn/config.yaml",
        f"{_home}/.claude/.credentials.json",
    )
    _sensitive_dirs = (
        f"{_home}/.digitorn/kv/",
        f"{_home}/.digitorn/sessions/",
        f"{_home}/.digitorn/state/",
        f"{_home}/.digitorn/logs/",
    )
    for needle in _sensitive:
        if _norm == needle:
            raise PermissionDeniedError(
                message=(
                    "Access to daemon-private files is denied. "
                    f"Path '{abs_path}' holds signing keys / master "
                    "encryption keys / DB - it can never be read or "
                    "written from a tool, even by admins."
                ),
                action="filesystem.read",
                module="filesystem",
                profile="daemon-secret-denylist",
            )
    for prefix in _sensitive_dirs:
        if _norm.startswith(prefix):
            raise PermissionDeniedError(
                message=(
                    f"Access to daemon-private directory is denied: "
                    f"'{abs_path}'."
                ),
                action="filesystem",
                module="filesystem",
                profile="daemon-secret-denylist",
            )


class FilesystemModule(BaseModule):
    """Filesystem module with 5 ultra-powerful actions."""

    MODULE_ID = "filesystem"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.LINUX, Platform.MACOS, Platform.WINDOWS]

    def __init__(self) -> None:
        super().__init__()
        # Injected by bootstrap when an LSP / preview module is loaded
        # in the same app. Both are optional - write/edit still work
        # without them, just without post-write diagnostics push.
        self._lsp: Any | None = None
        self._preview: Any | None = None
        # Per-(session, path) generation counter - same pattern as
        # workspace module. Prevents cross-session payload pollution
        # since FilesystemModule is also isolation=shared.
        self._diag_gen: dict[tuple[str, str], int] = {}

    async def on_start(self) -> None:
        """Module startup."""
        logger.info("Filesystem module started (5 actions: Read, Write, Edit, Grep, Glob)")

    async def _run_lint_and_publish(
        self, file_path: str, content: str,
    ) -> list[dict[str, Any]]:
        """Run LSP diagnostics on a freshly-written file and push to the
        `diagnostics` preview channel if available.

        Resolution: real LSP server first (ruff/pyright/eslint/texlab),
        then in-memory built-in validators (JSON/YAML/TOML/Python/LaTeX).
        Matches the workspace module's behavior exactly.

        Returns the flat-shape diagnostics (same format as workspace
        module's inline `lint` field) so the agent sees errors directly
        in the tool response. Also fires off an async publish to the
        preview channel when it's wired.
        """
        items: list[dict[str, Any]] = []

        # 1. Real LSP server via lsp module.
        if self._lsp is not None:
            try:
                from digitorn.modules.lsp.params import NotifyChangeParams
                result = await self._lsp.notify_change(
                    NotifyChangeParams(path=file_path, content=content),
                )
                if result.success and result.data:
                    items = result.data.get("diagnostics", []) or []
            except Exception as exc:
                logger.debug(
                    "filesystem_lsp_failed path=%s: %s", file_path, exc,
                )

        # 2. Built-in validators - reuse the workspace module's shared map
        #    so we don't drift on JSON/YAML/TOML/Python/LaTeX semantics.
        if not items:
            try:
                from digitorn.modules.workspace.module import (
                    _BUILTIN_CONTENT_VALIDATORS,
                )
                _, ext = os.path.splitext(file_path.lower())
                validator = _BUILTIN_CONTENT_VALIDATORS.get(ext)
                if validator:
                    items = validator(content, file_path) or []
            except Exception as exc:
                logger.debug(
                    "filesystem_builtin_lint_failed path=%s: %s",
                    file_path, exc,
                )

        # Publish to preview channel (key = path relative to workspace).
        if self._preview is not None:
            try:
                import time as _time
                from digitorn.modules.preview.module import SetResourceParams
                from digitorn.modules.lsp.parsers import Diagnostic as _Diag

                lsp_items: list[dict[str, Any]] = []
                severities_seen: set[str] = set()
                for it in items:
                    sev = (it.get("severity") or "info").lower()
                    severities_seen.add(sev)
                    d = _Diag(
                        file=it.get("file", file_path),
                        line=int(it.get("line") or 1),
                        column=int(it.get("column") or 1),
                        severity=sev,
                        message=it.get("message", ""),
                        code=str(it.get("code") or ""),
                        source=str(it.get("source") or ""),
                    )
                    lsp_items.append(d.to_lsp_dict())
                order = ("error", "warning", "info", "hint")
                severity_max = next(
                    (s for s in order if s in severities_seen), None,
                )

                try:
                    sid = self._preview._resolve_session_id()
                except Exception:
                    sid = ""
                rel_path = self._to_relative(file_path)
                key = (sid, rel_path)
                self._diag_gen[key] = self._diag_gen.get(key, 0) + 1

                await self._preview.set_resource(SetResourceParams(
                    channel="diagnostics",
                    id=rel_path,
                    payload={
                        "file_path": rel_path,
                        "items": lsp_items,
                        "generation": self._diag_gen[key],
                        "severity_max": severity_max,
                        "updated_at": _time.time(),
                        "source_module": "filesystem",
                    },
                ))
            except Exception as exc:
                logger.debug(
                    "filesystem_publish_diagnostics_failed path=%s: %s",
                    file_path, exc,
                )

        return items

    def _resolve_path(self, file_path: str) -> str:
        """Resolve a file path - relative paths are resolved from the workspace root."""
        p = os.path.expanduser(file_path)
        if not os.path.isabs(p):
            ws = self.workspace
            base = ws if ws else os.getcwd()
            p = os.path.join(base, p)
        resolved = os.path.normpath(p)
        # BUG-077 + BUG-083: even when a caller legitimately reaches
        # this module (admin token, agent with explicit workspace
        # grant, etc.) there is a hard-denied set of daemon-private
        # files whose exposure would let the caller impersonate the
        # daemon or decrypt every user's secrets. This is defence-in-
        # depth - BUG-061's admin gate on /api/modules/execute is the
        # primary lock, but a bug there must not be enough to leak
        # jwt.key or master.key.
        _assert_not_daemon_secret(resolved)
        return resolved

    def _to_relative(self, abs_path: str) -> str:
        """Convert an absolute path to workspace-relative (forward slashes).

        Returns the relative path the agent can pass directly to Read/Edit/Write.
        If the path is not under the workspace, returns it as-is.
        """
        ws = self.workspace
        if not ws:
            return abs_path.replace("\\", "/")
        try:
            rel = os.path.relpath(abs_path, ws)
            return rel.replace("\\", "/")
        except ValueError:
            return abs_path.replace("\\", "/")

    # ========================================================================
    # ACTION 1: Read
    # ========================================================================

    @action(
        description="Read a file from the local filesystem.",
        params_model=ReadParams,
        cli_label="Read",
        cli_param="file_path",
        tool_prompt=(
            "Read a file from the local filesystem. Returns content with line numbers.\n"
            "\n"
            "## When to use\n"
            "- ALWAYS read before editing - Edit will fail on files you haven't read\n"
            "- Read to understand code before proposing changes - never guess at file contents\n"
            "- Read config files, READMEs, package.json before starting work on a project\n"
            "- Read test files to understand expected behavior before fixing bugs\n"
            "- After editing, read the modified section to verify your changes are correct\n"
            "\n"
            "## When NOT to use\n"
            "- Don't read entire large files (1000+ lines) - use offset/limit for specific sections\n"
            "- Don't read when Grep would be faster - search first, then read matching regions\n"
            "- Don't read binary files - use Bash('file <path>') instead\n"
            "- For large codebases, delegate bulk reading to a sub-agent to protect your context\n"
            "\n"
            "## Smart reading strategy\n"
            "1. Grep(pattern='function_name') to find the file and line number\n"
            "2. Read(file_path, offset=line-5, limit=30) to read just that section\n"
            "3. Never read 10 files sequentially - call multiple Read in parallel\n"
            "\n"
            "## Parameters\n"
            "- file_path: absolute or relative (resolved from workspace root)\n"
            "- offset: start line (1-based). Use to skip to a specific section\n"
            "- limit: max lines to read. Default 2000. Use smaller values for targeted reads\n"
            "- Auto-detects images, PDFs, notebooks - returns appropriate metadata"
        )
    )
    async def read(self, params: ReadParams) -> ActionResult:
        """Read a file with line numbers. Auto-detects images, PDFs, notebooks."""
        try:
            file_path = self._resolve_path(params.file_path)

            # Check existence
            if not os.path.exists(file_path):
                suggestion = (
                    f"File not found. Use Glob to find similar files:\n"
                    f"  glob(pattern='**/*{Path(file_path).name}')"
                )
                return ActionResult(
                    success=False,
                    error=f"File does not exist: {file_path}",
                    metadata={"suggestion": suggestion},
                )

            # Check if path is a directory (Windows returns PermissionError on open())
            if os.path.isdir(file_path):
                return ActionResult(
                    success=False,
                    error=f"Path is a directory, not a file: {file_path}",
                    metadata={"suggestion": "Use Glob to list files in this directory:\n  Glob(pattern='" + file_path.replace("\\", "/") + "/**/*')"},
                )

            # Check permissions
            if not os.access(file_path, os.R_OK):
                suggestion = f"Permission denied. Try: `file {file_path}` via Bash to check permissions."
                return ActionResult(
                    success=False,
                    error=f"Permission denied: {file_path}",
                    metadata={"suggestion": suggestion},
                )

            # File metadata
            file_size = os.path.getsize(file_path)
            file_meta = gather_file_metadata(file_path)

            # Handle images
            if is_image_file(file_path):
                return self._handle_image_read(file_path, file_meta)

            # Handle PDFs
            if is_pdf_file(file_path):
                return self._handle_pdf_read(file_path, params, file_meta)

            # Handle notebooks
            if is_notebook_file(file_path):
                return self._handle_notebook_read(file_path, file_meta)

            # Binary file check
            with open(file_path, "rb") as f:
                sample = f.read(512)
            if is_binary_file(file_path, sample):
                suggestion = (
                    f"This is a binary file. Try Bash commands instead:\n"
                    f"  bash(command='file {file_path}')\n"
                    f"  bash(command='xxd {file_path} | head')"
                )
                return ActionResult(
                    success=False,
                    error=f"Cannot read binary file: {file_path}",
                    metadata={"suggestion": suggestion},
                )

            # Regular text file
            return await self._read_text_file(file_path, params, file_meta)

        except Exception as e:
            logger.error(f"Read failed: {e}", exc_info=True)
            return ActionResult(
                success=False,
                error=f"Read failed: {e}",
            )

    async def _read_text_file(
        self, file_path: str, params: ReadParams, file_meta: dict
    ) -> ActionResult:
        """Read a regular text file."""
        try:
            with open(file_path, "r", encoding=params.encoding, errors="replace") as f:
                lines = f.readlines()

            total_lines = len(lines)

            # Apply offset and limit
            # offset is 0-based line index, limit is max lines to read
            start = params.offset if params.offset is not None else 0
            limit = params.limit if params.limit is not None else total_lines
            end = min(start + limit, total_lines)
            selected_lines = lines[start:end]

            # Format with line numbers
            content = ""
            for i, line in enumerate(selected_lines, start=start + 1):
                content += f"{i}\t{line}" if line.endswith("\n") else f"{i}\t{line}\n"

            # Detect language from extension
            _ext = Path(file_path).suffix.lower()
            _lang_map = {
                ".py": "python", ".js": "javascript", ".ts": "typescript",
                ".tsx": "tsx", ".jsx": "jsx", ".java": "java", ".go": "go",
                ".rs": "rust", ".c": "c", ".cpp": "cpp", ".rb": "ruby",
                ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".toml": "toml",
                ".md": "markdown", ".html": "html", ".css": "css", ".sql": "sql",
                ".sh": "shell", ".bash": "shell", ".dart": "dart",
            }

            return ActionResult(
                success=True,
                data={
                    "path": self._to_relative(file_path),
                    "content": content.rstrip(),
                    "language": _lang_map.get(_ext, "text"),
                    "total_lines": total_lines,
                    "lines_read": len(selected_lines),
                    "start_line": start + 1,
                    "end_line": end,
                },
                metadata=file_meta,
            )

        except UnicodeDecodeError as e:
            suggestion = (
                f"Encoding issue. Detect with: `file -i {file_path}` via Bash"
            )
            return ActionResult(
                success=False,
                error=f"Encoding error: {e}",
                metadata={"suggestion": suggestion},
            )
        except Exception as e:
            logger.error(f"Text file read failed: {e}", exc_info=True)
            return ActionResult(
                success=False,
                error=f"Read failed: {e}",
            )

    def _handle_image_read(self, file_path: str, file_meta: dict) -> ActionResult:
        """Handle image file reads."""
        # For now, return metadata indicating it's an image
        return ActionResult(
            success=True,
            error=f"[Image: {Path(file_path).name}]",
            metadata={
                **file_meta,
                "file_type": "image",
                "detected": "image auto-detection enabled",
            },
        )

    def _handle_pdf_read(
        self, file_path: str, params: ReadParams, file_meta: dict
    ) -> ActionResult:
        """Handle PDF file reads."""
        # Placeholder for PDF reading
        return ActionResult(
            success=True,
            output=f"[PDF: {Path(file_path).name}]",
            metadata={
                **file_meta,
                "file_type": "pdf",
                "detected": "pdf auto-detection enabled",
            },
        )

    def _handle_notebook_read(self, file_path: str, file_meta: dict) -> ActionResult:
        """Handle Jupyter notebook reads."""
        # Placeholder for notebook reading
        return ActionResult(
            success=True,
            output=f"[Notebook: {Path(file_path).name}]",
            metadata={
                **file_meta,
                "file_type": "notebook",
                "detected": "notebook auto-detection enabled",
            },
        )

    # ========================================================================
    # ACTION 2: Write
    # ========================================================================

    @action(
        description="Write a file to the local filesystem.",
        params_model=WriteParams,
        cli_label="Write",
        cli_param="file_path",
        tool_prompt=(
            "Create a new file or completely rewrite an existing one.\n"
            "\n"
            "## When to use\n"
            "- Creating brand new files that don't exist yet\n"
            "- Complete rewrites where >50% of the file changes\n"
            "- Generating config files, test files, or boilerplate from scratch\n"
            "\n"
            "## When NOT to use - use Edit instead\n"
            "- Modifying a few lines in an existing file - Edit sends only the diff\n"
            "- Fixing a bug in one function - Edit is surgical, Write replaces everything\n"
            "- NEVER Write an existing file you haven't Read first - you'll lose content\n"
            "\n"
            "## Rules\n"
            "- Parent directories are created automatically (atomic writes)\n"
            "- NEVER create documentation files (*.md, README) unless explicitly asked\n"
            "- NEVER create files that duplicate existing functionality - check with Glob first\n"
            "- After writing, the file is automatically linted - check the lint result for errors\n"
            "- Prefer editing existing files over creating new ones - less file bloat\n"
            "\n"
            "## Parameters\n"
            "- file_path: absolute or relative (resolved from workspace root)\n"
            "- content: the complete file content"
        )
    )
    async def write(self, params: WriteParams) -> ActionResult:
        """Write a file. Creates parent directories automatically."""
        try:
            file_path = self._resolve_path(params.file_path)

            # Create parent directories
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            # Atomic write: write to temp, then rename.
            # newline="" prevents Windows from translating \n to \r\n
            # so the file matches the LLM's content byte-for-byte.
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding=params.encoding,
                newline="",
                delete=False,
                dir=os.path.dirname(file_path),
            ) as tmp:
                tmp.write(params.content)
                tmp_path = tmp.name

            # Check if file existed before
            file_existed = os.path.exists(file_path)

            # Atomic rename
            if os.path.exists(file_path):
                os.replace(tmp_path, file_path)
            else:
                os.rename(tmp_path, file_path)

            # File metadata
            file_meta = gather_file_metadata(file_path)
            file_size = os.path.getsize(file_path)
            lines = params.content.count("\n") + 1

            _ext = Path(file_path).suffix.lower()
            _lang_map = {
                ".py": "python", ".js": "javascript", ".ts": "typescript",
                ".tsx": "tsx", ".jsx": "jsx", ".java": "java", ".go": "go",
                ".rs": "rust", ".yaml": "yaml", ".yml": "yaml",
                ".json": "json", ".toml": "toml", ".md": "markdown",
                ".html": "html", ".css": "css", ".sh": "shell",
            }

            # Post-write diagnostics - push to preview channel AND
            # include inline so the agent can react to syntax errors
            # without a second tool call.
            lint = await self._run_lint_and_publish(file_path, params.content)
            data: dict[str, Any] = {
                "path": self._to_relative(file_path),
                "language": _lang_map.get(_ext, "text"),
                "operation": "create" if not file_existed else "update",
                "size": file_size,
                "lines": lines,
            }
            if lint:
                data["lint"] = lint
                errors = [d for d in lint if d.get("severity") == "error"]
                warnings = [d for d in lint if d.get("severity") != "error"]
                data["errors"] = len(errors)
                data["warnings"] = len(warnings)

            return ActionResult(
                success=True,
                data=data,
                metadata=file_meta,
            )

        except PermissionError:
            suggestion = f"Permission denied. Try: `sudo nano {file_path}` or use Bash to adjust permissions."
            return ActionResult(
                success=False,
                error=f"Permission denied: {file_path}",
                metadata={"suggestion": suggestion},
            )
        except Exception as e:
            logger.error(f"Write failed: {e}", exc_info=True)
            return ActionResult(
                success=False,
                error=f"Write failed: {e}",
            )

    # ========================================================================
    # ACTION 3: Edit
    # ========================================================================

    @action(
        description="Find-and-replace in a file.",
        params_model=EditParams,
        cli_label="Edit",
        cli_param="file_path",
        tool_prompt=(
            "Surgical text replacement in a file. Only sends the diff, not the full file.\n"
            "\n"
            "## When to use\n"
            "- Fixing bugs - change the broken line(s), leave everything else untouched\n"
            "- Adding features - insert new code at a specific location\n"
            "- Refactoring - rename, restructure, or update specific sections\n"
            "- ANY modification to an existing file - always prefer Edit over Write\n"
            "\n"
            "## Required workflow\n"
            "1. Read the file first (or the relevant section) - Edit FAILS on unread files\n"
            "2. Copy old_string EXACTLY from the Read output - including indentation\n"
            "3. Make your change in new_string - preserve surrounding indentation\n"
            "4. After editing, Read the modified section to verify correctness\n"
            "\n"
            "## Handling failures\n"
            "- If old_string is not found, the error shows closest matches with line numbers\n"
            "- Use those line numbers to Read the exact region, then copy the correct text\n"
            "- Fuzzy matching auto-handles: trailing whitespace, CRLF, indentation differences\n"
            "- If old_string appears multiple times, add more context to make it unique\n"
            "\n"
            "## Parameters\n"
            "- file_path: the file to edit\n"
            "- old_string: exact text to find (copy from Read output)\n"
            "- new_string: replacement text\n"
            "- replace_all: true to replace ALL occurrences (default: false)\n"
            "- insert_at_line: insert new_string at this line number (no old_string needed)\n"
            "\n"
            "## Rules\n"
            "- NEVER rewrite entire files with Edit - use Write for complete rewrites\n"
            "- Keep edits small and focused - one logical change per Edit call\n"
            "- Preserve exact indentation - the file's style, not yours\n"
            "- After each edit, the file is automatically linted - check for errors"
        )
    )
    async def edit(self, params: EditParams) -> ActionResult:
        """Edit a file with fuzzy matching + insert_at_line support."""
        try:
            file_path = self._resolve_path(params.file_path)

            # Check existence
            if not os.path.exists(file_path):
                return ActionResult(
                    success=False,
                    error=f"File does not exist: {file_path}",
                )

            # Read file
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                original_content = f.read()

            original_lines = original_content.split("\n")
            result_content = original_content

            # Case 1: Insert at line
            if params.insert_at_line is not None:
                line_num = params.insert_at_line - 1  # 0-indexed
                if line_num < 0 or line_num > len(original_lines):
                    return ActionResult(
                        success=False,
                        error=f"Line {params.insert_at_line} out of range (file has {len(original_lines)} lines)",
                    )

                # Insert at line
                original_lines.insert(line_num, params.new_string)
                result_content = "\n".join(original_lines)

            # Case 2: Find and replace
            elif params.old_string:
                # Try exact match first
                pos = fuzzy_find_old_string(
                    params.old_string,
                    original_content,
                    threshold=params.fuzzy_threshold,
                )

                if pos is None:
                    # Fuzzy match failed - show closest matches
                    closest = find_closest_matches(
                        params.old_string,
                        original_content,
                        max_matches=params.max_suggestions,
                    )

                    suggestion = suggest_edit_recovery(
                        "old_string not found",
                        params.old_string,
                        closest,
                    )

                    # Put the closest matches in the error message so the
                    # LLM sees them without digging into metadata.
                    err = "old_string not found in file."
                    if closest:
                        samples = []
                        for m in closest[:3]:
                            preview = m.text[:80].replace("\n", "\\n")
                            samples.append(
                                f"  line {m.start_line}-{m.end_line} (~{m.similarity*100:.0f}% match): {preview!r}"
                            )
                        err += (
                            " Closest lines in the file:\n"
                            + "\n".join(samples)
                            + "\nRead the file again and copy the EXACT text as old_string."
                        )
                    else:
                        err += " No similar text found. The file may not contain what you think - Read it again."
                    return ActionResult(
                        success=False,
                        error=err,
                        metadata={
                            "suggestion": suggestion,
                            "closest_matches": [
                                {
                                    "line_range": f"{m.start_line}-{m.end_line}",
                                    "similarity": f"{m.similarity*100:.0f}%",
                                    "text": m.text[:100],
                                }
                                for m in closest
                            ],
                        },
                    )

                start, end = pos

                # Check uniqueness (unless replace_all)
                if not params.replace_all:
                    count = original_content.count(params.old_string)
                    if count > 1:
                        # Show the line numbers where each occurrence lives
                        # so the agent can pick a unique surrounding context.
                        occurrences = []
                        search_from = 0
                        while True:
                            idx = original_content.find(params.old_string, search_from)
                            if idx == -1:
                                break
                            line_no = original_content[:idx].count("\n") + 1
                            occurrences.append(line_no)
                            search_from = idx + 1
                        return ActionResult(
                            success=False,
                            error=(
                                f"old_string is not unique (appears {count} times "
                                f"at lines {occurrences}). "
                                f"Fix: add surrounding lines to old_string to make it unique, "
                                f"or pass replace_all=true to replace all occurrences."
                            ),
                            metadata={
                                "occurrences_count": count,
                                "line_numbers": occurrences,
                            },
                        )

                    # Single replacement - re-indent if fuzzy matched
                    matched_text = original_content[start:end]
                    new_adjusted = _reindent_replacement(
                        params.old_string, params.new_string, matched_text,
                    )
                    result_content = (
                        original_content[:start]
                        + new_adjusted
                        + original_content[end:]
                    )

                else:
                    # Replace all
                    result_content = original_content.replace(
                        params.old_string, params.new_string
                    )

            # Write result. newline="" disables the default Windows
            # translation of \n → \r\n, preserving exact byte content.
            # The LLM's \n stays \n; pre-existing \r\n pass through too.
            with open(file_path, "w", encoding="utf-8", newline="") as f:
                f.write(result_content)

            # Generate diff
            diff_preview = generate_diff_preview(original_content, result_content)

            # Metadata
            lines_changed = abs(
                len(result_content.split("\n")) - len(original_content.split("\n"))
            )
            bytes_changed = len(result_content) - len(original_content)

            # Compute a snippet around the change for the LLM to see what happened
            new_lines = result_content.split("\n")
            total_new = len(new_lines)

            # Find the first changed line
            old_lines = original_content.split("\n")
            first_changed = 0
            for ci, (ol, nl) in enumerate(zip(old_lines, new_lines)):
                if ol != nl:
                    first_changed = ci
                    break

            # Show 3 lines before + changed area + 3 lines after
            snippet_start = max(0, first_changed - 3)
            snippet_end = min(total_new, first_changed + abs(lines_changed) + 5)
            snippet = "\n".join(
                f"{snippet_start + j + 1}\t{new_lines[snippet_start + j]}"
                for j in range(snippet_end - snippet_start)
            )

            # Post-edit diagnostics (mirrors write() path).
            lint = await self._run_lint_and_publish(file_path, result_content)
            edit_data: dict[str, Any] = {
                "path": self._to_relative(file_path),
                "diff": diff_preview,
                "snippet": snippet,
                "lines_changed": lines_changed,
                "total_lines": total_new,
                "operation": "insert" if params.insert_at_line else "replace",
            }
            if lint:
                edit_data["lint"] = lint
                errors = [d for d in lint if d.get("severity") == "error"]
                warnings = [d for d in lint if d.get("severity") != "error"]
                edit_data["errors"] = len(errors)
                edit_data["warnings"] = len(warnings)

            return ActionResult(success=True, data=edit_data)

        except Exception as e:
            logger.error(f"Edit failed: {e}", exc_info=True)
            return ActionResult(
                success=False,
                error=f"Edit failed: {e}",
            )

    # ========================================================================
    # ACTION 4: Glob
    # ========================================================================

    @action(
        description="Find files by name pattern.",
        params_model=GlobParams,
        cli_label="Glob",
        cli_param="pattern",
        tool_prompt=(
            "Find files by name pattern. Use this to discover project structure.\n"
            "\n"
            "## When to use\n"
            "- Starting work on a new project - Glob('**/*.py') to see the structure\n"
            "- Finding files by name - Glob('**/auth*.py') to find auth-related files\n"
            "- Checking if a file exists before creating it - avoid duplicates\n"
            "- Discovering test files - Glob('**/test_*.py') or Glob('**/*.test.ts')\n"
            "\n"
            "## When NOT to use - use Grep instead\n"
            "- Searching for content INSIDE files (function names, imports, strings)\n"
            "- Finding which file contains a specific error message\n"
            "\n"
            "## Common patterns\n"
            "- '**/*.py' - all Python files recursively\n"
            "- 'src/**/*.tsx' - all TSX files under src/\n"
            "- '**/test_*' - all test files\n"
            "- '*.yaml' - YAML files in current directory only\n"
            "- '**/*config*' - any file with 'config' in the name\n"
            "\n"
            "## Parameters\n"
            "- pattern: glob pattern (* = one level, ** = any depth)\n"
            "- path: directory to search (default: workspace root)\n"
            "- Results sorted by modification time (newest first)"
        )
    )
    async def glob(self, params: GlobParams) -> ActionResult:
        """Find files by name pattern."""
        try:
            search_path = self._resolve_path(params.path) if params.path else (self.workspace or ".")

            if not os.path.isdir(search_path):
                return ActionResult(
                    success=False,
                    error=f"Not a directory: {search_path}",
                )

            # Offload the filesystem walk + stat calls to a thread -
            # ``pathlib.Path.glob`` is fully synchronous and on large
            # workspaces (or deep ``**/*`` patterns) routinely takes a
            # few seconds, blocking the event loop. Measured at N=50
            # concurrent turns: a stall of 2.4 s came from this call
            # (caught by the loop watchdog). Running it off-loop keeps
            # every other session responsive while the walk completes.
            import asyncio as _asyncio
            p = Path(search_path)
            ws = self.workspace or str(Path.cwd())
            pattern = params.pattern
            filter_type = params.type
            max_results = params.max_results

            def _scan() -> tuple[list[Path], int, bool]:
                _matches = list(p.glob(pattern))
                if filter_type == "file":
                    _matches = [m for m in _matches if m.is_file()]
                elif filter_type == "dir":
                    _matches = [m for m in _matches if m.is_dir()]
                _total = len(_matches)
                _trunc = _total > max_results
                if _trunc:
                    _matches = _matches[:max_results]
                _matches.sort(
                    key=lambda m: os.path.getmtime(m) if m.exists() else 0,
                    reverse=True,
                )
                return _matches, _total, _trunc

            matches, total_found, truncated = await _asyncio.to_thread(_scan)

            # Build structured file entries with full usable paths
            # (cheap enough to stay on the loop)
            file_entries = []
            for m in matches:
                try:
                    usable_path = str(m.relative_to(ws))
                except ValueError:
                    usable_path = str(m)
                entry: dict[str, Any] = {"path": usable_path.replace("\\", "/")}
                if m.is_file():
                    try:
                        entry["size"] = m.stat().st_size
                    except OSError:
                        entry["size"] = 0
                file_entries.append(entry)

            if not file_entries:
                suggestion = suggest_glob_recovery(params.pattern, 0)
                return ActionResult(
                    success=True,
                    data={
                        "files": [],
                        "count": 0,
                        "message": suggestion or "No matches found",
                    },
                )

            return ActionResult(
                success=True,
                data={
                    "files": file_entries,
                    "count": len(file_entries),
                    "total_found": total_found,
                    "truncated": truncated,
                    **({"hint": f"Only showing {params.max_results} of {total_found} matches. Use a more specific pattern or Grep for narrower results."} if truncated else {}),
                },
            )

        except Exception as e:
            logger.error(f"Glob failed: {e}", exc_info=True)
            return ActionResult(
                success=False,
                error=f"Glob failed: {e}",
            )

    # ========================================================================
    # ACTION 5: Grep
    # ========================================================================

    @action(
        description="Search file contents for a regex pattern.",
        params_model=GrepParams,
        cli_label="Grep",
        cli_param="pattern",
        tool_prompt=(
            "Search file contents by regex pattern. Your primary discovery tool.\n"
            "\n"
            "## When to use - ALWAYS search before reading\n"
            "- Finding where a function/class/variable is defined - Grep('def function_name')\n"
            "- Finding all usages of a symbol - Grep('import.*module_name')\n"
            "- Tracing errors - Grep('error message text')\n"
            "- Understanding how something is used across the codebase\n"
            "- ALWAYS Grep before Read - find the exact location, then read that section\n"
            "\n"
            "## When NOT to use\n"
            "- Finding files by NAME - use Glob instead\n"
            "- Reading a file you already know the path of - use Read directly\n"
            "\n"
            "## Smart search strategy\n"
            "1. Start broad: Grep('function_name') with output_mode='files_with_matches'\n"
            "2. Narrow down: Grep('function_name', path='src/', glob='*.py')\n"
            "3. Read context: Grep('function_name', context=5) to see surrounding lines\n"
            "4. Then Read the specific section with offset/limit\n"
            "\n"
            "## Parameters\n"
            "- pattern: regex (e.g. 'def verify', 'import.*auth', 'TODO|FIXME|HACK')\n"
            "- path: file or directory (default: workspace root)\n"
            "- glob: filter files (e.g. '*.py', '**/*.ts') - combine with path for precision\n"
            "- output_mode: 'content' (matching lines), 'files_with_matches' (paths only), 'count'\n"
            "- context: lines before/after each match (0-20)\n"
            "- multiline: true for patterns spanning multiple lines"
        )
    )
    async def grep(self, params: GrepParams) -> ActionResult:
        """Search file contents for a regex pattern. Uses rg if available, Python fallback otherwise."""
        try:
            search_path = self._resolve_path(params.path) if params.path else (self.workspace or ".")

            # Offload the search to a worker thread - both paths are
            # fully sync (``subprocess.run`` for rg, ``read_text`` +
            # rglob for the Python fallback) and routinely block the
            # event loop for 1–3 s on large workspaces. Measured at
            # 2.0 s stall on :8000 under load before this offload.
            import asyncio as _asyncio

            def _dispatch() -> ActionResult:
                try:
                    return self._grep_rg(params, search_path)
                except FileNotFoundError:
                    return self._grep_python(params, search_path)

            return await _asyncio.to_thread(_dispatch)

        except Exception as e:
            logger.error(f"Grep failed: {e}", exc_info=True)
            return ActionResult(
                success=False,
                error=f"Grep failed: {e}",
            )

    def _grep_rg(self, params: GrepParams, search_path: str) -> ActionResult:
        """Grep using ripgrep (rg) - fast, handles large codebases."""
        import shutil as _shutil
        rg_exe = _shutil.which("rg") or _shutil.which("rg.exe")
        if not rg_exe:
            raise FileNotFoundError("rg not in PATH")
        cmd = [rg_exe, params.pattern, search_path]
        if params.glob:
            cmd.extend(["--glob", params.glob])
        if params.output_mode == "files_with_matches":
            cmd.append("-l")
        elif params.output_mode == "count":
            cmd.append("-c")
        if params.context:
            cmd.append(f"-C{params.context}")
        if params.multiline:
            cmd.append("-U")
        cmd.extend(["--max-count", str(params.max_results)])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = result.stdout.strip()
        num_matches = len(output.split("\n")) if output else 0

        return ActionResult(
            success=True,
            data={
                "content": output or "No matches found",
                "count": num_matches,
                "output_mode": params.output_mode,
            },
        )

    def _grep_python(self, params: GrepParams, search_path: str) -> ActionResult:
        """Grep using pure Python - fallback when rg is not installed."""
        import fnmatch as _fnmatch
        import re as _re

        flags = _re.MULTILINE
        if params.multiline:
            flags |= _re.DOTALL
        try:
            pattern = _re.compile(params.pattern, flags)
        except _re.error as e:
            return ActionResult(success=False, error=f"Invalid regex: {e}")

        search = Path(search_path)
        results: list[str] = []
        files_matched: set[str] = set()
        count = 0
        max_results = params.max_results or 100

        # Collect files to search
        if search.is_file():
            files = [search]
        else:
            if params.glob:
                files = list(search.glob(params.glob))
            else:
                files = list(search.rglob("*"))
            files = [f for f in files if f.is_file() and f.suffix in (
                ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs",
                ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".kt",
                ".yaml", ".yml", ".json", ".toml", ".md", ".txt", ".html",
                ".css", ".sql", ".sh", ".bash", ".zsh", ".xml", ".csv",
                ".dart", ".lua", ".zig", ".dockerfile", ".env", ".cfg", ".ini",
            )]

        # Skip common dirs that bloat file counts without useful content
        _SKIP_DIRS = {
            ".git", ".venv", "venv", "node_modules", "__pycache__",
            ".pytest_cache", ".mypy_cache", "dist", "build", ".next",
            "target", ".tox", "htmlcov",
        }

        def _is_in_skip_dir(p: Path) -> bool:
            return any(part in _SKIP_DIRS for part in p.parts)

        files = [f for f in files if not _is_in_skip_dir(f)]

        for fpath in files:  # scan all - like rg does
            if count >= max_results:
                break
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            lines = text.split("\n")
            # Path relative to workspace (usable in Read/Edit)
            _ws = Path(self.workspace) if self.workspace else Path.cwd()
            try:
                rel = str(fpath.relative_to(_ws)).replace("\\", "/")
            except ValueError:
                rel = str(fpath.relative_to(search) if search.is_dir() else fpath.name).replace("\\", "/")

            for i, line in enumerate(lines, 1):
                if count >= max_results:
                    break
                if pattern.search(line):
                    count += 1
                    files_matched.add(rel)

                    if params.output_mode == "files_with_matches":
                        break  # One match per file is enough
                    elif params.output_mode == "count":
                        pass  # Just count
                    else:
                        # Content mode - show matching line with optional context
                        ctx = params.context or 0
                        start = max(0, i - 1 - ctx)
                        end = min(len(lines), i + ctx)
                        for j in range(start, end):
                            prefix = f"{rel}:{j+1}:"
                            marker = "-" if j != i - 1 else ""
                            results.append(f"{prefix}{marker}{lines[j]}")
                        if ctx > 0:
                            results.append("--")

        if params.output_mode == "files_with_matches":
            output = "\n".join(sorted(files_matched))
            return ActionResult(success=True, data={
                "content": output or "No matches found",
                "count": len(files_matched),
                "output_mode": "files_with_matches",
            })
        elif params.output_mode == "count":
            return ActionResult(success=True, data={
                "content": f"{count} matches",
                "count": count,
                "output_mode": "count",
            })
        else:
            output = "\n".join(results)
            return ActionResult(success=True, data={
                "content": output or "No matches found",
                "count": count,
                "output_mode": params.output_mode,
            })

    # ========================================================================
    # Module metadata
    # ========================================================================

    def get_manifest(self) -> ModuleManifest:
        """Return module manifest."""
        return ModuleManifest.from_module(self).model_copy(update={
            "description": "Filesystem operations - 5 ultra-powerful actions: read, write, edit, glob, grep.",
        })
