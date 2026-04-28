"""Platform-specific adapters for the shell module.

Each adapter encapsulates:
  - The default shell executable
  - Forbidden command patterns specific to the platform
  - Workspace root resolution
  - Shell-specific subprocess invocation
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path


SENSITIVE_ENV_KEYS: list[str] = [
    "key", "secret", "password", "passwd", "token",
    "api", "auth", "credential", "private", "cert",
    "jwt", "signing", "encryption", "ssh", "pgp", "gpg",
]

_MASKED = "***MASKED***"


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
        if requested:
            base = Path(requested).resolve()
        elif workspace:
            base = Path(workspace).resolve()
        else:
            base = Path.cwd()
        # Verify directory exists - fallback to avoid WinError 267 / ENOENT
        if not base.is_dir():
            if workspace and Path(workspace).resolve().is_dir():
                base = Path(workspace).resolve()
            else:
                base = Path.cwd()
        if workspace:
            root = Path(workspace).resolve()
            try:
                base.relative_to(root)
            except ValueError:
                # Outside workspace - fallback to workspace root
                base = root if root.is_dir() else Path.cwd()
        return str(base), None

    async def run_command(
        self,
        command: str,
        cwd: str,
        env: dict[str, str],
        timeout: float,
        max_output_bytes: int,
    ) -> tuple[str, str, int]:
        proc = await asyncio.create_subprocess_shell(
            command,
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
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
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
        # Prefer Git Bash on Windows - like Claude Code.
        # IMPORTANT: Do NOT use shutil.which("bash") - it may return
        # C:\Windows\System32\bash.exe (WSL) which fails if WSL is not installed.
        # Search Git Bash explicitly first.
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
        if requested:
            base = Path(requested).resolve()
        elif workspace:
            base = Path(workspace).resolve()
        else:
            base = Path.cwd()
        # Verify directory exists - fallback to avoid WinError 267 / ENOENT
        if not base.is_dir():
            if workspace and Path(workspace).resolve().is_dir():
                base = Path(workspace).resolve()
            else:
                base = Path.cwd()
        if workspace:
            root = Path(workspace).resolve()
            try:
                base.relative_to(root)
            except ValueError:
                # Outside workspace - fallback to workspace root
                base = root if root.is_dir() else Path.cwd()
        return str(base), None

    async def run_command(
        self,
        command: str,
        cwd: str,
        env: dict[str, str],
        timeout: float,
        max_output_bytes: int,
    ) -> tuple[str, str, int]:
        shell = self.default_shell.lower()
        if "bash" in shell:
            # Bash (Git Bash) - like Claude Code. Handles all POSIX syntax natively.
            proc = await asyncio.create_subprocess_exec(
                self.default_shell,
                "-c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        elif shell.endswith(("powershell.exe", "pwsh.exe", "powershell", "pwsh")):
            proc = await asyncio.create_subprocess_exec(
                self.default_shell,
                "-NonInteractive",
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-Command", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        else:
            cmd = os.environ.get("COMSPEC") or "cmd.exe"
            proc = await asyncio.create_subprocess_exec(
                cmd,
                "/d", "/s", "/c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
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
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ps1", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("Set-StrictMode -Version Latest\n")
            tmp.write("$ErrorActionPreference = 'Stop'\n")
            tmp.write(script)
            tmp_path = tmp.name

        try:
            ps = shutil.which("powershell.exe") or shutil.which("pwsh.exe") or "powershell.exe"
            proc = await asyncio.create_subprocess_exec(
                ps,
                "-NonInteractive",
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
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


def get_adapter() -> PlatformAdapter:
    """Return the correct adapter for the current OS."""
    import platform as _p
    system = _p.system().lower()
    if system == "windows":
        return WindowsAdapter()
    return UnixAdapter()
