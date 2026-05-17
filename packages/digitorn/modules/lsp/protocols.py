"""Feedback protocols - 3 modes of real-time communication.

Each protocol knows how to talk to its type of external tool:
  - LspProtocol: JSON-RPC persistent (pyright, gopls, texlab)
  - CompilerProtocol: Persistent process, re-triggered per edit (cargo, tsc --watch)
  - LinterProtocol: Shell-out on-demand, no persistent process (ruff, eslint)

All protocols expose the same interface via FeedbackProtocol,
so the module doesn't care which mode is active.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .parsers import Diagnostic, get_parser, parse_fallback, parse_lsp_diagnostics

logger = logging.getLogger(__name__)


class FeedbackProtocol(ABC):
    """Base class for all feedback protocols."""

    name: str = ""
    extensions: list[str] = []
    mode: str = ""  # "lsp", "compiler", "linter"

    @abstractmethod
    async def start(
        self,
        pool: Any,
        name: str,
        app_id: str,
        command: list[str],
        cwd: str | None,
    ) -> bool:
        """Start the protocol. Returns True if successful."""
        ...

    @abstractmethod
    async def notify_file_changed(self, path: str, content: str | None = None) -> None:
        """A file was modified - update diagnostics."""
        ...

    @abstractmethod
    def get_diagnostics(self, path: str) -> list[Diagnostic]:
        """Get cached diagnostics for a file. Instant."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Shutdown the protocol and release resources."""
        ...

    async def request(
        self, method: str, params: dict[str, Any], *, timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        """Generic LSP-method dispatch for hover / goto / references /
        completion / rename / etc.

        Default implementation returns ``None`` (the feature is only
        meaningful for real LSP protocols - compilers and linters
        don't speak JSON-RPC). Subclasses that have a live LSP channel
        override to forward the call.
        """
        return None

    @property
    def is_connected(self) -> bool:
        return False


# ── LSP Protocol ─────────────────────────────────────────────────


class LspProtocol(FeedbackProtocol):
    """JSON-RPC LSP - persistent sidecar with push diagnostics.

    Communication flow:
      1. start() → spawn process, initialize handshake
      2. notify_file_changed() → didOpen/didChange
      3. Server pushes publishDiagnostics → cached
      4. get_diagnostics() → read cache (instant)
    """

    mode = "lsp"

    def __init__(self, parser_name: str = ""):
        self._channel: Any = None
        self._pool: Any = None
        self._name: str = ""
        self._app_id: str = ""
        self._cwd: str | None = None
        self._cached: dict[str, list[dict[str, Any]]] = {}  # uri → raw LSP diagnostics
        self._opened: set[str] = set()  # URIs already opened
        self._parser_name = parser_name

    async def start(self, pool, name, app_id, command, cwd) -> bool:
        self._pool = pool
        self._name = name
        self._app_id = app_id
        self._cwd = cwd

        try:
            self._channel = await pool.acquire(
                name=f"lsp-{name}",
                app_id=app_id,
                command=command,
                protocol="jsonrpc",
                cwd=cwd,
                on_notification=self._on_notification,
            )

            ws_uri = Path(cwd).resolve().as_uri() if cwd else "file:///tmp"
            await self._channel.request("initialize", {
                "processId": None,
                "capabilities": {
                    "textDocument": {
                        "publishDiagnostics": {"relatedInformation": True},
                        "synchronization": {"didSave": True, "didOpen": True, "didChange": True},
                    },
                },
                "rootUri": ws_uri,
                "workspaceFolders": [{"uri": ws_uri, "name": Path(cwd).name if cwd else "workspace"}],
            }, timeout=30)

            await self._channel.notify("initialized", {})
            logger.info("lsp_protocol_started name=%s", name)
            return True

        except Exception as exc:
            logger.warning("lsp_protocol_start_failed name=%s: %s", name, exc)
            return False

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "textDocument/publishDiagnostics":
            uri = params.get("uri", "")
            self._cached[uri] = params.get("diagnostics", [])

    async def notify_file_changed(self, path: str, content: str | None = None) -> None:
        if not self._channel or self._channel.status != "connected":
            return

        # ``Path.as_uri()`` builds a *valid* file:// URI for the host
        # OS. The legacy ``f"file://{Path(path).resolve()}"`` shape
        # produced ``file://C:\Users\...`` on Windows (backslashes,
        # two slashes), which pyright / tsserver accept silently on
        # didOpen but then can't match against hover / goto requests
        # using normalised slashes. ``as_uri`` returns
        # ``file:///C:/Users/...`` (three slashes, forward slashes) --
        # the canonical RFC 8089 shape both servers expect.
        resolved_path = Path(path).resolve()
        uri = resolved_path.as_uri()

        if content is None:
            # Off-loop: ``Path.read_text`` is sync. Called for every
            # post-write LSP notification - on a 2 MB file under load
            # this would stall the loop and drop Socket.IO pings.
            try:
                import asyncio as _asyncio
                content = await _asyncio.to_thread(
                    Path(path).read_text, encoding="utf-8", errors="replace",
                )
            except OSError:
                return

        ext = Path(path).suffix.lower()
        lang_ids = {
            ".py": "python", ".pyi": "python", ".js": "javascript",
            ".jsx": "javascriptreact", ".ts": "typescript", ".tsx": "typescriptreact",
            ".go": "go", ".rs": "rust", ".tex": "latex", ".bib": "bibtex",
            ".json": "json", ".yaml": "yaml", ".yml": "yaml",
            ".css": "css", ".scss": "scss", ".html": "html",
            ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp",
            ".java": "java", ".rb": "ruby", ".php": "php",
            ".swift": "swift", ".kt": "kotlin", ".sql": "sql",
            ".xml": "xml", ".proto": "protobuf", ".toml": "toml",
            ".md": "markdown",
        }

        try:
            if uri in self._opened:
                await self._channel.notify("textDocument/didChange", {
                    "textDocument": {"uri": uri, "version": 2},
                    "contentChanges": [{"text": content}],
                })
            else:
                await self._channel.notify("textDocument/didOpen", {
                    "textDocument": {
                        "uri": uri,
                        "languageId": lang_ids.get(ext, "plaintext"),
                        "version": 1,
                        "text": content,
                    },
                })
                self._opened.add(uri)
        except Exception as exc:
            logger.debug("lsp_notify_error name=%s path=%s: %s", self._name, path, exc)

    def get_diagnostics(self, path: str) -> list[Diagnostic]:
        uri = Path(path).resolve().as_uri()
        raw = self._cached.get(uri, [])
        return parse_lsp_diagnostics(raw, path)

    async def request(
        self, method: str, params: dict[str, Any], *, timeout: float = 10.0,
    ) -> dict[str, Any] | None:
        """Forward a JSON-RPC LSP request to the backing language server.

        Used by the ``/lsp/request`` REST endpoint for hover, goto,
        references, completion, rename, etc. The caller passes raw LSP
        params - we don't reshape (the LSP spec itself is the contract).
        """
        if not self._channel or self._channel.status != "connected":
            return None
        try:
            return await self._channel.request(method, params, timeout=timeout)
        except Exception as exc:
            logger.debug(
                "lsp_request_failed name=%s method=%s: %s",
                self._name, method, exc,
            )
            return None

    async def stop(self) -> None:
        if self._channel:
            try:
                await self._channel.request("shutdown", {}, timeout=5)
                await self._channel.notify("exit", {})
            except Exception:
                pass
            if self._pool:
                try:
                    await self._pool.release(f"lsp-{self._name}", self._app_id)
                except Exception:
                    pass
        self._cached.clear()
        self._opened.clear()

    @property
    def is_connected(self) -> bool:
        return self._channel is not None and self._channel.status == "connected"


# ── Compiler Protocol ────────────────────────────────────────────


class CompilerProtocol(FeedbackProtocol):
    """Compiler - re-run after each edit, parse structured output.

    For tools like: cargo check, tsc --noEmit, gcc, javac
    These don't speak LSP but produce structured error output.

    Communication flow:
      1. start() → nothing (no persistent process needed)
      2. notify_file_changed() → run compiler, parse output, cache
      3. get_diagnostics() → read cache (instant after first run)
    """

    mode = "compiler"

    def __init__(self, parser_name: str = "fallback"):
        self._command: list[str] = []
        self._cwd: str | None = None
        self._name: str = ""
        self._parser_name = parser_name
        self._cached: dict[str, list[Diagnostic]] = {}
        self._lock = asyncio.Lock()
        self._last_run: float = 0

    async def start(self, pool, name, app_id, command, cwd) -> bool:
        self._command = command
        self._cwd = cwd
        self._name = name
        logger.info("compiler_protocol_started name=%s cmd=%s", name, " ".join(command))
        return True

    async def notify_file_changed(self, path: str, content: str | None = None) -> None:
        import time

        now = time.monotonic()
        if now - self._last_run < 1.0:
            return
        self._last_run = now

        cwd = self._cwd or str(Path(path).parent)

        # Compose the argv. Project-wide compilers (``cargo check``,
        # ``go vet``) walk the whole crate / module from their cwd and
        # would choke on a bare file path. Single-file compilers
        # (``tsc --noEmit``, ``javac``, ``gcc -fsyntax-only``) need the
        # file appended explicitly -- without it, tsc returns no
        # inputs and the audit reports zero diagnostics for an
        # obviously broken .ts. We detect the project-wide tools by
        # name and skip the append for them.
        _PROJECT_WIDE = {"cargo", "go"}
        first = (self._command[0] if self._command else "").lower()
        first_base = first.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        # Strip a ``.exe`` suffix on Windows so the check fires for
        # both ``cargo`` and ``cargo.exe``.
        if first_base.endswith(".exe"):
            first_base = first_base[:-4]
        argv = list(self._command)
        if first_base not in _PROJECT_WIDE:
            argv.append(path)

        # Windows: asyncio.create_subprocess_exec uses CreateProcessW
        # which does NOT honour PATHEXT for ``.cmd`` / ``.bat`` wrappers
        # (``tsc``, ``npm``, ``yarn`` ship as .cmd shims). shutil.which
        # does honour PATHEXT, so we pre-resolve the full path here and
        # let CreateProcessW load it directly. POSIX: shutil.which is a
        # no-op when the binary is already absolute or on PATH.
        _resolved_exe = shutil.which(argv[0])
        if _resolved_exe:
            argv[0] = _resolved_exe

        async with self._lock:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                stdout_str = stdout.decode("utf-8", errors="replace")
                stderr_str = stderr.decode("utf-8", errors="replace")

                parser = get_parser(self._parser_name)
                diags = parser(stdout_str, stderr_str)

                # Cache per-file
                self._cached.clear()
                for d in diags:
                    self._cached.setdefault(d.file, []).append(d)

                logger.debug("compiler_run name=%s diags=%d", self._name, len(diags))

            except asyncio.TimeoutError:
                logger.warning("compiler_timeout name=%s", self._name)
            except FileNotFoundError:
                logger.warning(
                    "compiler_not_found name=%s cmd=%s",
                    self._name, argv[0],
                )
            except Exception as exc:
                logger.warning("compiler_error name=%s: %s", self._name, exc)

    def get_diagnostics(self, path: str) -> list[Diagnostic]:
        # Try exact match first
        resolved = str(Path(path).resolve())
        for key, diags in self._cached.items():
            if key == path or key == resolved or resolved.endswith(key):
                return diags
        # Return all if no match (project-wide)
        all_diags: list[Diagnostic] = []
        for diags in self._cached.values():
            all_diags.extend(diags)
        return all_diags

    async def stop(self) -> None:
        self._cached.clear()

    @property
    def is_connected(self) -> bool:
        return bool(self._command)


# ── Linter Protocol ──────────────────────────────────────────────


class LinterProtocol(FeedbackProtocol):
    """Linter - shell-out on-demand, no persistent process.

    For tools like: ruff, eslint, stylelint, jsonlint
    Fast enough to run per-file without a sidecar.

    Communication flow:
      1. start() → just save config (no process)
      2. notify_file_changed() → run linter on file, cache result
      3. get_diagnostics() → read cache
    """

    mode = "linter"

    def __init__(self, parser_name: str = "fallback"):
        self._command: list[str] = []
        self._cwd: str | None = None
        self._name: str = ""
        self._parser_name = parser_name
        self._cached: dict[str, list[Diagnostic]] = {}
        self._json_flag: str = ""

    async def start(self, pool, name, app_id, command, cwd) -> bool:
        self._command = command
        self._cwd = cwd
        self._name = name

        # Auto-detect JSON flag for known linters
        cmd_name = command[0] if command else ""
        if "ruff" in cmd_name:
            self._json_flag = "--output-format=json"
            self._parser_name = "ruff"
        elif "eslint" in cmd_name:
            self._json_flag = "--format=json"
            self._parser_name = "eslint"
        elif "mypy" in cmd_name:
            self._json_flag = "--output=json"
            self._parser_name = "ruff"

        logger.info("linter_protocol_started name=%s cmd=%s", name, " ".join(command))
        return True

    async def notify_file_changed(self, path: str, content: str | None = None) -> None:
        parts = list(self._command)
        if self._json_flag and self._json_flag not in parts:
            parts.append(self._json_flag)
        parts.append(path)

        cwd = self._cwd or str(Path(path).parent)

        # Resolve the binary via shutil.which (honours PATHEXT for
        # ``.cmd`` / ``.bat`` shims that asyncio.create_subprocess_exec
        # would otherwise miss on Windows). Matches CompilerProtocol's
        # behaviour; no-op when ``parts[0]`` is already an absolute
        # path or on POSIX.
        _resolved = shutil.which(parts[0])
        if _resolved:
            parts[0] = _resolved

        try:
            proc = await asyncio.create_subprocess_exec(
                *parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")

            parser = get_parser(self._parser_name)
            diags = parser(stdout_str, stderr_str)
            self._cached[path] = diags

        except asyncio.TimeoutError:
            logger.warning("linter_timeout name=%s", self._name)
        except FileNotFoundError:
            logger.warning("linter_not_found name=%s cmd=%s", self._name, parts[0])
        except Exception as exc:
            logger.warning("linter_error name=%s: %s", self._name, exc)

    def get_diagnostics(self, path: str) -> list[Diagnostic]:
        return self._cached.get(path, [])

    async def stop(self) -> None:
        self._cached.clear()

    @property
    def is_connected(self) -> bool:
        return bool(self._command)


# ── Factory ──────────────────────────────────────────────────────


def create_protocol(mode: str, parser_name: str = "fallback") -> FeedbackProtocol:
    """Create a protocol instance by mode name."""
    if mode == "lsp":
        return LspProtocol(parser_name)
    elif mode == "compiler":
        return CompilerProtocol(parser_name)
    elif mode == "linter":
        return LinterProtocol(parser_name)
    else:
        raise ValueError(f"Unknown protocol mode: {mode}. Use 'lsp', 'compiler', or 'linter'.")
