"""Sandbox profile: platform-agnostic OS-level isolation requirements."""

from __future__ import annotations


import logging

logger = logging.getLogger(__name__)
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _default_max_processes() -> int:
    try:
        from digitorn.core.config import get_settings
        return get_settings().sandbox.max_processes
    except Exception:
        return 10


@dataclass
class SandboxProfile:
    """OS-level isolation requirements for a single app."""

    app_id: str

    writable_paths: set[str] = field(default_factory=set)
    readable_paths: set[str] = field(default_factory=set)
    fs_unrestricted: bool = False

    allow_exec: bool = False
    allow_fork: bool = False
    max_processes: int = field(default_factory=lambda: _default_max_processes())

    allow_network: bool = False
    allowed_hosts: set[str] = field(default_factory=set)

    memory_limit: int = 0
    cpu_percent: int = 0

    namespaces: set[str] = field(default_factory=set)

    hardening_drop_caps: bool = True
    hardening_no_dumpable: bool = True
    hardening_mdwe: bool = True

    audit_enabled: bool = False

    level: str = "standard"

    @property
    def has_fs_restrictions(self) -> bool:
        return not self.fs_unrestricted

    @property
    def all_allowed_paths(self) -> set[str]:
        return self.writable_paths | self.readable_paths

    def add_system_paths(self) -> None:
        """Add paths the runtime always needs (libs, Python, per-app state)."""
        import sys

        home = Path.home() / ".digitorn"

        # ~/.digitorn is READ-ONLY (jwt.key, server.key, credentials.json)
        self.readable_paths.add(str(home))

        app_state = home / "app_state" / self.app_id
        app_state.mkdir(parents=True, exist_ok=True)
        self.writable_paths.add(str(app_state))

        import platform
        _os = platform.system().lower()

        if _os == "linux":
            for sys_path in (
                "/usr", "/lib", "/lib64", "/bin", "/sbin",
                "/etc",
                "/run/systemd/resolve",
            ):
                if Path(sys_path).exists():
                    self.readable_paths.add(sys_path)
        elif _os == "darwin":
            for sys_path in (
                "/usr", "/Library", "/System",
                "/etc",
                "/private/etc",
                "/private/var/run/resolv.conf",
            ):
                if Path(sys_path).exists():
                    self.readable_paths.add(sys_path)
        elif _os == "windows":
            win_root = os.environ.get("SystemRoot", r"C:\Windows")
            self.readable_paths.add(win_root)

        for path in sys.path:
            if path and Path(path).exists():
                self.readable_paths.add(str(Path(path).resolve()))

        if sys.executable:
            exe_dir = Path(sys.executable).resolve().parent
            self.readable_paths.add(str(exe_dir))
            prefix = Path(sys.prefix).resolve()
            self.readable_paths.add(str(prefix))

        try:
            import ssl as _ssl
            for attr in ("cafile", "openssl_cafile"):
                p = getattr(_ssl.get_default_verify_paths(), attr, None)
                if p and Path(p).exists():
                    self.readable_paths.add(str(Path(p).resolve().parent))
        except Exception as exc:
            logger.debug("profile best-effort block failed: %s", exc)

        pkg = Path(__file__).resolve().parents[3]
        self.readable_paths.add(str(pkg))

    def create_private_tmpdir(self) -> str:
        """Create a private per-session tmpdir and add it to writable paths."""
        import tempfile
        private_tmp = tempfile.mkdtemp(
            prefix=f"digitorn-{self.app_id}-",
        )
        self.writable_paths.add(private_tmp)
        return private_tmp
