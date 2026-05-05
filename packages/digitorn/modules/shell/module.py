"""Shell module - 1 ultra-powerful bash action with 4 execution modes.

bash - execute shell commands with 4 modes:
  1. Sync execution: normal commands (wait for result)
  2. Async execution: long-running commands (return task_id immediately)
  3. Status checking: check running task status and output
  4. Task management: kill running tasks

Security (all platforms):
  - Platform-specific command blacklist checked before every execution.
  - Workspace path confinement: absolute paths outside workspace are blocked.
  - Output sanitization: API keys, passwords, tokens are redacted.
  - Timeout enforced on every subprocess call.
  - Audit log: every command recorded with command, cwd, exit code, timestamp.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform as _platform
import re
import signal
import tempfile
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from digitorn.core.runtime.types import WORKSPACE_PLACEHOLDER
from pydantic import BaseModel, Field as PydanticField

from digitorn.modules.base import ActionResult, BaseModule, Platform, ResourceEstimate
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ConstraintSpec, ModuleManifest

from .params import BashParams
from .platform_adapters import SENSITIVE_ENV_KEYS, get_adapter, mask_env

logger = logging.getLogger(__name__)

_SYSTEM = _platform.system().lower()
_IS_WINDOWS = _SYSTEM == "windows"
_BG_MAX_LINES = 10_000


# ── Hang detection ──────────────────────────────────────────────
#
# CLIs that try to read user input from stdin will, with stdin=DEVNULL,
# usually see EOF and exit with an error. But some tools print a prompt
# THEN sit waiting on /dev/tty (sudo, ssh keyboard-interactive) or
# loop on read with retries (custom scripts), or expect a TTY for the
# OAuth callback wait (gh/vercel login).
#
# When that happens, the user-visible symptom is "command runs to the
# 5-minute hard timeout, then errors with 'timed out'" — which leaves
# the agent guessing what to do. The hang detector watches the output
# for prompt-like text and, when it sees one + silence, kills early
# and returns a STRUCTURED directive telling the agent the exact fix.
_PROMPT_PATTERN_HINTS: list[tuple[re.Pattern[str], str]] = [
    # npm/npx
    (re.compile(r"need to install the following packages", re.I),
     "npm/npx is asking permission to install. Set npm_config_yes=true "
     "(already in default env) or pass --yes to npm create."),
    (re.compile(r"ok to proceed\?", re.I),
     "Tool asked 'Ok to proceed?'. Add --yes (npm) or -y (apt) flag."),
    # apt / dpkg
    (re.compile(r"do you want to continue\?", re.I),
     "apt is asking 'Continue?'. Run with `apt-get install -y` or set "
     "DEBIAN_FRONTEND=noninteractive (already default)."),
    # confirmations
    (re.compile(r"\(y/n\)|\(yes/no\)|\[y/n\]|\[yes/no\]", re.I),
     "CLI is asking for y/n confirmation. Add the tool's non-interactive "
     "flag (--yes, --force, --non-interactive) or use keep_stdin_open=true "
     "+ stdin_text='y' to drive the prompt deliberately."),
    (re.compile(r"are you sure\?|confirm:", re.I),
     "CLI wants confirmation. Use the tool's non-interactive flag or "
     "stdin_text='yes\\n'."),
    # auth
    # ``password`` and ``passphrase`` may have arbitrary text between
    # the keyword and the trailing ``:`` (e.g. ``Enter passphrase for
    # key '/home/.../id_rsa':``). Match the keyword + any chars until
    # a ``:`` or end-of-line.
    (re.compile(r"(?:password|passphrase)\b[^\n:]{0,120}:", re.I),
     "CLI is asking for a password / passphrase. Cannot prompt over "
     "non-TTY. Use SSH keys, .netrc, env vars (e.g. PGPASSWORD), or "
     "stdin_text + keep_stdin_open=true (insecure for real secrets)."),
    (re.compile(r"username:", re.I),
     "CLI is asking for a username. Pass it explicitly via --user, env "
     "var, or config file."),
    (re.compile(r"sudo: a terminal is required", re.I),
     "sudo needs a TTY. Use NOPASSWD sudoers config, or run the daemon "
     "as the target user, or avoid sudo in agent commands."),
    # browser-auth
    (re.compile(r"open the following url|press enter to open", re.I),
     "CLI is launching browser auth. This requires a TTY user. Use API "
     "tokens via env vars (e.g. GH_TOKEN, VERCEL_TOKEN) instead of "
     "interactive login."),
    # editor
    (re.compile(r"select.*editor|editor not configured", re.I),
     "Tool wants to open an editor. Default EDITOR=true is set, but "
     "this tool ignored it. Pass the inputs as flags (e.g. git commit -m)."),
    # generic stdin reads
    (re.compile(r"press \[?enter\]?|press any key", re.I),
     "CLI wants Enter / any key. Pass stdin_text='\\n' with "
     "keep_stdin_open=true, or use the tool's non-interactive flag."),
]


def _detect_hang_directive(stdout_tail: str, stderr_tail: str) -> str | None:
    """Return a human-readable directive when a prompt pattern is found
    in recent stdout/stderr, ``None`` otherwise.

    Scans the last ~600 chars of each stream for known prompt patterns
    and returns the first matching hint with the literal prompt line
    that triggered it (so the agent knows exactly which CLI prompt to
    address).
    """
    for stream_label, content in (("stdout", stdout_tail), ("stderr", stderr_tail)):
        if not content:
            continue
        # Only inspect the tail — prompts are always near the end of
        # output (they're the LAST thing the CLI printed before sitting
        # waiting for a reply).
        tail = content[-600:]
        for pattern, hint in _PROMPT_PATTERN_HINTS:
            m = pattern.search(tail)
            if m:
                # Find the line that contained the match for context.
                line_start = tail.rfind("\n", 0, m.start()) + 1
                line_end = tail.find("\n", m.end())
                if line_end == -1:
                    line_end = len(tail)
                prompt_line = tail[line_start:line_end].strip()
                return (
                    f"Detected interactive prompt on {stream_label}: "
                    f"{prompt_line!r}. {hint}"
                )
    return None


# ── Background task tracking ────────────────────────────────────


@dataclass
class BackgroundTask:
    """Tracks a long-running subprocess."""

    task_id: str
    command: str
    cwd: str
    pid: int
    started_at: str
    process: asyncio.subprocess.Process
    stdout_lines: deque[str] = field(default_factory=lambda: deque(maxlen=_BG_MAX_LINES))
    stderr_lines: deque[str] = field(default_factory=lambda: deque(maxlen=_BG_MAX_LINES))
    exit_code: int | None = None
    finished_at: str | None = None
    _reader_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _progress_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _last_notified_lines: int = field(default=0, repr=False)

    @property
    def is_running(self) -> bool:
        return self.exit_code is None

    @property
    def uptime_seconds(self) -> float:
        start = datetime.fromisoformat(self.started_at)
        end = datetime.fromisoformat(self.finished_at) if self.finished_at else datetime.now(timezone.utc)
        return (end - start).total_seconds()


# ── Audit logging ───────────────────────────────────────────────


def _audit(action_name: str, command: str, cwd: str, exit_code: int | None, error: str | None) -> None:
    logger.info(
        "shell_audit action=%s command=%r cwd=%s exit_code=%s error=%s ts=%s",
        action_name, command, cwd, exit_code, error,
        datetime.now(timezone.utc).isoformat(),
    )


# ── Output sanitization ────────────────────────────────────────


def _sanitize_output(text: str, extra_patterns: list[str] | None = None) -> str:
    """Redact sensitive environment variable values from command output."""
    all_patterns = list(SENSITIVE_ENV_KEYS) + (extra_patterns or [])
    for key, value in os.environ.items():
        if not value or len(value) < 8:
            continue
        if any(f in key.lower() for f in all_patterns):
            text = text.replace(value, "***REDACTED***")
    return text


# ── Config model ────────────────────────────────────────────────


class ShellConfig(BaseModel):
    """Shell module configuration."""

    model_config = {"extra": "forbid"}

    workspace: str = PydanticField(
        default="",
        description=(
            "Auto-injected by the daemon at module init time. "
            "Do NOT set manually in YAML - the daemon resolves it from "
            "the app's workspace/workspace_mode config."
        ),
    )
    timeout: int = PydanticField(300, ge=1, le=1800, description="Default timeout (seconds)")
    max_output_bytes: int = PydanticField(1_000_000, ge=1000, le=100_000_000, description="Max output size")
    sanitize_output: bool = PydanticField(True, description="Redact secrets from output")
    persist_large_output: bool = PydanticField(True, description="Save large output (>30KB) to disk")
    large_output_threshold: int = PydanticField(30_000, ge=1000, description="Bytes threshold for disk persistence")
    max_persisted_bytes: int = PydanticField(64_000_000, ge=100_000, description="Max bytes to persist to disk (64MB)")


# Redundant ``cd <basename> &&`` rewrite: LLMs trained on stateless
# shell sessions reprefix ``cd web && ...`` every turn even though our
# shell module persists cwd between calls. Once we're already inside
# ``web/``, the next ``cd web`` looks for ``web/web/`` and fails. We
# detect ``cd <X>`` (unquoted, single-segment, relative) when the
# basename of cwd matches X, and strip that prefix. Multi-level cds
# (``cd a/b``) and absolute cds (``cd /foo`` / ``cd C:/foo``) are
# left alone - we only handle the exact "agent forgot the cwd
# persists" pattern.
_LEADING_CD_PATTERN = re.compile(
    r"""^\s*cd\s+
        (?:                  # the cd target -- one of:
            "([^"\\]+)"       #   double-quoted (no escapes / no slashes inside)
            | '([^'\\]+)'     #   single-quoted
            | (\S+)           #   bare token (no whitespace)
        )
        \s*&&\s*             # mandatory ``&&`` separator
    """,
    re.VERBOSE,
)


def _strip_redundant_cd(command: str, cwd: str) -> str:
    """Strip ``cd <X> &&`` from the front of ``command`` when cwd's
    basename already matches ``X``.

    Returns the command unchanged when:
      - it does not start with ``cd <X> && ...``
      - ``X`` contains a path separator (``cd a/b``) - multi-level
      - ``X`` is absolute (``cd /foo`` or ``C:/foo``)
      - ``X`` resolves to ``..`` (``cd ../web``)
      - the current cwd's basename does not match ``X``
    """
    if not cwd:
        return command
    m = _LEADING_CD_PATTERN.match(command)
    if not m:
        return command
    target = (m.group(1) or m.group(2) or m.group(3) or "").strip()
    if not target:
        return command
    # Reject multi-level / absolute / parent-relative targets.
    if "/" in target or "\\" in target:
        return command
    if target.startswith(".") or target == "~":
        return command
    # Compare cwd's basename. Normalise to forward slashes + lowercase
    # because Windows is case-insensitive and the user reported the
    # symptom on Windows (``cd web`` from ``...\test_workspace_2\web``).
    cwd_base = os.path.basename(cwd.rstrip("/\\")).lower()
    if cwd_base != target.lower():
        return command
    rewritten = command[m.end():].lstrip()
    if not rewritten:
        # The whole command was just ``cd <X>`` - turn it into a no-op
        # rather than returning empty (which the shell might error on).
        return "true"
    return rewritten


# Sleep detection - block bare sleep >2s (like Claude Code)
_SLEEP_PATTERN = re.compile(r"^\s*sleep\s+(\d+(?:\.\d+)?)\s*$")

# Sed interception - detect sed -i commands to convert to filesystem.edit
_SED_I_PATTERN = re.compile(r"""sed\s+-i(?:\s+|=['"])""")

# Image detection - base64-encoded images in output
_BASE64_IMAGE_PREFIX = re.compile(r"data:image/(png|jpeg|gif|webp);base64,")
_RAW_PNG_B64 = "iVBORw0KGgo"
_RAW_JPEG_B64 = "/9j/"


# ── Module ──────────────────────────────────────────────────────


class ShellModule(BaseModule):
    MODULE_ID = "shell"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.LINUX, Platform.MACOS, Platform.WINDOWS]
    CONFIG_MODEL = ShellConfig

    # Environment variables that must never be set via session_env.
    # These can hijack process loading, inject code, or alter PATH resolution.
    _BLOCKED_SESSION_ENV: frozenset[str] = frozenset({
        "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH", "PYTHONSTARTUP", "NODE_OPTIONS",
        "BASH_ENV", "ENV", "CDPATH", "PROMPT_COMMAND", "PATH",
    })

    CONSTRAINTS = [
        ConstraintSpec(name="allowed_commands", type="string_list", description="Restrict executable commands.", scope="universal"),
        ConstraintSpec(name="blocked_commands", type="string_list", description="Block specific commands.", scope="universal"),
        ConstraintSpec(name="allowed_paths", type="string_list", description="Extra directories beyond workspace.", scope="module"),
        ConstraintSpec(name="unrestricted", type="boolean", description="Allow any path (disable confinement).", scope="module", default=False),
    ]

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        return [{
            "id": "shell_environment",
            "title": "Execution Environment",
            "priority": 5,
            "content": self._build_environment_prompt_section(),
        }]

    def __init__(self) -> None:
        super().__init__()
        self._adapter = get_adapter()
        self._workspace: str | None = None
        # Reference to the workspace module - injected by bootstrap so
        # `_check_cwd` can fall back to the workspace module's
        # `_resolve_sync_dir()` when ``ctx.workspace`` is missing. Keeps
        # WsWrite and Bash on the same workspace root regardless of how
        # the UI propagates the per-message workspace.
        self._workspace_module: Any | None = None
        self._tasks: dict[str, BackgroundTask] = {}
        # Per-session tracking of task_ids - used by cleanup_session to kill
        # the right background tasks when a session ends.
        self._session_tasks: dict[str, set[str]] = {}
        # Lock for atomic track + cleanup operations on _session_tasks
        import threading
        self._session_tasks_lock = threading.RLock()
        self._sanitize: bool = True
        self._extra_sensitive: list[str] = []
        # Per-session persisted cwd - `cd` in session A must never leak
        # into session B. The shell module is a shared singleton, so a
        # single instance-wide attribute was causing `cd foo` in an
        # earlier session to poison later sessions with a fantasy cwd.
        self._persisted_cwd_by_session: dict[str, str] = {}

    @property
    def _persisted_cwd(self) -> str | None:
        return self._persisted_cwd_by_session.get(self._current_session_id())

    @_persisted_cwd.setter
    def _persisted_cwd(self, value: str | None) -> None:
        sid = self._current_session_id()
        if value is None:
            self._persisted_cwd_by_session.pop(sid, None)
        else:
            self._persisted_cwd_by_session[sid] = value

    def _current_session_id(self) -> str:
        """Resolve the current session_id from execution context."""
        try:
            ctx = self._context_var.get()
            if ctx is not None and getattr(ctx, "session_id", None):
                return ctx.session_id
        except Exception:
            pass
        return "_default"

    def _track_session_task(self, task_id: str) -> None:
        """Associate a background task with the current session.

        Atomic against cleanup_session via _session_tasks_lock.
        """
        sid = self._current_session_id()
        with self._session_tasks_lock:
            self._session_tasks.setdefault(sid, set()).add(task_id)

    def _cleanup_old_finished_tasks(self, max_age_seconds: float = 3600) -> int:
        """Remove finished tasks older than max_age_seconds.

        Called from _bash_status_mode to opportunistically clean up the _tasks dict.
        Prevents unbounded growth from forgotten background tasks.
        """
        from datetime import datetime as _dt
        now = _dt.now(timezone.utc)
        removed = 0
        for tid in list(self._tasks.keys()):
            task = self._tasks.get(tid)
            if task is None or task.is_running:
                continue
            if not task.finished_at:
                continue
            try:
                finished = _dt.fromisoformat(task.finished_at)
                age = (now - finished).total_seconds()
                if age > max_age_seconds:
                    self._tasks.pop(tid, None)
                    # Also remove from session tracking
                    for sid_set in self._session_tasks.values():
                        sid_set.discard(tid)
                    removed += 1
            except (ValueError, TypeError):
                pass
        if removed > 0:
            logger.debug("shell_cleanup_old_tasks removed=%d", removed)
        return removed

    def _task_gone_result(self, task_id: str | None, mode: str) -> ActionResult:
        """Uniform response when an agent queries a background task that
        no longer exists in ``self._tasks``.

        Two distinct cases collapse here:

        - The task ran, finished, and was popped by the >1h cleanup or
          a session cleanup (abort, app re-deploy).
        - The daemon was restarted, wiping the in-RAM task table while
          the conversation history (which still names the task_id) was
          rehydrated from the DB.

        The previous error string ("Task 'X' not found. Active tasks:
        (none)") looked alarming in the chat AND tempted the LLM to
        retry the lookup with the same id. This message is explicit
        about what happened and tells the agent to STOP querying that
        id (the task_id will never come back).
        """
        active = list(self._tasks.keys())
        if active:
            tail = (
                "Currently tracked task ids: "
                + ", ".join(f"'{t}'" for t in active[:5])
                + ("…" if len(active) > 5 else "")
            )
        else:
            tail = (
                "No background tasks are currently tracked - the previous "
                "task table was likely wiped by a daemon restart or session "
                "cleanup."
            )
        return ActionResult(
            success=False,
            error=(
                f"Background task '{task_id}' is no longer tracked. "
                f"Do not retry this lookup - the id will not come back. "
                f"{tail}"
            ),
            data={
                "task_id": task_id,
                "status": "expired",
                "mode": mode,
            },
        )

    async def cleanup_session(self, session_id: str) -> None:
        """Kill all background tasks for a session and remove tracking.

        Atomic against _track_session_task via _session_tasks_lock.
        Sends a 'cancelled' notification for each killed task so the agent
        is aware on the next turn (e.g. after abort + resume).
        """
        with self._session_tasks_lock:
            task_ids = self._session_tasks.pop(session_id, set())
        for tid in task_ids:
            task = self._tasks.pop(tid, None)
            if task is None:
                continue
            try:
                was_running = task.is_running
                if was_running:
                    try:
                        task.process.kill()
                    except ProcessLookupError:
                        pass
                    task.exit_code = -9
                    task.finished_at = datetime.now(timezone.utc).isoformat()
                if task._progress_task and not task._progress_task.done():
                    task._progress_task.cancel()
                if task._reader_task and not task._reader_task.done():
                    task._reader_task.cancel()
                # Notify agent so it knows the task was cancelled.
                # session_id stamped explicitly so push_module_notification
                # routes to the right per-session queue (the shared shell
                # module has no contextvar set at session-cleanup time -
                # without this the notification lands in "_standalone"
                # and the poller drops it on the floor).
                if was_running:
                    self._notify_bg({
                        "task_id": tid,
                        "tool_name": "shell.bash",
                        "status": "cancelled",
                        "elapsed_seconds": round(task.uptime_seconds, 1),
                        "result_preview": "(task cancelled by session cleanup)",
                        "hint": f"Background task '{tid}' ({task.command[:60]}) was cancelled.",
                        "session_id": session_id,
                    })
            except Exception as exc:
                logger.debug("shell_cleanup_task_failed task=%s: %s", tid, exc)
        self._persisted_cwd_by_session.pop(session_id, None)
        logger.debug("shell_cleanup_session session=%s killed=%d", session_id, len(task_ids))

    async def cancel_task(
        self,
        session_id: str,
        task_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Cancel a specific background task (user-initiated).

        Returns a dict with cancel result. Sends notification to agent.
        Called from API route when user clicks cancel on a bg task.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return {"success": False, "error": f"Task '{task_id}' not found."}
        if not task.is_running:
            return {"success": True, "already_finished": True, "exit_code": task.exit_code}

        try:
            task.process.terminate()
            try:
                await asyncio.wait_for(task.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                task.process.kill()
                await task.process.wait()
        except ProcessLookupError:
            pass

        task.exit_code = task.process.returncode
        task.finished_at = datetime.now(timezone.utc).isoformat()

        if task._progress_task and not task._progress_task.done():
            task._progress_task.cancel()

        # Notify agent that user cancelled this task. Stamp session_id +
        # user_id explicitly: this method is invoked from a plain HTTP
        # request (no ExecutionContext on the contextvar), so without
        # them push_module_notification falls back to "_standalone" and
        # the notification poller skips that queue - the agent would
        # never get woken up. The shared shell module has no per-session
        # state to lean on either, so the routing keys must come from
        # the caller.
        notification: dict[str, Any] = {
            "task_id": task_id,
            "tool_name": "shell.bash",
            "status": "cancelled",
            "elapsed_seconds": round(task.uptime_seconds, 1),
            "result_preview": "(cancelled by user)",
            "hint": f"User cancelled task '{task_id}' ({task.command[:60]}).",
            "session_id": session_id,
        }
        if user_id:
            notification["user_id"] = user_id
        self._notify_bg(notification)

        return {"success": True, "task_id": task_id, "status": "cancelled", "exit_code": task.exit_code}

    async def on_config_update(self, config: dict[str, Any]) -> None:
        if isinstance(self._config, ShellConfig):
            self._sanitize = self._config.sanitize_output
        else:
            self._sanitize = config.get("security", {}).get("sanitize_output", True) if isinstance(config, dict) else True
        self._extra_sensitive = config.get("security", {}).get("sensitive_patterns", []) if isinstance(config, dict) else []
        ws = config.get("workspace") if isinstance(config, dict) else None
        if ws:
            self._workspace = str(ws)
        await super().on_config_update(config)

    async def on_stop(self) -> None:
        for task in list(self._tasks.values()):
            if task.is_running:
                try:
                    task.process.kill()
                except ProcessLookupError:
                    pass
            if task._progress_task and not task._progress_task.done():
                task._progress_task.cancel()
            if task._reader_task and not task._reader_task.done():
                task._reader_task.cancel()
        self._tasks.clear()

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": "Shell command execution with background task support.",
        })

    def get_context_snippet(self) -> str | None:
        shell_family = self._shell_family()
        workspace = self.workspace or self._workspace or WORKSPACE_PLACEHOLDER
        lines = [
            f"Shell: {self._adapter.platform_name} "
            f"({self._adapter.default_shell}, dialect={shell_family})",
            f"Session workspace: {workspace}",
        ]
        if self._persisted_cwd:
            lines.append(f"Current shell working directory: {self._persisted_cwd}")
        return "\n".join(lines)

    def _shell_family(self) -> str:
        shell = os.path.basename(self._adapter.default_shell).lower()
        if shell in {"powershell.exe", "pwsh.exe", "powershell", "pwsh"}:
            return "powershell"
        if shell in {"cmd.exe", "cmd"}:
            return "cmd"
        if "bash" in shell:
            return "bash"
        if shell in {"sh", "zsh", "fish", "dash"}:
            return shell
        return "unknown"

    def _build_environment_prompt_section(self) -> str:
        workspace = self.workspace or self._workspace or WORKSPACE_PLACEHOLDER
        shell_family = self._shell_family()
        lines = [
            f"- OS family: {self._adapter.platform_name}",
            f"- Shell executable for shell.bash: {self._adapter.default_shell}",
            f"- Shell command dialect: {shell_family}",
            f"- Session workspace root: {workspace}",
            f"- Path separator: {os.sep}",
            f"- Temp directory: {tempfile.gettempdir()}",
            "- Initial shell working directory: the session workspace root above, unless a command changes it.",
            "",
            "Rules:",
            "- Match commands to the shell command dialect above.",
            "- Do not assume Linux, macOS, Bash, or PowerShell unless the environment block says so.",
            "- On Windows, prefer PowerShell syntax only when the shell command dialect is powershell.",
            "- On Windows with cmd dialect, do not use Bash builtins like `pwd`, `ls`, `export`, or `cat`.",
            "- Treat the session workspace root as your working area.",
            "- You START in the workspace root. Do NOT `cd` to reach it - use relative paths directly (e.g. `pytest tests/` not `cd /path/to/workspace && pytest`).",
            "- If a shell command changes directory, trust the latest shell tool result for the current working directory.",
            "- Use filesystem tools for file reads/edits/search instead of shell commands whenever possible.",
        ]
        if shell_family == "powershell":
            lines.extend([
                "",
                "PowerShell examples:",
                "- Current directory: `Get-Location` or `pwd`",
                "- List files: `Get-ChildItem`",
                "- Set env var: `$env:NAME = \"value\"`",
            ])
        elif shell_family == "cmd":
            lines.extend([
                "",
                "cmd examples:",
                "- Current directory: `cd`",
                "- List files: `dir`",
                "- Set env var: `set NAME=value`",
            ])
        else:
            lines.extend([
                "",
                "POSIX shell examples:",
                "- Current directory: `pwd`",
                "- List files: `ls`",
                "- Set env var: `export NAME=value`",
            ])
        return "\n".join(lines)

    # ── Security checks ──────────────────────────────────────

    def _check_forbidden(self, command: str, action_name: str) -> ActionResult | None:
        # BUG-077 + BUG-083: catch command lines that reference the
        # daemon-private files (signing keys, master key, DB) and
        # refuse them at the shell layer. This is a second wall of
        # defence - the admin gate on /api/modules/execute (BUG-061)
        # is the first, the filesystem-module denylist is the third.
        _sensitive_keywords = (
            ".digitorn/jwt.key", ".digitorn\\jwt.key",
            ".digitorn/master.key", ".digitorn\\master.key",
            ".digitorn/server.key", ".digitorn\\server.key",
            ".digitorn/digitorn.db", ".digitorn\\digitorn.db",
            ".claude/.credentials.json", ".claude\\.credentials.json",
        )
        _lowered = command.lower()
        for needle in _sensitive_keywords:
            if needle.lower() in _lowered:
                _audit(action_name, command, "", None, f"denied_secret:{needle}")
                return ActionResult(
                    success=False,
                    error=(
                        "Command rejected: references a daemon-private "
                        f"path ('{needle}'). Signing keys, master "
                        "encryption keys, and the daemon DB are never "
                        "accessible from any tool, even with an admin "
                        "token - this is defence-in-depth against "
                        "token-forging and secret-decryption attacks."
                    ),
                )

        match = self._adapter.is_forbidden(command)
        if match:
            _audit(action_name, command, "", None, f"forbidden:{match}")
            return ActionResult(success=False, error=f"Command rejected: forbidden pattern '{match}'.")

        # Defence-in-depth: catch infinite-wait patterns that LLMs invent
        # when "wait until X" is in their head but they pick the wrong
        # primitive (tail -f /dev/null, sleep infinity, etc.). The agent's
        # intent is legitimate; the implementation freezes the turn.
        wait_hint = self._adapter.infinite_wait_hint(command)
        if wait_hint:
            _audit(action_name, command, "", None, f"infinite_wait:{command[:80]}")
            return ActionResult(
                success=False,
                error=f"Command rejected: infinite-wait pattern. {wait_hint}",
            )

        ctx = getattr(self, "_context", None)
        if ctx and hasattr(ctx, "constraints") and ctx.constraints:
            blocked = ctx.constraints.get("blocked_commands", [])
            cmd_base = command.split()[0] if command.strip() else ""
            for b in blocked:
                if b in command or cmd_base == b:
                    return ActionResult(success=False, error=f"Command '{cmd_base}' is blocked.")
            allowed = ctx.constraints.get("allowed_commands", [])
            if allowed and cmd_base not in allowed:
                return ActionResult(success=False, error=f"Command '{cmd_base}' not in allowed_commands: {allowed}")
        return None

    def _check_cwd(self, action_name: str) -> tuple[str, ActionResult | None]:
        requested = self._persisted_cwd
        ctx = self._context_var.get()
        ctx_ws = ctx.workspace if ctx else None
        ws = ctx_ws or self.workspace or self._workspace
        # When the per-message body omits ``workspace`` (some UI flows
        # only set it on session creation, not every send), ctx.workspace
        # is None and ``ws`` resolves to the daemon's startup cwd. The
        # agent then issues ``cd <subdir>`` and hits ENOENT because the
        # daemon dir has no such subdir, while ``WsWrite`` happily wrote
        # to the user-picked dir via the workspace module's own
        # ``_resolve_sync_dir`` chain (which checks
        # ``preview._session_workspaces[sid]`` first).
        # Bridge that gap: when the workspace module is wired in and
        # ctx.workspace is missing, ask it for the canonical session
        # sync dir. Mirrors workspace's resolution exactly so WsWrite
        # and Bash always agree on the workspace root.
        if not ctx_ws:
            ws_mod = getattr(self, "_workspace_module", None)
            if ws_mod is not None:
                try:
                    resolved = ws_mod._resolve_sync_dir()
                except Exception:
                    resolved = None
                if resolved:
                    ws = resolved
        if requested and ws:
            try:
                Path(requested).resolve().relative_to(Path(ws).resolve())
            except ValueError:
                requested = None
        cwd, err = self._adapter.resolve_cwd(requested, ws)
        if err:
            _audit(action_name, "", requested or "", None, err)
            return "", ActionResult(success=False, error=err)
        return cwd, None

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Force UTF-8 everywhere - prevents cp1252 UnicodeEncodeError on Windows
        # when subprocess output contains emojis or non-ASCII characters.
        if _IS_WINDOWS:
            env.setdefault("PYTHONUTF8", "1")
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("LANG", "en_US.UTF-8")
        # Prepend the workspace to PYTHONPATH so `python -c "from src.foo
        # import ..."` works regardless of any parent pytest.ini /
        # pyproject.toml that would otherwise hijack sys.path. We intentionally
        # ignore the _BLOCKED_SESSION_ENV list here because that list
        # protects against user-injected PYTHONPATH, not our own safe
        # workspace-prefix default.
        ws = self.workspace or self._workspace
        if ws:
            try:
                ws_resolved = str(Path(ws).resolve())
                existing = env.get("PYTHONPATH", "")
                sep = ";" if _IS_WINDOWS else ":"
                parts = [p for p in existing.split(sep) if p]
                if ws_resolved not in parts:
                    env["PYTHONPATH"] = (
                        ws_resolved + (sep + existing if existing else "")
                    )
            except Exception:
                logger.debug("pythonpath_prepend_failed ws=%s", ws, exc_info=True)
        return env

    _SAFE_SYSTEM_PATHS = frozenset({
        # Unix
        "/usr/bin", "/usr/local/bin", "/usr/sbin", "/bin", "/sbin",
        "/usr/lib", "/usr/local/lib", "/usr/share",
        "/tmp", "/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr",
        # Windows (common system dirs)
        "C:\\Windows", "C:\\Windows\\System32", "C:\\Program Files",
        "C:\\Program Files (x86)",
    })

    def _check_command_paths(self, command: str, action_name: str) -> ActionResult | None:
        """Block commands with absolute paths outside workspace."""
        import shlex

        ctx = getattr(self, "_context", None)
        constraints = ctx.constraints if ctx and ctx.constraints else {}

        if constraints.get("unrestricted") is True:
            return None

        workspace = Path(self.workspace).resolve() if self.workspace else None
        extra_paths = constraints.get("allowed_paths", [])

        allowed_roots: list[Path] = []
        if workspace:
            allowed_roots.append(workspace)
        # Always allow the user's home directory and /tmp
        allowed_roots.append(Path.home().resolve())
        _tmp = Path(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")))
        if _tmp.exists():
            allowed_roots.append(_tmp.resolve())
        for p in extra_paths:
            allowed_roots.append(Path(p).resolve())

        try:
            tokens = shlex.split(command)
        except ValueError:
            return None

        for token in tokens:
            # Check absolute paths: Unix (/) and Windows (C:\)
            is_abs = token.startswith("/") or (len(token) >= 3 and token[1] == ":" and token[2] in ("/", "\\"))
            if not is_abs or len(token) < 2:
                continue
            # Skip flags that look like paths (e.g. /-v, /dev/null)
            if token.startswith("/") and len(token) > 1 and token[1] == "-":
                continue
            # Skip common Unix pseudo-paths used in bash
            if token in ("/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"):
                continue
            # Convert Git Bash paths (/c/Users/...) to Windows (C:/Users/...)
            _resolved_token = token
            if len(token) >= 3 and token[0] == "/" and token[1].isalpha() and token[2] == "/":
                _resolved_token = token[1].upper() + ":" + token[2:]
            if any(
                _resolved_token == s or _resolved_token.startswith(s + "/") or _resolved_token.startswith(s + "\\")
                for s in self._SAFE_SYSTEM_PATHS
            ):
                continue
            resolved = Path(_resolved_token).resolve()
            inside = any(
                True for root in allowed_roots
                if _is_inside(resolved, root)
            )
            if not inside:
                _audit(action_name, command, "", None, f"path_outside:{token}")
                return ActionResult(success=False, error=(
                    f"Path '{token}' is outside the workspace. "
                    "Use a relative path or add it to constraints.allowed_paths."
                ))
        return None

    async def _check_port_available_for_command(
        self, command: str,
    ) -> ActionResult | None:
        """Pre-flight check: when ``command`` clearly binds a TCP port,
        refuse if that port is already in use.

        Why: Python's ``http.server`` on Windows may silently share
        the port with a zombie from a previous session via SO_REUSEADDR
        - the second process appears to start fine but requests are
        served from the wrong cwd, leading to mysterious 404s. Same
        story for some webpack-dev-server / vite versions that swallow
        the OSError. Catching it here is cheaper than debugging it
        downstream.
        """
        import re as _re
        # Extract port from common patterns. Order matters - the more
        # specific patterns first so we don't false-match things like
        # ``--config 8000.cfg``.
        port: int | None = None
        # python -m http.server PORT (positional)
        m = _re.search(
            r"\bpython\d?\s+(?:-\S+\s+)*-m\s+http\.server\s+(\d{2,5})\b",
            command,
        )
        if m:
            port = int(m.group(1))
        else:
            # --port PORT / --port=PORT / -p PORT (npm/uvicorn/gunicorn/django)
            m = _re.search(
                r"--port[=\s](\d{2,5})\b|(?<!\w)-p\s+(\d{2,5})\b",
                command,
            )
            if m:
                port = int(m.group(1) or m.group(2))
        if port is None or port < 1 or port > 65535:
            return None

        # Quick TCP connect to detect "something already listening here".
        # Bound to ~250ms so we don't add noticeable latency.
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port),
                timeout=0.25,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
            # Nothing listening - good, we can bind.
            return None

        return ActionResult(
            success=False,
            error=(
                f"Port {port} is already in use on 127.0.0.1. The "
                f"command would either fail to bind or (on Windows) "
                f"silently share the port with the existing process, "
                f"causing the iframe to load files from the wrong "
                f"directory. PREFER a different port (try {port + 1}, "
                f"{port + 100}, or any free TCP port). To kill the "
                f"squatter instead:\n"
                f"  Linux/macOS: lsof -ti :{port} | xargs -r kill -9\n"
                f"  Windows (Git Bash): use SINGLE quotes around the "
                f"PowerShell command so bash doesn't expand $_ - "
                f"powershell -Command 'Get-NetTCPConnection -LocalPort "
                f"{port} -ErrorAction SilentlyContinue | ForEach-Object "
                f"{{ Stop-Process -Id $_.OwningProcess -Force }}'"
            ),
        )

    def _do_sanitize(self, text: str) -> str:
        if not self._sanitize:
            return text
        return _sanitize_output(text, self._extra_sensitive)

    # ── bash ─────────────────────────────────────────────────

    @action(
        description="Execute shell commands: sync, async, status check, or kill.",
        tool_prompt=(
            "Execute shell commands. Your interface to git, build tools, test runners, and system commands.\n"
            "\n"
            "## Modes\n"
            "Bash(command='ls -la') - sync, wait for result (default timeout: 300s)\n"
            "Bash(command='npm start', run_in_background=true) - async, returns task_id\n"
            "Bash(task_id='abc') - check background task status\n"
            "Bash(task_id='abc', kill=true) - terminate background task\n"
            "Bash(task_id='abc', stdin_text='y\\n') - send input to interactive process\n"
            "\n"
            "## When to use\n"
            "- git operations: status, diff, add, commit, push, log, branch\n"
            "- Build tools: make, npm run build, cargo build, pip install\n"
            "- Test runners: pytest, npm test, cargo test, go test\n"
            "- Package managers: npm install, pip install, cargo add\n"
            "- System: ls, pwd, env, which, file, wc\n"
            "- Long-running: dev servers, watchers, compilers → use run_in_background=true\n"
            "\n"
            "## When NOT to use - use dedicated tools\n"
            "- Read files → Read (NOT cat/head/tail)\n"
            "- Edit files → Edit (NOT sed/awk)\n"
            "- Create files → Write (NOT echo/cat)\n"
            "- Search content → Grep (NOT grep/rg)\n"
            "- Find files → Glob (NOT find/ls)\n"
            "\n"
            "## Git safety rules\n"
            "- ALWAYS git status before and after changes\n"
            "- ALWAYS git diff to review before committing\n"
            "- ALWAYS stage specific files (git add path/to/file) - NEVER git add -A or git add .\n"
            "- ALWAYS create NEW commits - NEVER amend unless explicitly asked\n"
            "- NEVER skip hooks (--no-verify) or force push (--force)\n"
            "- NEVER push unless explicitly told to\n"
            "\n"
            "## Behavior rules\n"
            "- Check exit codes - non-zero means failure, read stderr\n"
            "- If a command fails, diagnose the error before retrying\n"
            "- For destructive commands (rm -rf, git reset), ask the user first\n"
            "- Chain independent commands with && (stops on first failure)\n"
            "- Long builds/tests → run_in_background=true, then check with task_id\n"
            "- After implementing changes, ALWAYS run tests to verify"
        ),
        params_model=BashParams,
        permissions=["process.exec"],
        risk_level="high",
        tags=["shell", "exec"],
        aliases=["executer", "commande", "cmd", "run"],
        cli_label="Bash",
        cli_param="command",
        cli_show_output=True,
        cli_output_lines=5,
    )
    async def bash(self, params: BashParams) -> ActionResult:
        # ── Mode dispatch ────────────────────────────────────────

        # Mode 6: Wait for background task to complete (task_id + wait=true)
        if params.task_id and params.wait:
            return await self._bash_wait_mode(params)

        # Mode 7: Stream output with filtering (command + stream=true)
        if params.command and params.stream:
            return await self._bash_stream_mode(params)

        # Mode 4: Kill task (task_id + kill flag) - check BEFORE status to avoid misroute
        if params.task_id and params.kill:
            return await self._bash_kill_mode(params)

        # Mode 5: Send stdin to background task (task_id + stdin_text) - check BEFORE status
        if params.task_id and params.stdin_text is not None:
            return await self._bash_stdin_mode(params)

        # Mode 3: Status check (task_id provided, no command)
        if params.task_id and not params.command:
            return await self._bash_status_mode(params)

        # Mode 4: Async execution (run_in_background flag)
        if params.run_in_background:
            return await self._bash_async_mode(params)

        # Mode 5: Sync execution (default, requires command)
        if not params.command:
            return ActionResult(
                success=False,
                error="Must provide either command (sync/async) or task_id (status/kill/stdin)"
            )
        return await self._bash_sync_mode(params)

    # ── Mode handlers (internal methods, not @action) ──

    async def _bash_sync_mode(self, params: BashParams) -> ActionResult:
        """Synchronous execution: run command, wait for result."""
        command = params.command
        assert command is not None

        # ── Sleep detection (like Claude Code) ──────────────────
        sleep_match = _SLEEP_PATTERN.match(command.strip())
        if sleep_match:
            duration = float(sleep_match.group(1))
            if duration > 2:
                return ActionResult(
                    success=False,
                    error=(
                        f"Blocked: 'sleep {duration}' would waste time. "
                        "Use Bash(run_in_background=true) for long-running commands, or "
                        "remove the sleep and check the condition directly."
                    ),
                )

        # ── Sed -i interception (like Claude Code) ──────────────
        if _SED_I_PATTERN.search(command):
            return ActionResult(
                success=False,
                error=(
                    "Do not use 'sed -i' to edit files. Use the filesystem.edit() action instead - "
                    "it provides surgical find-and-replace with undo support and proper validation. "
                    "Example: filesystem.edit(path='file.py', old_string='...', new_string='...')"
                ),
            )

        if (r := self._check_forbidden(command, "bash")):
            return r
        if (r := self._check_command_paths(command, "bash")):
            return r
        cwd, cwd_err = self._check_cwd("bash")
        if cwd_err:
            return cwd_err

        # Defensive: strip a leading ``cd <basename> &&`` when the cwd
        # is ALREADY inside <basename>. The shell module persists cwd
        # across calls, but LLMs trained on stateless shell sessions
        # repeatedly prefix ``cd web && ...`` even after a previous
        # ``cd web`` succeeded. The follow-up then resolves to
        # ``cwd/web/web`` which doesn't exist, and the command fails
        # with ``cd: web: No such file or directory``. We rewrite to
        # the equivalent command that runs in the current cwd. Same
        # outcome, no behaviour change for callers that genuinely
        # want to descend further (multi-level ``cd a/b`` is left
        # alone, and absolute ``cd /foo`` is left alone).
        command = _strip_redundant_cd(command, cwd)

        env = self._build_env()

        max_output = 1_000_000
        if isinstance(self._config, ShellConfig):
            max_output = self._config.max_output_bytes

        try:
            stdout, stderr, exit_code = await asyncio.wait_for(
                self._adapter.run_command(command, cwd, env, params.timeout, max_output),
                timeout=params.timeout + 2,
            )
        except asyncio.TimeoutError:
            _audit("bash", command, cwd, None, "timeout")
            # Best-effort prompt detection on whatever output the proc
            # managed to flush before the hard timeout. The adapter's
            # run_command buffers via communicate(), so on TimeoutError
            # we don't have direct access to the partial output here.
            # Return the standard timeout error with a hint pointing to
            # the structured-output mechanism via run_in_background +
            # stdout polling, which DOES expose partial output and
            # triggers proactive hang detection in the bg watcher.
            return ActionResult(
                success=False,
                error=(
                    f"Timed out after {params.timeout}s. If the command may "
                    f"be waiting on an interactive prompt, re-run with "
                    f"`run_in_background=true` — the background watcher "
                    f"detects prompts proactively and returns an "
                    f"actionable directive within ~30s instead of waiting "
                    f"for the full timeout."
                ),
            )
        except Exception as exc:
            # Always surface SOMETHING to the agent - some exceptions
            # have an empty ``str()`` (notably ``NotImplementedError()``
            # raised by asyncio's SelectorEventLoop on Windows when it
            # can't spawn subprocesses). Without the type-name fallback
            # the LLM sees ``error=""`` and can't react. The event loop
            # policy is force-set at server.py import time, but this
            # belt-and-braces protects against future regressions.
            _msg = str(exc) or f"{type(exc).__name__}"
            _audit("bash", command, cwd, None, _msg)
            return ActionResult(success=False, error=_msg)

        # Persist CWD only if command changed dir AND succeeded
        _update_cwd(self, command, cwd, exit_code=exit_code if exit_code is not None else 0)

        _audit("bash", command, cwd, exit_code, None if exit_code == 0 else stderr[:200])
        stdout = self._do_sanitize(stdout)
        stderr = self._do_sanitize(stderr)

        effective_cwd = self._persisted_cwd or cwd
        result_data: dict[str, Any] = {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "command": command,
            "cwd": effective_cwd,
            "platform": self._adapter.platform_name,
            "shell": self._adapter.default_shell,
        }
        # Hang-on-prompt directive: if the command was trying to read
        # an interactive prompt and got EOF (which is the universal
        # outcome with stdin=DEVNULL), the exit code is non-zero and
        # the LAST line of output is the prompt itself. Surface a
        # concrete fix the agent can apply immediately, rather than
        # leaving it to grep cryptic exit codes.
        if exit_code != 0:
            directive = _detect_hang_directive(stdout, stderr)
            if directive:
                result_data["hang_directive"] = directive

        # Smart hints on common failure modes
        if exit_code != 0:
            merged = f"{stderr}\n{stdout}".lower() if stderr or stdout else ""
            cmd_low = command.lower()
            ws_low = (self.workspace or "").lower().replace("\\", "/")

            if "no such file" in merged or "file or directory not found" in merged:
                # Only show the workspace-hint when the agent tried to cd
                # INTO the workspace full path (or into "workspace" literal
                # while being already there).
                current_cwd_low = (effective_cwd or "").lower().replace("\\", "/")
                tried_cd_to_ws = False
                if "cd " in cmd_low and ws_low:
                    cd_target = cmd_low.split("cd ", 1)[1].split()[0].strip("'\"")
                    cd_target = cd_target.replace("\\", "/")
                    # matched when target == full workspace path OR when
                    # agent is already in workspace AND target is its basename
                    if cd_target == ws_low or cd_target.rstrip("/") == ws_low.rstrip("/"):
                        tried_cd_to_ws = True
                    elif current_cwd_low == ws_low and cd_target == ws_low.rstrip("/").split("/")[-1]:
                        tried_cd_to_ws = True
                base_hint = (
                    f"Current working directory: {effective_cwd}. "
                    f"Use paths relative to this directory, or absolute paths. "
                    f"Shell cwd persists across calls."
                )
                if tried_cd_to_ws:
                    base_hint += (
                        " NOTE: you already START in the workspace - don't `cd` into it."
                    )
                result_data["hint"] = base_hint

            # pytest rootdir surprise: it picks up config files in parent dirs
            if "rootdir:" in merged and "pytest" in cmd_low:
                parent_rootdir = False
                for line in merged.splitlines():
                    if "rootdir:" in line:
                        rd = line.split("rootdir:", 1)[1].strip().lower().replace("\\", "/")
                        # rootdir is "parent" of workspace when rd is a
                        # strict prefix of ws (rd = /a, ws = /a/b/c).
                        if ws_low and rd != ws_low and ws_low.startswith(rd.rstrip("/") + "/"):
                            parent_rootdir = True
                            break
                if parent_rootdir:
                    result_data["hint"] = (
                        "pytest picked up a parent-directory config (rootdir mismatch). "
                        "Fix: run with `pytest --rootdir=. ...` or create an empty "
                        "`pytest.ini` in the workspace to anchor the root."
                    )

            # pytest exit 4 - "found no collectors" / import errors from
            # a parent pytest.ini that hijacks pythonpath. This variant
            # errors before printing the "rootdir:" banner so the check
            # above doesn't catch it.
            if "pytest" in cmd_low and exit_code == 4 and (
                "found no collectors" in merged
                or "no module named" in merged.lower()
                or "importerror" in merged.lower()
            ):
                result_data["hint"] = (
                    "pytest exit 4 - it found the file but couldn't collect tests "
                    "(often caused by a parent pytest.ini pointing pythonpath away "
                    "from the workspace). Fix options: "
                    "(1) `python -m pytest --rootdir=. -o testpaths=. -o pythonpath=. <path>`, "
                    "(2) create an empty `pytest.ini` in the workspace to anchor the root, "
                    "(3) invoke tests directly with `python -c 'from module import fn; assert ...'`."
                )

        # ── Image detection in output ───────────────────────────
        if _is_image_output(stdout):
            result_data["is_image"] = True

        # ── Large output persistence (like Claude Code >30KB → disk) ──
        persist_cfg = isinstance(self._config, ShellConfig) and self._config.persist_large_output
        threshold = self._config.large_output_threshold if isinstance(self._config, ShellConfig) else 30_000
        max_persist = self._config.max_persisted_bytes if isinstance(self._config, ShellConfig) else 64_000_000

        if persist_cfg and len(stdout.encode("utf-8", errors="replace")) > threshold:
            # Disk write off-loop: 30 KB+ stdout dumped synchronously can
            # stall the loop on slow disks long enough to drop Socket.IO.
            persisted_path = await asyncio.to_thread(
                self._persist_output, stdout, command, max_persist,
            )
            if persisted_path:
                result_data["persisted_output_path"] = persisted_path
                result_data["persisted_output_size"] = len(stdout)
                # Truncate in-memory output to save context window
                lines = stdout.splitlines()
                if len(lines) > 200:
                    head = "\n".join(lines[:100])
                    tail = "\n".join(lines[-100:])
                    result_data["stdout"] = (
                        f"{head}\n\n... ({len(lines) - 200} lines omitted, "
                        f"full output saved to {persisted_path}) ...\n\n{tail}"
                    )

        # ── Semantic exit code interpretation ───────────────────
        interpretation = _interpret_exit_code(exit_code, command, stderr)
        if interpretation:
            result_data["exit_code_meaning"] = interpretation

        error_msg = None
        if exit_code != 0:
            # Include stderr in the error message so the LLM always sees it
            parts = [f"Exit code {exit_code}."]
            if stderr and stderr.strip():
                parts.append(stderr.strip()[:2000])
            elif stdout and stdout.strip():
                # Some commands write errors to stdout (e.g. Python tracebacks)
                parts.append(stdout.strip()[:2000])
            error_msg = "\n".join(parts)

        # Disk → workspace sync. Any external tool the agent invoked
        # (npm install, npm create, cargo build, git clone, …) drops
        # files on disk that the workspace module never saw — without
        # this step, the IDE's Monaco view stays empty even though
        # the agent successfully scaffolded a whole project. We run
        # the sync after EVERY bash command (cheap when nothing
        # changed: walk tree + mtime compare), reporting the deltas
        # so the agent can confirm what landed in the IDE view.
        ws_mod = getattr(self, "_workspace_module", None)
        if ws_mod is not None and hasattr(ws_mod, "_sync_from_disk"):
            try:
                deltas = await ws_mod._sync_from_disk()
                if deltas and any(deltas.get(k, 0) for k in ("added", "modified", "deleted")):
                    result_data["workspace_sync"] = deltas
            except Exception as _exc:
                logger.debug("workspace disk sync failed: %s", _exc)

        return ActionResult(
            success=exit_code == 0,
            data=result_data,
            error=error_msg,
        )

    async def _bash_async_mode(self, params: BashParams) -> ActionResult:
        """Asynchronous execution: launch subprocess, return task_id immediately."""
        command = params.command
        assert command is not None

        if (r := self._check_forbidden(command, "bash")):
            return r
        if (r := self._check_command_paths(command, "bash")):
            return r
        cwd, cwd_err = self._check_cwd("bash")
        if cwd_err:
            return cwd_err

        # Pre-check: when the command binds a TCP port (python http.server,
        # vite/next dev --port, uvicorn/gunicorn --port, etc.), refuse
        # if the port is already in use. Catches the silent-bind-fail
        # cases where Python's http.server on Windows may silently share
        # the port with a zombie from a previous session, leading to
        # the iframe being served from the wrong cwd.
        port_check = await self._check_port_available_for_command(command)
        if port_check is not None:
            _audit("bash", command, cwd, None, port_check.error or "")
            return port_check
        # Same redundant ``cd <X> &&`` strip as sync mode - see the
        # docstring on ``_strip_redundant_cd``.
        command = _strip_redundant_cd(command, cwd)
        env = self._build_env()
        # Inject CI / non-interactive defaults so npm/yarn/git/apt/pip
        # don't prompt on stdin (covers the most common hang causes).
        from digitorn.modules.shell.platform_adapters import (
            inject_non_interactive_env,
        )
        env = inject_non_interactive_env(env)

        # stdin handling: if the agent opted into ``keep_stdin_open``,
        # we keep a writable PIPE so a later ``stdin_text`` call can
        # send input (interactive REPL / scripted password / Y/N
        # prompts the agent wants to drive). Otherwise we close stdin
        # IMMEDIATELY after spawn — child sees EOF, abort prompts
        # gracefully, no infinite hang on ``npm create``,
        # ``apt install`` confirmation, or ``git clone`` over auth.
        keep_stdin_open = bool(getattr(params, "keep_stdin_open", False))

        try:
            shell = self._adapter.default_shell
            shell_lower = shell.lower()

            if "bash" in shell_lower:
                # Git Bash (Windows) or native bash (Unix) - POSIX syntax
                proc = await asyncio.create_subprocess_exec(
                    shell, "-c", command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
            elif shell_lower.endswith(("powershell.exe", "pwsh.exe", "powershell", "pwsh")):
                proc = await asyncio.create_subprocess_exec(
                    shell,
                    "-NonInteractive",
                    "-NoProfile",
                    "-ExecutionPolicy", "Bypass",
                    "-Command", command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
            else:
                # cmd.exe fallback
                cmd_exe = os.environ.get("COMSPEC") or "cmd.exe"
                proc = await asyncio.create_subprocess_exec(
                    cmd_exe,
                    "/d", "/s", "/c", command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )

            # Close stdin immediately unless explicitly keeping it
            # open. Sends EOF to the child. ``proc.stdin`` is
            # ``StreamWriter`` here; ``.close()`` is non-blocking and
            # safe even before the child reads.
            if not keep_stdin_open and proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
        except NotImplementedError as exc:
            # Windows: subprocess only works on ProactorEventLoop. The
            # caller is on a SelectorEventLoop (sub-agent thread, ad-hoc
            # worker without policy). ``str(NotImplementedError())`` is
            # empty, so the agent would otherwise see ``error=""`` and
            # give up. Surface a clear actionable message instead.
            _msg = (
                "shell.bash background mode requires ProactorEventLoop "
                "(Windows asyncio subprocess limitation). The current "
                "loop is SelectorEventLoop. Use sync mode "
                "(no run_in_background flag) instead - it auto-falls back "
                "to thread-pool execution and works on any loop."
            )
            _audit("bash", command, cwd, None, _msg)
            return ActionResult(success=False, error=_msg)
        except Exception as exc:
            _msg = str(exc) or f"{type(exc).__name__}"
            _audit("bash", command, cwd, None, _msg)
            return ActionResult(success=False, error=_msg)

        task_id = uuid.uuid4().hex[:12]
        task = BackgroundTask(
            task_id=task_id,
            command=command,
            cwd=cwd,
            pid=proc.pid,
            started_at=datetime.now(timezone.utc).isoformat(),
            process=proc,
        )
        task._reader_task = asyncio.create_task(self._bg_reader(task, 5_000_000))
        task._progress_task = asyncio.create_task(self._bg_progress_watcher(task))
        self._tasks[task_id] = task
        self._track_session_task(task_id)

        # Wait briefly to detect immediate failures (port-in-use, missing
        # binary, syntax error, etc.). The window is longer for known-slow
        # crashers like Python (interpreter startup + asyncio init means
        # OSError surfaces ~400-500ms after spawn, so 300ms wasn't enough).
        # Most well-behaved background commands either print early stdout
        # within a few hundred ms or stay alive for minutes; the wait
        # cost is negligible vs. the cost of attaching to a dead server.
        cmd_lower = command.lower()
        is_slow_crasher = (
            "python " in cmd_lower
            or "python3 " in cmd_lower
            or "py -" in cmd_lower
            or cmd_lower.startswith("node ")
            or "npm " in cmd_lower
        )
        watchdog_s = 1.0 if is_slow_crasher else 0.3
        try:
            await asyncio.wait_for(proc.wait(), timeout=watchdog_s)
        except asyncio.TimeoutError:
            pass

        if not task.is_running:
            stderr_tail = "\n".join(list(task.stderr_lines)[-20:])
            _audit("bash", command, cwd, task.exit_code, stderr_tail[:200])
            # Clean up immediately-failed task to prevent _tasks dict bloat
            self._tasks.pop(task_id, None)
            sid = self._current_session_id()
            session_set = self._session_tasks.get(sid)
            if session_set is not None:
                session_set.discard(task_id)
            # Cancel the reader and progress tasks if still running
            if task._reader_task and not task._reader_task.done():
                task._reader_task.cancel()
            if task._progress_task and not task._progress_task.done():
                task._progress_task.cancel()
            return ActionResult(
                success=False,
                error=f"Command exited immediately with code {task.exit_code}.",
                data={"task_id": task_id, "stderr": stderr_tail},
            )

        _audit("bash", command, cwd, None, f"started:pid={proc.pid}")
        return ActionResult(
            success=True,
            data={
                "task_id": task_id,
                "pid": proc.pid,
                "command": command,
                "status": "running",
                "message": f"Task '{task_id}' started (PID {proc.pid}). Use Bash(task_id='{task_id}') to check.",
                "platform": self._adapter.platform_name,
                "shell": self._adapter.default_shell,
            },
        )

    async def _bash_status_mode(self, params: BashParams) -> ActionResult:
        """Status check: return status and output of a background task."""
        # Periodic cleanup: drop tasks that finished more than 1 hour ago
        self._cleanup_old_finished_tasks(max_age_seconds=3600)

        task = self._tasks.get(params.task_id)
        if task is None:
            return self._task_gone_result(params.task_id, mode="status")

        # Get tail of output
        n = 50  # Default tail size
        stdout = "\n".join(list(task.stdout_lines)[-n:])
        stderr = "\n".join(list(task.stderr_lines)[-n:])

        return ActionResult(success=True, data={
            "task_id": params.task_id,
            "command": task.command,
            "status": "running" if task.is_running else "finished",
            "exit_code": task.exit_code,
            "uptime_seconds": round(task.uptime_seconds, 1),
            "stdout": self._do_sanitize(stdout),
            "stderr": self._do_sanitize(stderr),
            "stdout_total_lines": len(task.stdout_lines),
            "stderr_total_lines": len(task.stderr_lines),
        })

    async def _bash_kill_mode(self, params: BashParams) -> ActionResult:
        """Kill mode: terminate a background task."""
        task = self._tasks.get(params.task_id)
        if task is None:
            return self._task_gone_result(params.task_id, mode="kill")

        if not task.is_running:
            return ActionResult(success=True, data={
                "task_id": params.task_id,
                "status": "already_finished",
                "exit_code": task.exit_code,
            })

        proc = task.process
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass

        task.exit_code = proc.returncode
        task.finished_at = datetime.now(timezone.utc).isoformat()
        _audit("bash", task.command, task.cwd, task.exit_code, "killed")

        # Cancel progress watcher
        if task._progress_task and not task._progress_task.done():
            task._progress_task.cancel()

        # Notify the bus so the FRONTEND (BackgroundService) flips the
        # task off the "running" pile. Without this hook the agent's
        # kill landed silently: the result was a `tool_call` event with
        # `status: "killed"` carried in its payload, but the frontend's
        # subscriber only watches `bg_task_update` events to update the
        # tasks panel - so the UI kept rendering the task as running
        # long after the process was dead. The relay in `_deploy.py`
        # converts this notification into a `bg_task_update` envelope
        # (`status: "cancelled"`) and the BackgroundService then
        # applies `copyWith(status: "cancelled")` on the matching row.
        # The agent itself doesn't need a re-injection here because it
        # GOT the kill result synchronously as the tool_call return
        # value - this notification is purely for the UI.
        stdout_tail = "\n".join(list(task.stdout_lines)[-5:])
        self._notify_bg({
            "task_id": params.task_id,
            "tool_name": "shell.bash",
            "status": "cancelled",
            "elapsed_seconds": round(task.uptime_seconds, 1),
            "result_preview": stdout_tail or "(killed by agent)",
            "hint": f"Agent killed task '{params.task_id}'.",
            "exit_code": task.exit_code,
        })

        return ActionResult(success=True, data={
            "task_id": params.task_id,
            "status": "killed",
            "exit_code": task.exit_code,
        })

    async def _bash_stdin_mode(self, params: BashParams) -> ActionResult:
        """Send text to a running background task's stdin."""
        task = self._tasks.get(params.task_id)
        if task is None:
            return self._task_gone_result(params.task_id, mode="stdin")
        if not task.is_running:
            return ActionResult(
                success=False,
                error=f"Task '{params.task_id}' is not running (exit code {task.exit_code})."
            )
        if task.process.stdin is None:
            return ActionResult(
                success=False,
                error="Task stdin is not available."
            )
        try:
            text = params.stdin_text
            assert text is not None
            if not text.endswith("\n"):
                text += "\n"
            task.process.stdin.write(text.encode("utf-8"))
            await task.process.stdin.drain()
            return ActionResult(success=True, data={
                "task_id": params.task_id,
                "sent": params.stdin_text.rstrip("\n"),
                "hint": f"Input sent. Use Bash(task_id='{params.task_id}') to see if the process responded.",
            })
        except BrokenPipeError:
            return ActionResult(
                success=False,
                error="Process stdin pipe is broken (process may have exited)."
            )
        except Exception as exc:
            return ActionResult(success=False, error=str(exc))

    async def _bash_wait_mode(self, params: BashParams) -> ActionResult:
        """Wait for background task to complete and return final result.

        Like my pattern of waiting for pytest to complete.
        Blocks until task finishes, then returns stdout/stderr/exit_code.
        """
        task = self._tasks.get(params.task_id)
        if task is None:
            return self._task_gone_result(params.task_id, mode="wait")

        # Wait for completion (with timeout)
        timeout = params.timeout or 300.0
        try:
            exit_code = await asyncio.wait_for(task.process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return ActionResult(
                success=False,
                error=f"Task timed out after {timeout}s. Use Bash(task_id='{params.task_id}', kill=true) to terminate.",
                data={
                    "task_id": params.task_id,
                    "status": "timeout",
                    "elapsed_seconds": round(task.uptime_seconds, 1),
                }
            )
        except Exception as e:
            return ActionResult(success=False, error=f"Wait failed: {e}")

        # Set exit code for the task
        task.exit_code = exit_code
        task.finished_at = datetime.now(timezone.utc).isoformat()

        # Return final result
        return ActionResult(
            success=exit_code == 0,
            data={
                "task_id": params.task_id,
                "status": "completed" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "stdout": "\n".join(list(task.stdout_lines)),
                "stderr": "\n".join(list(task.stderr_lines)),
                "elapsed_seconds": round(task.uptime_seconds, 1),
            }
        )

    async def _bash_stream_mode(self, params: BashParams) -> ActionResult:
        """Stream command output line-by-line with optional pattern filtering.

        Like Monitor tool - emits notifications for each matching line.
        Pattern is regex; only matching lines trigger notifications.
        """
        command = params.command
        assert command is not None

        # Launch command in a subprocess
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=params.cwd or ".",
            )
        except NotImplementedError:
            return ActionResult(success=False, error=(
                "shell.bash stream mode requires ProactorEventLoop "
                "(Windows asyncio subprocess limitation). The current "
                "loop is SelectorEventLoop. Use sync mode without "
                "stream_pattern - it auto-falls back to thread-pool "
                "execution and works on any loop."
            ))
        except Exception as e:
            return ActionResult(success=False, error=f"Failed to start command: {e}")

        # Compile pattern filter if provided
        pattern_re = None
        if params.stream_pattern:
            try:
                pattern_re = re.compile(params.stream_pattern)
            except re.error as e:
                return ActionResult(success=False, error=f"Invalid stream_pattern regex: {e}")

        # Stream lines until completion
        line_count = 0
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break

                text = line.decode("utf-8", errors="replace").rstrip("\n")
                line_count += 1

                # Filter by pattern if provided
                if pattern_re and not pattern_re.search(text):
                    continue

                # Send notification for this line (like Monitor does)
                self._notify_bg({
                    "task_id": f"stream_{id(process)}",
                    "tool_name": "shell.bash",
                    "status": "progress",
                    "result_preview": text,
                    "hint": f"Line {line_count}",
                })
        except asyncio.CancelledError:
            process.kill()
            return ActionResult(success=False, error="Stream cancelled")
        except Exception as e:
            return ActionResult(success=False, error=f"Stream error: {e}")

        # Wait for completion
        exit_code = await process.wait()

        return ActionResult(
            success=exit_code == 0,
            data={
                "command": command,
                "status": "completed" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "lines_processed": line_count,
                "platform": self._adapter.platform_name,
                "shell": self._adapter.default_shell,
            }
        )

    def _persist_output(self, stdout: str, command: str, max_bytes: int) -> str | None:
        """Save large output to a temp file and return the path."""
        try:
            ws = self.workspace or self._workspace
            output_dir = Path(ws or tempfile.gettempdir()) / ".digitorn" / "tool-results"
            output_dir.mkdir(parents=True, exist_ok=True)
            fname = f"bash_{uuid.uuid4().hex[:8]}.txt"
            dest = output_dir / fname
            data = stdout[:max_bytes].encode("utf-8", errors="replace")
            dest.write_bytes(data)
            return str(dest)
        except Exception as exc:
            logger.debug("persist_output failed: %s", exc)
            return None

    # ── Background progress watcher ──────────────────────────

    async def _bg_progress_watcher(self, task: BackgroundTask) -> None:
        """Send periodic progress notifications while task is running.

        Schedule: 5s, 15s, 30s, then every 60s.

        Two responsibilities:
          1. Periodic ``status: progress`` notifications when new output
             arrives (already implemented).
          2. **Hang detection** — if the output buffer ends with a known
             interactive prompt AND no new output has appeared for the
             last interval, kill the task and emit a structured
             ``hang_detected`` notification carrying a directive the
             agent can apply (e.g. "add --yes flag"). Bypasses the 5
             min hard timeout: the agent gets actionable feedback
             within ~30 s of the prompt.
        """
        schedule = [5, 15, 30]

        for delay in schedule:
            await asyncio.sleep(delay)
            if not task.is_running:
                return

            current_lines = len(task.stdout_lines) + len(task.stderr_lines)
            new_output = current_lines > task._last_notified_lines
            stdout_tail = "\n".join(list(task.stdout_lines)[-5:])
            stderr_tail = "\n".join(list(task.stderr_lines)[-5:])

            # Hang detection: when the buffer hasn't grown since the
            # previous check AND its tail looks like an interactive
            # prompt, kill the task and report a directive. We require
            # at least the second tick (delay >= 15 s) to avoid
            # killing a slow-starting build.
            if (
                not new_output
                and delay >= 15
                and (stdout_tail or stderr_tail)
            ):
                directive = _detect_hang_directive(stdout_tail, stderr_tail)
                if directive:
                    try:
                        task.process.kill()
                    except Exception:
                        pass
                    self._notify_bg({
                        "task_id": task.task_id,
                        "tool_name": "shell.bash",
                        "status": "hang_detected",
                        "elapsed_seconds": round(task.uptime_seconds, 1),
                        "result_preview": stdout_tail or stderr_tail,
                        "hang_directive": directive,
                        "error": (
                            "Background command was killed by the hang "
                            "detector — it printed a prompt and stopped "
                            "producing output. See hang_directive for the "
                            "fix."
                        ),
                        "hint": (
                            f"Task '{task.task_id}' killed due to "
                            f"interactive prompt. Apply the directive "
                            f"and re-run with the appropriate flag."
                        ),
                    })
                    return

            if new_output:
                task._last_notified_lines = current_lines
                self._notify_bg({
                    "task_id": task.task_id,
                    "tool_name": "shell.bash",
                    "status": "progress",
                    "elapsed_seconds": round(task.uptime_seconds, 1),
                    "result_preview": stdout_tail or "(no stdout yet)",
                    "hint": (
                        f"Task still running (PID {task.pid}). "
                        f"Use Bash(task_id='{task.task_id}') for full output."
                    ),
                })

        # After initial schedule, check every 60s until done
        while task.is_running:
            await asyncio.sleep(60)
            if not task.is_running:
                return
            current_lines = len(task.stdout_lines) + len(task.stderr_lines)
            if current_lines > task._last_notified_lines:
                task._last_notified_lines = current_lines
                stdout_tail = "\n".join(list(task.stdout_lines)[-5:])
                self._notify_bg({
                    "task_id": task.task_id,
                    "tool_name": "shell.bash",
                    "status": "progress",
                    "elapsed_seconds": round(task.uptime_seconds, 1),
                    "result_preview": stdout_tail,
                    "hint": (
                        f"Task still running after {round(task.uptime_seconds)}s. "
                        f"Use Bash(task_id='{task.task_id}') for full output."
                    ),
                })

    # ── Background reader ────────────────────────────────────

    async def _bg_reader(self, task: BackgroundTask, max_bytes: int) -> None:
        proc = task.process

        async def _read_stream(stream: asyncio.StreamReader | None, buf: deque[str]) -> None:
            if stream is None:
                return
            total = 0
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip("\n")
                if total < max_bytes:
                    buf.append(decoded)
                    total += len(line)

        await asyncio.gather(
            _read_stream(proc.stdout, task.stdout_lines),
            _read_stream(proc.stderr, task.stderr_lines),
        )
        await proc.wait()
        task.exit_code = proc.returncode
        task.finished_at = datetime.now(timezone.utc).isoformat()
        _audit("bash", task.command, task.cwd, task.exit_code, None)

        # Disk → workspace sync after the bg task exits. Same rationale
        # as sync mode: tools like ``npm run dev`` (kept running),
        # ``npm install``, etc. write files on disk that the IDE
        # otherwise can't see.
        ws_mod = getattr(self, "_workspace_module", None)
        if ws_mod is not None and hasattr(ws_mod, "_sync_from_disk"):
            try:
                await ws_mod._sync_from_disk()
            except Exception as _exc:
                logger.debug("workspace disk sync (bg) failed: %s", _exc)

        # Notify context_builder of completion
        stdout_tail = "\n".join(list(task.stdout_lines)[-20:])
        stderr_tail = "\n".join(list(task.stderr_lines)[-10:])

        notification: dict[str, Any] = {
            "task_id": task.task_id,
            "tool_name": "shell.bash",
            "status": "completed" if task.exit_code == 0 else "failed",
            "elapsed_seconds": round(task.uptime_seconds, 1),
            "result_preview": stdout_tail or "(no output)",
            "hint": f"Exit code: {task.exit_code}. Use Bash(task_id='{task.task_id}') for full output.",
        }
        if task.exit_code != 0:
            notification["error"] = f"Exit code {task.exit_code}\nStderr:\n{stderr_tail if stderr_tail else '(no stderr)'}"
        self._notify_bg(notification)



# ── Helpers ─────────────────────────────────────────────────────


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _update_cwd(
    module: ShellModule, command: str, current_cwd: str, exit_code: int = 0,
) -> None:
    """Track CWD changes from cd commands for persistence between calls.

    - Processes ALL cd commands in the chain (not just the first), so
      `cd .. && cd workspace` tracks correctly.
    - Only updates cwd when the command succeeded (exit_code == 0);
      otherwise the cd likely failed and we should keep the previous
      cwd instead of storing a fantasy path.
    - Validates that the resulting directory exists on disk.
    """
    if exit_code != 0:
        # Command failed - don't trust any cd inside it
        return
    import shlex
    try:
        parts = shlex.split(command)
    except ValueError:
        return

    cwd = current_cwd
    changed = False
    i = 0
    while i < len(parts):
        part = parts[i]
        if part == "cd" and i + 1 < len(parts):
            target = parts[i + 1]
            if target.startswith("/") or (len(target) > 1 and target[1] == ":"):
                # Absolute (unix or windows)
                cwd = target
            else:
                # Resolve each step so `cd ..` then `cd x` works correctly
                try:
                    cwd = str((Path(cwd) / target).resolve())
                except OSError:
                    pass
            changed = True
            i += 2
        else:
            i += 1

    if not changed:
        return
    # Final validation: the resulting dir must exist AND be inside the
    # workspace (the agent should never escape the session sandbox).
    try:
        if not Path(cwd).is_dir():
            return
        ws = getattr(module, "workspace", None) or getattr(module, "_workspace", None)
        if ws:
            try:
                Path(cwd).resolve().relative_to(Path(ws).resolve())
            except ValueError:
                # Outside workspace - keep the old cwd, don't update.
                return
        module._persisted_cwd = cwd
    except OSError:
        return


def _is_image_output(stdout: str) -> bool:
    """Detect if output contains base64-encoded images."""
    if not stdout:
        return False
    # Check for data URI scheme
    if _BASE64_IMAGE_PREFIX.search(stdout[:200]):
        return True
    # Check for raw base64 PNG/JPEG signatures
    stripped = stdout.strip()[:100]
    if stripped.startswith(_RAW_PNG_B64) or stripped.startswith(_RAW_JPEG_B64):
        return True
    return False


def _interpret_exit_code(exit_code: int, command: str, stderr: str) -> str | None:
    """Semantic interpretation of exit codes (like Claude Code's interpretCommandResult)."""
    if exit_code == 0:
        return None
    if exit_code == 1:
        # For grep/rg, exit 1 = no match (not an error)
        cmd_base = command.strip().split()[0] if command.strip() else ""
        if cmd_base in ("grep", "rg", "egrep", "fgrep"):
            return "No matches found (not an error for grep)"
        return None
    if exit_code == 2:
        return "Misuse of shell command or syntax error"
    if exit_code == 126:
        return "Permission denied - command found but not executable"
    if exit_code == 127:
        return "Command not found - check spelling or install the tool"
    if exit_code == 128:
        return "Invalid exit argument"
    if 129 <= exit_code <= 192:
        sig = exit_code - 128
        sig_names = {2: "SIGINT (interrupted)", 9: "SIGKILL (killed)", 15: "SIGTERM (terminated)", 11: "SIGSEGV (segfault)"}
        return f"Killed by signal {sig}: {sig_names.get(sig, 'unknown signal')}"
    if exit_code == 255:
        return "SSH connection error or exit status out of range"
    # Git index.lock detection
    if "index.lock" in stderr.lower():
        return "Git lock file exists - another git process may be running, or a previous one crashed. Remove .git/index.lock if safe."
    return None
