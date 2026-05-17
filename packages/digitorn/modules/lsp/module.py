"""LSP module v3 - Universal real-time feedback for any language.

Fully dynamic: every YAML entry = a feedback channel. Supports 3 modes:
  - ``lsp``: JSON-RPC persistent (pyright, gopls, texlab, rust-analyzer)
  - ``compiler``: Re-run after each edit (cargo check, tsc --noEmit)
  - ``linter``: Shell-out on-demand (ruff, eslint, stylelint)

Config examples::

    # Minimal - auto-detect from root markers
    lsp: {}

    # Simple - auto-detect protocol from command name
    lsp:
      config:
        python: "pyright-langserver --stdio"
        rust: "cargo check --message-format=json"

    # Full control
    lsp:
      config:
        servers:
          python:
            command: "pyright-langserver --stdio"
            protocol: lsp
            extensions: [".py", ".pyi"]
          latex:
            command: "texlab"
            protocol: lsp
            extensions: [".tex", ".bib"]
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from digitorn.modules.base import ActionResult, BaseModule, Platform
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ModuleManifest

from .params import CheckParams, DiagnosticsParams, LspRequestParams
from .params import LspCancelParams, NotifyChangeParams
from .protocols import FeedbackProtocol, create_protocol

logger = logging.getLogger(__name__)


# ── Config model (compile-time validation via CONFIG_MODEL) ──────


class LspConfig(BaseModel):
    """Pydantic config for the lsp module (validated at compile time).

    Top-level keys beyond ``workspace``/``servers`` are treated as shorthand
    language-server definitions by ``_parse_config`` (e.g. ``python:
    "ruff check ..."`` or ``markdown: {command: "markdownlint ..."}``),
    so ``extra="allow"`` is intentional.
    """

    model_config = {"extra": "allow"}

    workspace: str = Field(default="", description="Auto-injected by the daemon.")
    servers: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Named language server configs (command, protocol, extensions, ...).",
    )


# ── Auto-detection tables ────────────────────────────────────────

_NAME_TO_EXTENSIONS: dict[str, list[str]] = {
    "python": [".py", ".pyi"], "py": [".py", ".pyi"],
    "typescript": [".ts", ".tsx"], "ts": [".ts", ".tsx"],
    "javascript": [".js", ".jsx", ".mjs"], "js": [".js", ".jsx"],
    "go": [".go"], "golang": [".go"],
    "rust": [".rs"],
    "latex": [".tex", ".bib", ".cls", ".sty"], "tex": [".tex", ".bib"],
    "css": [".css", ".scss", ".less"],
    "html": [".html", ".htm"],
    "json": [".json"], "jsonc": [".json", ".jsonc"],
    "yaml": [".yaml", ".yml"], "yml": [".yaml", ".yml"],
    "toml": [".toml"],
    "markdown": [".md"], "md": [".md"],
    "c": [".c", ".h"],
    "cpp": [".cpp", ".hpp", ".cc", ".cxx"], "cxx": [".cpp", ".hpp"],
    "java": [".java"],
    "ruby": [".rb"], "rb": [".rb"],
    "php": [".php"],
    "swift": [".swift"],
    "kotlin": [".kt", ".kts"], "kt": [".kt"],
    "shell": [".sh", ".bash"], "bash": [".sh", ".bash"], "sh": [".sh"],
    "sql": [".sql"],
    "xml": [".xml", ".xsd", ".xsl"],
    "proto": [".proto"], "protobuf": [".proto"],
    "dockerfile": ["Dockerfile", ".dockerfile"],
    "graphql": [".graphql", ".gql"],
    "elixir": [".ex", ".exs"],
    "erlang": [".erl"],
    "haskell": [".hs"],
    "lua": [".lua"],
    "r": [".r", ".R"],
    "scala": [".scala"],
    "zig": [".zig"],
}

# Known LSP servers for auto-detection (when lsp: {} with no config)
_AUTO_DETECT_SERVERS: list[dict[str, Any]] = [
    {"name": "python", "command": "pyright-langserver --stdio", "protocol": "lsp", "markers": ["pyproject.toml", "setup.py", "requirements.txt", ".py"]},
    {"name": "typescript", "command": "typescript-language-server --stdio", "protocol": "lsp", "markers": ["tsconfig.json", "package.json"]},
    {"name": "go", "command": "gopls", "protocol": "lsp", "markers": ["go.mod"]},
    {"name": "rust", "command": "rust-analyzer", "protocol": "lsp", "markers": ["Cargo.toml"]},
    {"name": "latex", "command": "texlab", "protocol": "lsp", "markers": [".tex"]},
    {"name": "css", "command": "vscode-css-language-server --stdio", "protocol": "lsp", "markers": [".css", ".scss"]},
    {"name": "html", "command": "vscode-html-language-server --stdio", "protocol": "lsp", "markers": [".html"]},
    {"name": "json", "command": "vscode-json-language-server --stdio", "protocol": "lsp", "markers": [".json"]},
]

# Known linters for fallback (when no LSP server available)
_FALLBACK_LINTERS: list[dict[str, Any]] = [
    {"name": "python", "command": "ruff check", "protocol": "linter", "parser": "ruff", "extensions": [".py"]},
    {"name": "typescript", "command": "eslint", "protocol": "linter", "parser": "eslint", "extensions": [".ts", ".tsx", ".js", ".jsx"]},
    {"name": "tsc", "command": "tsc --noEmit", "protocol": "compiler", "parser": "tsc", "extensions": [".ts", ".tsx"]},
    {"name": "cargo", "command": "cargo check --message-format=json", "protocol": "compiler", "parser": "cargo", "extensions": [".rs"]},
    {"name": "govet", "command": "go vet -json", "protocol": "compiler", "parser": "govet", "extensions": [".go"]},
]

# Protocol auto-detection from command name
_LSP_KEYWORDS = {"langserver", "language-server", "lsp", "server", "analyzer", "texlab", "gopls", "pylsp", "vscode-css", "vscode-html", "vscode-json"}
_COMPILER_KEYWORDS = {"check", "vet", "build", "compile", "noemit", "watch"}
_LINTER_COMMANDS = {"ruff", "eslint", "stylelint", "jsonlint", "flake8", "pylint", "mypy", "black", "prettier", "biome"}


def _detect_protocol(command: str) -> str:
    """Guess protocol from command string."""
    cmd_lower = command.lower()
    parts = set(cmd_lower.replace("-", " ").replace("_", " ").split())
    cmd_name = command.split()[0].lower() if command else ""
    cmd_base = cmd_name.split("/")[-1]  # handle full paths

    # 1. Explicit linters first (before compiler keywords match "ruff check")
    if cmd_base in _LINTER_COMMANDS:
        return "linter"
    # 2. LSP servers
    if any(kw in parts or kw in cmd_name for kw in _LSP_KEYWORDS):
        return "lsp"
    # 3. Compilers
    if any(kw in parts or kw in cmd_lower for kw in _COMPILER_KEYWORDS):
        return "compiler"
    return "linter"


def _detect_parser(command: str) -> str:
    """Guess parser from command name."""
    cmd = command.split()[0].lower() if command else ""
    if "ruff" in cmd:
        return "ruff"
    if "eslint" in cmd:
        return "eslint"
    if "tsc" in cmd or "typescript" in cmd:
        return "tsc"
    if "cargo" in cmd:
        return "cargo"
    if "go" in cmd and "vet" in command.lower():
        return "govet"
    if "mypy" in cmd:
        return "ruff"  # Similar JSON format
    return "fallback"


_RGLOB_LIMIT = 500  # max entries to scan for extension markers


def _marker_present(ws: Path, marker: str) -> bool:
    """Check if a marker exists in workspace root.

    File/dir markers (e.g. "pyproject.toml") use direct existence check.
    Extension markers (e.g. ".css") scan for any matching file, with a
    limit to avoid crawling huge trees.
    """
    if marker.startswith(".") and "/" not in marker:
        # Extension marker - check if any file with that extension exists
        # Use next() on lazy rglob: stops at first match, avoids full scan
        return next(ws.rglob(f"*{marker}"), None) is not None
    return (ws / marker).exists()


# ── Module ───────────────────────────────────────────────────────


class LspModule(BaseModule):
    """Universal real-time feedback - any language, any tool.

    v3: Fully dynamic configuration. 3 protocol modes.
    Auto-detects project language and available tools.
    """

    MODULE_ID = "lsp"
    MODULE_TYPE = "action"
    VERSION = "3.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]
    CONFIG_MODEL = LspConfig

    def __init__(self) -> None:
        super().__init__()
        self._workspace: str = ""
        self._sidecar_pool: Any = None
        self._app_id: str = "default"
        # Per-app keyed state -- mandatory for the workered path where
        # one ``LspModule()`` instance is shared across every deployed
        # app routed to the ``tools`` worker. Without per-app keying,
        # ``app A`` configuring ``python: "ruff ..."`` would leak ruff
        # diagnostics into every other app's .py writes (verified by
        # ``state_isolation`` scenario). Each map is keyed by
        # ``app_id``; the legacy single-tenant fields below are
        # property views that aggregate everything for diagnostic /
        # introspection callers.
        self._app_protocols: dict[str, dict[str, FeedbackProtocol]] = {}
        self._app_protocol_instances: dict[str, list[FeedbackProtocol]] = {}
        self._app_pending_specs: dict[str, dict[str, dict[str, Any]]] = {}
        self._owns_pool: bool = False
        # Phase 3: in-flight request tracking for abort + supersession.
        # Keyed by (session_id, request_id) → asyncio.Task. Secondary
        # index by (session_id, path, method) for supersede_previous
        # semantics so completion/hover keystrokes auto-cancel stale
        # requests without the client doing bookkeeping.
        self._inflight: dict[tuple[str, str], asyncio.Task] = {}
        self._inflight_by_trio: dict[tuple[str, str, str], str] = {}
        # Methods where we default to "latest wins" - stale results are
        # discarded by the client anyway, so cancelling saves the LSP
        # server work. Rename/references/symbols are NOT in here:
        # those are user-initiated, should always deliver.
        self._supersede_methods: set[str] = {
            "textDocument/completion",
            "textDocument/hover",
            "textDocument/signatureHelp",
        }

    async def cleanup_session(self, session_id: str) -> int:
        """Cancel every in-flight LSP request for a session.

        Called by the app manager on ``end_session`` + abort paths so
        we don't leak asyncio tasks when a user closes a tab mid-hover.
        Returns the number of cancellations.

        Safe to call on sessions with nothing in-flight (no-op).
        Ignored for requests created with empty session_id (global /
        anonymous scope - rare, typically only CLI/standalone).
        """
        if not session_id:
            return 0
        cancelled = 0
        # Snapshot keys up front - we mutate _inflight in the loop.
        keys = [k for k in list(self._inflight) if k[0] == session_id]
        for k in keys:
            task = self._inflight.pop(k, None)
            if task is not None and not task.done():
                task.cancel()
                cancelled += 1
        # Clear trio index entries for this session too.
        for trio_key in [t for t in list(self._inflight_by_trio) if t[0] == session_id]:
            self._inflight_by_trio.pop(trio_key, None)
        if cancelled:
            logger.debug(
                "lsp_session_cleanup session=%s cancelled=%d",
                session_id, cancelled,
            )
        return cancelled

    # ── Per-app state helpers ───────────────────────────────────

    def _current_app_id(self) -> str:
        """Best-effort app_id for the *current* call. Consults the
        active ``ExecutionContext`` first (the worker route reconstructs
        it from the daemon-side ctx envelope on every dispatch), falls
        back to ``self._app_id`` which is set during the last
        ``on_config_update`` -- good enough for in-process / legacy
        callers where the module instance is per-app anyway.
        """
        try:
            ec = self._context_var.get()
        except LookupError:
            ec = None
        if ec is not None:
            aid = getattr(ec, "app_id", None)
            if aid:
                return aid
        return self._app_id or "default"

    def _protos_for(self, app_id: str) -> dict[str, FeedbackProtocol]:
        return self._app_protocols.setdefault(app_id, {})

    def _pending_for(self, app_id: str) -> dict[str, dict[str, Any]]:
        return self._app_pending_specs.setdefault(app_id, {})

    def _instances_for(self, app_id: str) -> list[FeedbackProtocol]:
        return self._app_protocol_instances.setdefault(app_id, [])

    async def _drain_app(self, app_id: str) -> None:
        """Stop and forget every protocol instance owned by ``app_id``.
        Called before re-registering on a hot redeploy so we don't end
        up with two pyright subprocesses serving the same extension.
        """
        for proto in self._instances_for(app_id):
            try:
                await proto.stop()
            except Exception:
                pass
        self._app_protocols.pop(app_id, None)
        self._app_protocol_instances.pop(app_id, None)
        self._app_pending_specs.pop(app_id, None)

    # ── Legacy single-tenant views (read-only) ──────────────────

    @property
    def _protocols(self) -> dict[str, FeedbackProtocol]:
        """Aggregated view across all apps -- last-write-wins per ext.
        Kept for diagnostic introspection / tests that don't carry an
        ExecutionContext. New code should go through ``_protos_for``
        with an explicit ``app_id``.
        """
        merged: dict[str, FeedbackProtocol] = {}
        for m in self._app_protocols.values():
            merged.update(m)
        return merged

    @property
    def _protocol_instances(self) -> list[FeedbackProtocol]:
        out: list[FeedbackProtocol] = []
        for lst in self._app_protocol_instances.values():
            out.extend(lst)
        return out

    @property
    def _pending_specs(self) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for m in self._app_pending_specs.values():
            merged.update(m)
        return merged

    # ── Lifecycle ────────────────────────────────────────────────

    async def on_config_update(
        self,
        config: dict[str, Any],
        *,
        app_id: str | None = None,
    ) -> None:
        await super().on_config_update(config)
        # _workspace is set by base class; keep empty-string compat for downstream
        if not self._workspace:
            self._workspace = ""

        # Resolve the owning app. Priority: explicit kwarg from the
        # worker's ``/admin/config/{module}`` route → ctx app_id →
        # legacy ``_app_id`` slot. ``default`` only for genuine
        # tenantless callers (CLI smoke tests).
        if app_id is None:
            app_id = self._current_app_id()
        self._app_id = app_id  # legacy slot consumers still read this

        if self._sidecar_pool is None:
            self._sidecar_pool = getattr(self, "_sidecar_pool", None)
        if self._sidecar_pool is None:
            ctx = getattr(self, "ctx", None)
            if ctx:
                self._sidecar_pool = getattr(ctx, "sidecar_pool", None)
        if self._sidecar_pool is None:
            from digitorn.core.sidecar_pool import DaemonSidecarPool
            self._sidecar_pool = DaemonSidecarPool()
            await self._sidecar_pool.start()
            self._owns_pool = True

        # Hot redeploy: drop the app's prior protocols before
        # registering the new config so a deploy with an emptied YAML
        # actually unregisters its servers.
        await self._drain_app(app_id)

        servers = self._parse_config(config)

        for spec in servers:
            await self._start_server(spec, app_id=app_id)

        if not servers:
            await self._auto_detect(app_id=app_id)

    def _parse_config(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse YAML config into normalized server specs."""
        servers: list[dict[str, Any]] = []

        explicit = config.get("servers", {})
        if isinstance(explicit, dict):
            for name, spec in explicit.items():
                if isinstance(spec, str) or isinstance(spec, dict):
                    servers.append(self._normalize_spec(name, spec))
                else:
                    # Skip invalid spec types (None, int, list, bool, etc.)
                    # This matches the behavior for top-level keys
                    continue

        for key, val in config.items():
            if key in ("workspace", "servers"):
                continue
            if isinstance(explicit, dict) and key in explicit:
                continue  # Already added from explicit servers dict
            if isinstance(val, str):
                servers.append(self._normalize_spec(key, val))
            elif isinstance(val, dict):
                servers.append(self._normalize_spec(key, val))

        return servers

    def _normalize_spec(self, name: str, spec: str | dict[str, Any]) -> dict[str, Any]:
        """Normalize a server spec to a standard dict."""
        if isinstance(spec, str):
            command = spec
            protocol = _detect_protocol(command)
            parser = _detect_parser(command)
            extensions = _NAME_TO_EXTENSIONS.get(name.lower(), [])
            return {
                "name": name,
                "command": command,
                "protocol": protocol,
                "extensions": extensions,
                "parser": parser,
            }

        command = spec.get("command", "")
        protocol = spec.get("protocol", _detect_protocol(command))
        extensions = spec.get("extensions", _NAME_TO_EXTENSIONS.get(name.lower(), []))
        parser = spec.get("parser", _detect_parser(command))
        return {
            "name": name,
            "command": command,
            "protocol": protocol,
            "extensions": extensions,
            "parser": parser,
        }

    async def _start_server(
        self, spec: dict[str, Any], *, app_id: str | None = None,
    ) -> bool:
        """Start a feedback server from a normalized spec under
        ``app_id`` (defaults to the current execution context's app
        when omitted)."""
        if app_id is None:
            app_id = self._current_app_id()
        name = spec["name"]
        command = spec.get("command", "")
        if not command:
            logger.debug("lsp_empty_command name=%s", name)
            return False
        # Use shlex.split so paths with spaces survive when quoted —
        # ``command.split()`` would mangle ``"C:/Program Files/foo.exe"
        # --stdio`` into three pieces. ``posix=False`` on Windows keeps
        # backslash literal and treats double-quoted segments as one
        # token; we still fall back to plain split() if shlex bails so
        # legacy callers passing un-shell-safe commands don't regress.
        try:
            import shlex
            cmd_parts = shlex.split(command, posix=(sys.platform != "win32"))
        except ValueError:
            cmd_parts = command.split()
        if not cmd_parts:
            logger.debug("lsp_empty_command_after_split name=%s", name)
            return False

        # Check binary exists
        if not shutil.which(cmd_parts[0]):
            logger.debug("lsp_binary_not_found name=%s cmd=%s", name, cmd_parts[0])
            return False

        protocol = create_protocol(spec["protocol"], spec.get("parser", "fallback"))
        protocol.name = name
        protocol.extensions = spec.get("extensions", [])

        success = await protocol.start(
            self._sidecar_pool, name, app_id, cmd_parts, self._workspace or None,
        )

        if success:
            self._instances_for(app_id).append(protocol)
            for ext in protocol.extensions:
                self._protos_for(app_id)[ext] = protocol
            logger.info(
                "lsp_server_active app=%s name=%s mode=%s extensions=%s",
                app_id, name, protocol.mode, protocol.extensions,
            )

        return success

    async def _auto_detect(self, *, app_id: str | None = None) -> None:
        """Auto-detect project languages and register servers as pending (lazy startup)."""
        if app_id is None:
            app_id = self._current_app_id()
        ws = Path(self._workspace) if self._workspace else Path.cwd()
        protos = self._protos_for(app_id)
        pending = self._pending_for(app_id)

        for server in _AUTO_DETECT_SERVERS:
            has_marker = any(
                _marker_present(ws, m) for m in server["markers"]
            )
            if not has_marker:
                continue

            name = server["name"]
            extensions = _NAME_TO_EXTENSIONS.get(name, [])
            if any(ext in protos for ext in extensions):
                continue  # Already covered by explicit config

            spec = {
                "name": name,
                "command": server["command"],
                "protocol": server["protocol"],
                "extensions": extensions,
                "parser": "fallback",
            }

            # Register as pending - will start on first use
            for ext in extensions:
                if ext not in pending:
                    pending[ext] = spec

            logger.debug(
                "lsp_pending app=%s name=%s extensions=%s",
                app_id, name, extensions,
            )

    async def on_stop(self) -> None:
        for app_id in list(self._app_protocol_instances.keys()):
            for proto in self._app_protocol_instances.get(app_id, []):
                try:
                    await proto.stop()
                except Exception:
                    pass
        self._app_protocols.clear()
        self._app_protocol_instances.clear()
        self._app_pending_specs.clear()

        if self._owns_pool and self._sidecar_pool:
            await self._sidecar_pool.stop()
            self._sidecar_pool = None

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": (
                "Universal real-time feedback for any language. Supports LSP servers "
                "(pyright, gopls, texlab, rust-analyzer), compilers (cargo check, tsc), "
                "and linters (ruff, eslint, stylelint). Fully configurable via YAML - "
                "each entry creates a persistent feedback channel."
            ),
            "author": "Digitorn Core",
            "tags": ["diagnostics", "linting", "lsp", "code-quality", "real-time"],
        })

    async def _get_protocol(self, path: str) -> FeedbackProtocol | None:
        """Resolve the feedback protocol for ``path`` *within the
        currently-active app's scope*.

        Cross-app isolation: an app that didn't configure a server
        for this extension gets ``None`` -- it doesn't inherit another
        app's protocol. This is what makes the workered LSP module
        safe to share across tenants.
        """
        ext = Path(path).suffix.lower()
        app_id = self._current_app_id()
        protos = self._protos_for(app_id)
        pending = self._pending_for(app_id)

        proto = protos.get(ext)
        if proto:
            return proto

        # Lazy startup: if we have a pending spec for this extension, start it now
        spec = pending.pop(ext, None)
        if spec is None:
            return None

        # Remove all extensions for this spec from pending (they share one server)
        for e in list(spec.get("extensions", [])):
            pending.pop(e, None)

        if await self._start_server(spec, app_id=app_id):
            return protos.get(ext)

        # LSP binary not available - try fallback linters
        name = spec["name"]
        for linter in _FALLBACK_LINTERS:
            if linter["name"] == name or set(linter["extensions"]) & set(spec["extensions"]):
                fallback_spec = {
                    "name": f"{name}-fallback",
                    "command": linter["command"],
                    "protocol": linter["protocol"],
                    "extensions": linter["extensions"],
                    "parser": linter.get("parser", "fallback"),
                }
                if await self._start_server(fallback_spec, app_id=app_id):
                    return protos.get(ext)
                break

        return None

    # ── Actions ──────────────────────────────────────────────────

    @action(
        description=(
            "Get diagnostics (errors, warnings) for a file or project. "
            "Uses real-time LSP if available, falls back to compiler or linter. "
            "Called by hooks / middleware / other modules - NOT exposed "
            "to the LLM (diagnostics flow to the agent via the inline "
            "``lint`` field of write/edit responses and via the "
            "``diagnostics`` preview channel for the client UI)."
        ),
        params_model=DiagnosticsParams,
        risk_level="low",
        tags=["diagnostics", "lint", "lsp", "code-quality"],
        aliases=["lint", "check_code", "verifier", "diagnostiquer"],
        cli_label="Diagnostics",
        cli_param="path",
        internal=True,
    )
    async def diagnostics(self, params: DiagnosticsParams) -> ActionResult:
        if params.path:
            proto = await self._get_protocol(params.path)
            if proto and proto.is_connected:
                diags = proto.get_diagnostics(params.path)
                errors = [d for d in diags if d.severity == "error"]
                return ActionResult(success=True, data={
                    "mode": proto.mode,
                    "server": proto.name,
                    "target": params.path,
                    "diagnostics": [d.to_dict() for d in diags[:50]],
                    "total": len(diags),
                    "errors": len(errors),
                    "warnings": len(diags) - len(errors),
                })

        # No protocol for this file - list active + pending servers
        if not params.path:
            active = [
                {"name": p.name, "mode": p.mode, "extensions": p.extensions, "connected": p.is_connected}
                for p in self._protocol_instances
            ]
            # Deduplicate pending specs by name
            seen: set[str] = set()
            pending = []
            for spec in self._pending_specs.values():
                if spec["name"] not in seen:
                    seen.add(spec["name"])
                    pending.append({"name": spec["name"], "extensions": spec["extensions"], "status": "pending"})
            if active or pending:
                return ActionResult(success=True, data={
                    "active_servers": active,
                    "pending_servers": pending,
                    "total_servers": len(active) + len(pending),
                })

        ext = Path(params.path).suffix if params.path else ""
        return ActionResult(
            success=False,
            error=f"No feedback server configured for '{ext or 'project'}'. "
                  f"Add one in YAML: lsp.config.{ext.lstrip('.') or 'language'}: \"command --stdio\"",
        )

    @action(
        description=(
            "Quick pass/fail check for a single file. "
            "Internal - called by hooks/middleware, not by the LLM agent."
        ),
        params_model=CheckParams,
        risk_level="low",
        tags=["diagnostics", "lint"],
        aliases=["verifier_fichier", "lint_file"],
        cli_label="Check",
        cli_param="path",
        internal=True,
    )
    async def check(self, params: CheckParams) -> ActionResult:
        proto = await self._get_protocol(params.path)
        if proto and proto.is_connected:
            diags = proto.get_diagnostics(params.path)
            errors = [d for d in diags if d.severity == "error"]
            return ActionResult(success=True, data={
                "path": params.path, "mode": proto.mode, "server": proto.name,
                "passed": len(errors) == 0,
                "errors": len(errors), "warnings": len(diags) - len(errors),
                "diagnostics": [d.to_dict() for d in diags[:20]],
            })
        return ActionResult(
            success=False,
            error=f"No feedback server for '{Path(params.path).suffix}'.",
        )

    @action(
        description=(
            "Notify that a file was changed - triggers fresh diagnostics. "
            "Internal - called automatically by the workspace/filesystem "
            "modules and the ``lsp_diagnose`` hook after write/edit. "
            "Agents never need to call this themselves."
        ),
        params_model=NotifyChangeParams,
        risk_level="low",
        tags=["diagnostics", "lsp"],
        internal=True,
    )
    async def notify_change(self, params: NotifyChangeParams) -> ActionResult:
        path = params.path if hasattr(params, "path") else params.get("path", "")
        if not path:
            return ActionResult(success=False, error="Missing 'path' parameter")

        proto = await self._get_protocol(path)
        if not proto or not proto.is_connected:
            return ActionResult(success=True, data={"mode": "none", "path": path})

        content = params.content if hasattr(params, "content") else params.get("content")
        # Cold-start: pyright/eslint/tsc take ~1-3 s to publish their
        # first diagnostics for a newly-opened URI. Once warm, didChange
        # responds in ~200 ms. Read _opened BEFORE notify_file_changed
        # (it adds the URI). LSP-mode only: compiler/linter protocols
        # are synchronous one-shots and don't need the wait.
        is_cold = (
            proto.mode == "lsp"
            and f"file://{Path(path).resolve()}" not in getattr(proto, "_opened", set())
        )
        await proto.notify_file_changed(path, content)

        await asyncio.sleep(3.0 if is_cold else (0.3 if proto.mode == "lsp" else 0.0))

        diags = proto.get_diagnostics(path)
        errors = [d for d in diags if d.severity == "error"]
        warnings = [d for d in diags if d.severity == "warning"]
        return ActionResult(success=True, data={
            "mode": proto.mode,
            "server": proto.name,
            "path": path,
            "diagnostics": [d.to_dict() for d in diags[:100]],
            "total": len(diags),
            "errors": len(errors),
            "warnings": len(warnings),
        })

    @action(
        description=(
            "Forward a raw LSP request (hover / goto / references / "
            "completion / rename / …) to the language server backing a "
            "given file. Internal - exposed to the REST /lsp/request "
            "endpoint, not to the LLM agent."
        ),
        params_model=LspRequestParams,
        risk_level="low",
        tags=["lsp", "rpc"],
        internal=True,
    )
    async def request(self, params: LspRequestParams) -> ActionResult:
        """Generic LSP method dispatch. See ``LspRequestParams``.

        Phase 3 additions:

        - Tracks each in-flight request by ``(session_id, request_id)``
          so the client can cancel it via ``cancel_request``.
        - Supersedes stale keystroke-driven requests automatically when
          a new one of the same kind arrives for the same file.
        """
        path = params.path
        method = params.method
        req_params = dict(params.params or {})
        session_id = params.session_id or ""

        proto = await self._get_protocol(path)
        if not proto:
            return ActionResult(
                success=False,
                error=f"No LSP server registered for extension of '{path}'",
            )
        if not proto.is_connected:
            return ActionResult(
                success=False,
                error=(
                    f"LSP server '{proto.name}' not connected - typically "
                    "means the binary is not installed on PATH"
                ),
            )
        if proto.mode != "lsp":
            return ActionResult(
                success=False,
                error=(
                    f"Protocol '{proto.name}' runs in '{proto.mode}' mode - "
                    "only real LSP servers support RPC methods. Use "
                    "lsp.check/diagnose for linters / compilers."
                ),
            )

        # Auto-fill textDocument.uri from `path` when absent - standard
        # LSP clients send this but we support shorthand requests.
        # ``Path.as_uri()`` is RFC 8089-compliant (``file:///C:/...`` on
        # Windows, three slashes + forward slashes); the legacy
        # ``f"file://{...resolve()}"`` shape would mismatch the URI that
        # ``notify_file_changed`` registered via didOpen and pyright /
        # tsserver would silently fail to find the document.
        from pathlib import Path as _Path
        td = req_params.get("textDocument")
        if not isinstance(td, dict):
            td = {}
            req_params["textDocument"] = td
        if not td.get("uri"):
            td["uri"] = _Path(path).resolve().as_uri()

        # Some servers require the doc to be explicitly opened before
        # accepting hover / goto / completion. Open-if-needed -- and
        # let the server actually parse the file before we ask it to
        # answer questions about it. Pyright / typescript-language-
        # server cold-start their analysis on the first didOpen for a
        # URI (~1-3 s); a hover fired immediately after returns ``null``
        # because no symbol table exists yet. We mirror the same warm-
        # up window used by ``notify_change`` (3 s on cold, 0.3 s on
        # warm) so the first request after a deploy gets a real reply.
        is_cold = _Path(path).resolve().as_uri() not in getattr(
            proto, "_opened", set(),
        )
        try:
            await proto.notify_file_changed(path)  # no-op if already opened
            await asyncio.sleep(3.0 if is_cold else 0.3)
        except Exception:
            pass

        # Mint a request_id if the client didn't supply one.
        import uuid as _uuid
        request_id = params.request_id or _uuid.uuid4().hex[:12]

        # Supersession: if an older in-flight request exists for the
        # same (session, path, method) triple AND the method is in the
        # "latest wins" set, cancel it before taking over. This is the
        # server-side equivalent of keystroke debouncing.
        if params.supersede_previous and method in self._supersede_methods:
            trio_key = (session_id, path, method)
            prev_rid = self._inflight_by_trio.get(trio_key)
            if prev_rid:
                prev_task = self._inflight.get((session_id, prev_rid))
                if prev_task is not None and not prev_task.done():
                    logger.debug(
                        "lsp_request_superseded session=%s method=%s old=%s new=%s",
                        session_id, method, prev_rid, request_id,
                    )
                    prev_task.cancel()
            self._inflight_by_trio[trio_key] = request_id

        key = (session_id, request_id)

        async def _do() -> dict[str, Any] | None:
            return await proto.request(
                method, req_params, timeout=params.timeout_seconds,
            )

        task = asyncio.create_task(_do())
        self._inflight[key] = task

        try:
            result = await task
        except asyncio.CancelledError:
            return ActionResult(
                success=False,
                error="request cancelled",
                data={"cancelled": True, "request_id": request_id,
                      "server": proto.name, "method": method},
            )
        finally:
            self._inflight.pop(key, None)
            # Clean the trio index if we're still the latest holder.
            trio_key = (session_id, path, method)
            if self._inflight_by_trio.get(trio_key) == request_id:
                self._inflight_by_trio.pop(trio_key, None)

        if result is None:
            return ActionResult(
                success=False,
                error=f"LSP request '{method}' returned no result (timeout or unsupported)",
                data={"server": proto.name, "method": method,
                      "request_id": request_id},
            )
        return ActionResult(success=True, data={
            "server": proto.name,
            "method": method,
            "request_id": request_id,
            "result": result,
        })

    @action(
        description=(
            "Cancel an in-flight LSP request by request_id. "
            "Internal - called by the REST /lsp/cancel endpoint."
        ),
        params_model=LspCancelParams,
        risk_level="low",
        tags=["lsp", "rpc"],
        internal=True,
    )
    async def cancel_request(self, params: LspCancelParams) -> ActionResult:
        """Cancel an in-flight LSP request by its correlation id."""
        session_id = getattr(params, "session_id", None) or ""
        request_id = params.request_id
        task = self._inflight.get((session_id, request_id))
        if task is None:
            # Try global lookup (ignore session_id)
            for (sid, rid), t in list(self._inflight.items()):
                if rid == request_id:
                    task = t
                    session_id = sid
                    break
        if task is None:
            return ActionResult(
                success=False, error="request not found (already done?)",
                data={"request_id": request_id, "cancelled": False},
            )
        if task.done():
            return ActionResult(
                success=True, data={"request_id": request_id,
                                     "cancelled": False, "already_done": True},
            )
        task.cancel()
        return ActionResult(
            success=True,
            data={"request_id": request_id, "cancelled": True,
                  "session_id": session_id},
        )
