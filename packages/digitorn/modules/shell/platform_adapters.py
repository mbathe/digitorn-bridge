"""Platform-specific adapters for the shell module."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

SENSITIVE_ENV_KEYS: list[str] = [
    "key", "secret", "password", "passwd", "token",
    "api", "auth", "credential", "private", "cert",
    "jwt", "signing", "encryption", "ssh", "pgp", "gpg",
]

_MASKED = "***MASKED***"

# Environment variables forced into every shell command so that any
# tooling the agent calls runs in NON-INTERACTIVE mode by default.
#
# Why: agents run in a non-TTY shell. Any CLI that prompts on stdin
# (npx "Ok to proceed?", apt "Continue? [Y/n]", git password prompt,
# etc.) hangs forever waiting for input that never comes. Combined
# with stdin=DEVNULL on the subprocess this guarantees zero hangs
# from interactive prompts across npm, yarn, pnpm, git, apt, debconf,
# pip, brew, and any tool that reads `CI=true` or `NO_COLOR`.
#
# These are SETDEFAULT - the agent / app YAML can still override.
_NON_INTERACTIVE_ENV: dict[str, str] = {
    # Universal CI markers (most tools detect this and switch to
    # non-interactive mode automatically)
    "CI": "true",                               # npm, yarn, pnpm, GitHub Actions
    "CONTINUOUS_INTEGRATION": "true",           # alternate spelling some tools use
    "BUILDKITE": "true",                        # buildkite agent runner pattern
    "DEBIAN_FRONTEND": "noninteractive",        # apt
    "DEBCONF_NONINTERACTIVE_SEEN": "true",      # debconf back-end
    # Editor fallbacks: `true` is a no-op so editor-spawning tools
    # (git, crontab, visudo) fail fast instead of hanging.
    "EDITOR": "true",
    "VISUAL": "true",
    "GIT_EDITOR": "true",
    # Pager defaults: `cat` avoids paging for `git log` / `man`.
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "MANPAGER": "cat",
    # NPM family
    "npm_config_yes": "true",                   # npm/npx auto-accept prompts
    "npm_config_audit": "false",                # silence audit prompts
    "npm_config_fund": "false",                 # silence funding prompts
    "npm_config_progress": "false",             # tidier logs
    "npm_config_loglevel": "warn",              # less spam in stdout
    "YARN_ENABLE_TELEMETRY": "0",               # yarn berry
    # Git
    "GIT_TERMINAL_PROMPT": "0",                 # git refuse password prompts
    "GIT_ASKPASS": "true",                      # bypass GUI askpass helper
    "SSH_ASKPASS": "true",                      # bypass GUI askpass on ssh
    # Color / TTY signals - some tools embed ANSI even when stdout
    # is a pipe; force off for clean log capture
    "NO_COLOR": "1",
    "FORCE_COLOR": "0",
    "CLICOLOR": "0",
    "CLICOLOR_FORCE": "0",
    # Python
    "PYTHONUNBUFFERED": "1",                    # prints flushed live
    "PYTHONDONTWRITEBYTECODE": "1",             # don't pollute __pycache__
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",       # silence pip upgrade nag
    "PIP_NO_INPUT": "1",                        # pip refuse prompts
    # Homebrew
    "HOMEBREW_NO_AUTO_UPDATE": "1",
    "HOMEBREW_NO_INSTALL_CLEANUP": "1",
    "HOMEBREW_NO_ENV_HINTS": "1",
    # AWS / cloud CLIs - refuse interactive paging
    "AWS_PAGER": "",
    # Cargo / rustup - non-interactive defaults
    "RUSTUP_INIT_SKIP_PATH_CHECK": "yes",
    "CARGO_TERM_PROGRESS_WHEN": "never",
    "CARGO_TERM_COLOR": "never",
    # Snap / flatpak / dnf
    "DNF_AUTO_INSTALL": "1",
    # PostgreSQL / MySQL - pass connection info via env, not prompts
    "PGCONNECT_TIMEOUT": "10",                  # bound psql probes
}

def inject_non_interactive_env(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of `env` with non-interactive defaults applied."""
    out = dict(env)
    for k, v in _NON_INTERACTIVE_ENV.items():
        out.setdefault(k, v)
    return out

def mask_env(variables: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for k, v in variables.items():
        if any(f in k.lower() for f in SENSITIVE_ENV_KEYS):
            result[k] = _MASKED
        else:
            result[k] = v
    return result

def truncate_output(raw: bytes, max_bytes: int) -> str:
    decoded = raw.decode(errors="replace")
    if len(raw) > max_bytes:
        decoded = decoded[:max_bytes]
        decoded += f"\n... [output truncated at {max_bytes} bytes]"
    return decoded

class PlatformAdapter(ABC):
    """Abstract base for platform-specific shell behaviour."""

    # Universal forbidden patterns - checked on ALL platforms.
    # These block destructive commands regardless of OS, because agents
    # may emit Unix commands even on Windows (e.g. via Git Bash / WSL).
    _UNIVERSAL_FORBIDDEN: list[str] = [
        "rm -rf /",
        "rm -rf ~",
        "rm -rf /*",
        "mkfs",
        "dd if=/dev/",
        "dd of=/dev/",
        "> /dev/sda",
        "> /dev/nvme",
        ":(){ :|:& };:",
        "chmod -R 777 /",
        "shutdown",
        "reboot",
        "halt",
        "init 0",
        "init 6",
        "curl | bash",
        "curl | sh",
        "wget | bash",
        "wget | sh",
        "wget -o- | bash",
        "wget -o- | sh",
    ]

    # Infinite-wait patterns rejected early with a fix hint.
    # Each entry: (compiled regex, hint string injected into the error).
    _INFINITE_WAIT_PATTERNS: list[tuple[Any, str]] = [
        (
            re.compile(r"\btail\s+(-[A-Za-z]*f[A-Za-z]*\s+)?/dev/null\b"),
            "tail -f /dev/null follows a sink that never gets bytes - "
            "use `until <check-condition>; do sleep 0.5; done` instead, "
            "or `Bash(task_id='X', status=true)` to poll a running task "
            "non-blockingly.",
        ),
        (
            re.compile(r"\bsleep\s+(infinity|inf)\b"),
            "sleep infinity has no termination - use a bounded sleep "
            "(`sleep 5`) or wrap a check in `until ...; do sleep N; done`.",
        ),
        (
            re.compile(r"\bsleep\s+\d{5,}\b"),
            "sleep > 10000s is almost certainly a mistake - if you "
            "really need to wait that long, schedule a task instead.",
        ),
        (
            re.compile(r"\bcat\s+/dev/zero\b"),
            "cat /dev/zero pumps zeros forever and has no exit - "
            "if you wanted to wait, use `until <check>; do sleep N; done`.",
        ),
        (
            re.compile(r"\byes\b\s*(\|\s*\w+\s*)?>\s*/dev/null\b"),
            "yes > /dev/null is an infinite output pump with no exit "
            "- not a valid wait primitive.",
        ),
        (
            re.compile(r"\bwhile\s+(true|:)\s*;\s*do\s*(:|sleep\s+\d+)?\s*;?\s*done\b"),
            "while true; do ... done has no exit condition - use "
            "`until <check>; do sleep N; done` (exits when the check "
            "passes) or add a `break` clause.",
        ),
    ]

    @property
    @abstractmethod
    def platform_name(self) -> str: ...

    @property
    @abstractmethod
    def default_shell(self) -> str: ...

    @property
    @abstractmethod
    def forbidden_patterns(self) -> list[str]: ...

    @abstractmethod
    def workspace_root(self) -> str: ...

    @abstractmethod
    def resolve_cwd(self, requested: str | None, workspace: str | None) -> tuple[str, str | None]:
        """Return (resolved_cwd, error_message)."""
        ...

    def is_forbidden(self, command: str) -> str | None:
        lower = command.lower()
        for pattern in self._UNIVERSAL_FORBIDDEN:
            if pattern in lower:
                return pattern
        for pattern in self.forbidden_patterns:
            if pattern in lower:
                return pattern
        return None

    def infinite_wait_hint(self, command: str) -> str | None:
        """Return an actionable error message if `command` matches."""
        for regex, hint in self._INFINITE_WAIT_PATTERNS:
            if regex.search(command):
                return hint
        return None

    @abstractmethod
    async def run_command(
        self,
        command: str,
        cwd: str,
        env: dict[str, str],
        timeout: float,
        max_output_bytes: int,
    ) -> tuple[str, str, int]: ...
    """Returns (stdout, stderr, exit_code)."""

    @abstractmethod
    async def run_script_block(
        self,
        script: str,
        cwd: str,
        timeout: float,
        max_output_bytes: int,
    ) -> tuple[str, str, int]: ...
    """Execute a multi-line script block. Returns (stdout, stderr, exit_code)."""

class UnixAdapter(PlatformAdapter):

    @property
    def platform_name(self) -> str:
        return "unix"

    @property
    def default_shell(self) -> str:
        for candidate in ("/bin/bash", "/usr/bin/bash", "/bin/sh"):
            if Path(candidate).exists():
                return candidate
        return "/bin/sh"

    @property
    def forbidden_patterns(self) -> list[str]:
        # Platform-specific patterns (universal patterns are in the base class)
        return [
            "chown -R",
            "/etc/passwd",
            "/etc/shadow",
            "base64 -d",
        ]

    def workspace_root(self) -> str:
        return os.environ.get("PWD", os.getcwd())

    def resolve_cwd(self, requested: str | None, workspace: str | None) -> tuple[str, str | None]:
        # Never silently fall back to `Path.cwd()` (= the daemon's
        # source tree); refuse the command instead.
        if requested:
            base = Path(requested).resolve()
        elif workspace:
            base = Path(workspace).resolve()
        else:
            return "", (
                "No workspace resolved for this session. Cannot run "
                "shell commands without a workspace anchor. Make sure "
                "the session has a workdir set or the app declares a "
                "workspace_mode."
            )
        if not base.is_dir():
            if workspace and Path(workspace).resolve().is_dir():
                base = Path(workspace).resolve()
            else:
                return "", (
                    f"Resolved cwd '{base}' is not a directory and the "
                    f"workspace cannot serve as a fallback. Aborting."
                )
        # Clamp: requested must sit inside the workspace, otherwise
        # snap back to the workspace root (no daemon-cwd fallback).
        if workspace:
            root = Path(workspace).resolve()
            try:
                base.relative_to(root)
            except ValueError:
                if not root.is_dir():
                    return "", (
                        f"Workspace root '{root}' does not exist on "
                        f"disk and 'requested' was outside it. Aborting."
                    )
                base = root
        return str(base), None

    async def run_command(
        self,
        command: str,
        cwd: str,
        env: dict[str, str],
        timeout: float,
        max_output_bytes: int,
    ) -> tuple[str, str, int]:
        env = inject_non_interactive_env(env)
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            executable=self.default_shell,
        )
        stdout_raw, stderr_raw = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        return (
            truncate_output(stdout_raw, max_output_bytes),
            truncate_output(stderr_raw, max_output_bytes),
            proc.returncode,
        )

    async def run_script_block(
        self,
        script: str,
        cwd: str,
        timeout: float,
        max_output_bytes: int,
    ) -> tuple[str, str, int]:
        fd = tempfile.mkstemp(suffix=".sh")
        tmp_path = fd[1]
        os.close(fd[0])
        os.chmod(tmp_path, 0o700)
        Path(tmp_path).write_text(
            "#!/bin/bash\nset -euo pipefail\n" + script,
            encoding="utf-8",
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                self.default_shell, tmp_path,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=inject_non_interactive_env(dict(os.environ)),
            )
            stdout_raw, stderr_raw = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return (
                truncate_output(stdout_raw, max_output_bytes),
                truncate_output(stderr_raw, max_output_bytes),
                proc.returncode,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

class WindowsAdapter(PlatformAdapter):

    @property
    def platform_name(self) -> str:
        return "windows"

    @property
    def default_shell(self) -> str:
        # Prefer Git Bash; `shutil.which("bash")` would return WSL.
        for candidate in [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ]:
            if Path(candidate).exists():
                return candidate
        # Fallback to PowerShell/cmd
        ps = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        return ps or "cmd.exe"

    @property
    def forbidden_patterns(self) -> list[str]:
        return [
            "format c:",
            "format d:",
            "del /f /s /q c:\\",
            "rd /s /q c:\\",
            "rmdir /s /q c:\\",
            "reg delete hklm",
            "reg delete hkcu",
            "bcdedit",
            "diskpart",
            "cipher /w:c",
            "sfc /scannow",
            "netsh firewall",
            "net user administrator",
            "shutdown /r",
            "shutdown /s",
            "taskkill /f /im",
            "powershell -encodedcommand",
            "iex(",
            "invoke-expression",
            "downloadstring",
        ]

    def workspace_root(self) -> str:
        return os.environ.get("CD", os.getcwd())

    def resolve_cwd(self, requested: str | None, workspace: str | None) -> tuple[str, str | None]:
        # Never silently fall back to `Path.cwd()` (= the daemon's
        # source tree); refuse the command instead.
        if requested:
            base = Path(requested).resolve()
        elif workspace:
            base = Path(workspace).resolve()
        else:
            return "", (
                "No workspace resolved for this session. Cannot run "
                "shell commands without a workspace anchor. Make sure "
                "the session has a workdir set or the app declares a "
                "workspace_mode."
            )
        if not base.is_dir():
            if workspace and Path(workspace).resolve().is_dir():
                base = Path(workspace).resolve()
            else:
                return "", (
                    f"Resolved cwd '{base}' is not a directory and the "
                    f"workspace cannot serve as a fallback. Aborting."
                )
        # Clamp: requested must sit inside the workspace, otherwise
        # snap back to the workspace root (no daemon-cwd fallback).
        if workspace:
            root = Path(workspace).resolve()
            try:
                base.relative_to(root)
            except ValueError:
                if not root.is_dir():
                    return "", (
                        f"Workspace root '{root}' does not exist on "
                        f"disk and 'requested' was outside it. Aborting."
                    )
                base = root
        return str(base), None

    async def run_command(
        self,
        command: str,
        cwd: str,
        env: dict[str, str],
        timeout: float,
        max_output_bytes: int,
    ) -> tuple[str, str, int]:
        env = inject_non_interactive_env(env)
        shell = self.default_shell.lower()
        if "bash" in shell:
            argv = [self.default_shell, "-c", command]
        elif shell.endswith(("powershell.exe", "pwsh.exe", "powershell", "pwsh")):
            argv = [
                self.default_shell,
                "-NonInteractive",
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-Command", command,
            ]
        else:
            cmd = os.environ.get("COMSPEC") or "cmd.exe"
            argv = [cmd, "/d", "/s", "/c", command]

        # Falls back to a thread-pool `subprocess.run` on a
        # SelectorEventLoop where `create_subprocess_exec` raises
        # `NotImplementedError` with an empty message.
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            stdout_raw, stderr_raw = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
            return (
                truncate_output(stdout_raw, max_output_bytes),
                truncate_output(stderr_raw, max_output_bytes),
                proc.returncode,
            )
        except NotImplementedError:
            import subprocess as _subprocess
            def _sync_run() -> tuple[bytes, bytes, int]:
                p = _subprocess.Popen(
                    argv,
                    stdin=_subprocess.DEVNULL,
                    stdout=_subprocess.PIPE,
                    stderr=_subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
                try:
                    out, err = p.communicate(timeout=timeout)
                except _subprocess.TimeoutExpired:
                    p.kill()
                    raise asyncio.TimeoutError() from None
                return out, err, p.returncode

            stdout_raw, stderr_raw, rc = await asyncio.to_thread(_sync_run)
            return (
                truncate_output(stdout_raw, max_output_bytes),
                truncate_output(stderr_raw, max_output_bytes),
                rc,
            )

    async def run_script_block(
        self,
        script: str,
        cwd: str,
        timeout: float,
        max_output_bytes: int,
    ) -> tuple[str, str, int]:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ps1", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("Set-StrictMode -Version Latest\n")
            tmp.write("$ErrorActionPreference = 'Stop'\n")
            tmp.write(script)
            tmp_path = tmp.name

        try:
            ps = shutil.which("powershell.exe") or shutil.which("pwsh.exe") or "powershell.exe"
            argv = [
                ps,
                "-NonInteractive",
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", tmp_path,
            ]
            # Same async / thread fallback as `run_command`.
            env = inject_non_interactive_env(dict(os.environ))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
                stdout_raw, stderr_raw = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout,
                )
                return (
                    truncate_output(stdout_raw, max_output_bytes),
                    truncate_output(stderr_raw, max_output_bytes),
                    proc.returncode,
                )
            except NotImplementedError:
                import subprocess as _subprocess
                def _sync_run() -> tuple[bytes, bytes, int]:
                    p = _subprocess.Popen(
                        argv,
                        stdin=_subprocess.DEVNULL,
                        stdout=_subprocess.PIPE,
                        stderr=_subprocess.PIPE,
                        cwd=cwd,
                        env=env,
                    )
                    try:
                        out, err = p.communicate(timeout=timeout)
                    except _subprocess.TimeoutExpired:
                        p.kill()
                        raise asyncio.TimeoutError() from None
                    return out, err, p.returncode

                stdout_raw, stderr_raw, rc = await asyncio.to_thread(_sync_run)
                return (
                    truncate_output(stdout_raw, max_output_bytes),
                    truncate_output(stderr_raw, max_output_bytes),
                    rc,
                )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

def get_adapter() -> PlatformAdapter:
    """Return the correct adapter for the current OS."""
    import platform as _p
    system = _p.system().lower()
    if system == "windows":
        return WindowsAdapter()
    return UnixAdapter()
