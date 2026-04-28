"""Sandbox profile - OS-level isolation requirements derived from app YAML.

A SandboxProfile is a platform-agnostic description of what an app is
allowed to do at the OS level.  It is built automatically from the
CompiledApp (capabilities + constraints) and translated into native
OS enforcement by the platform backends.

Nothing in this module touches the OS.  It is pure data.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _default_max_processes() -> int:
    """Load max_processes from config, fall back to 10."""
    try:
        from digitorn.core.config import get_settings
        return get_settings().sandbox.max_processes
    except Exception:
        return 10


@dataclass
class SandboxProfile:
    """OS-level isolation requirements for a single app."""

    app_id: str

    # Filesystem - paths the app may write to.
    # Read access is implicitly granted to writable paths.
    writable_paths: set[str] = field(default_factory=set)
    readable_paths: set[str] = field(default_factory=set)
    fs_unrestricted: bool = False

    # Process - can the app spawn subprocesses?
    allow_exec: bool = False
    allow_fork: bool = False
    max_processes: int = field(default_factory=lambda: _default_max_processes())

    # Network - can the app open sockets?
    allow_network: bool = False
    allowed_hosts: set[str] = field(default_factory=set)

    # Resource limits (0 = no limit).
    memory_limit: int = 0
    cpu_percent: int = 0

    # ── Per-session sandbox (warm pool mode) ─────────────────────

    # Namespaces to create: {"user", "pid", "net", "mount"}
    namespaces: set[str] = field(default_factory=set)

    # Process hardening flags
    hardening_drop_caps: bool = True
    hardening_no_dumpable: bool = True
    hardening_mdwe: bool = True

    # Audit via seccomp-notify
    audit_enabled: bool = False

    # Sandbox level: off | standard | strict | maximum
    level: str = "standard"

    @property
    def has_fs_restrictions(self) -> bool:
        return not self.fs_unrestricted

    @property
    def all_allowed_paths(self) -> set[str]:
        return self.writable_paths | self.readable_paths

    def add_system_paths(self) -> None:
        """Add paths the runtime always needs (libs, Python, per-app state).

        Security:
            - ~/.digitorn is READ-ONLY (contains jwt.key, credentials.json)
            - Per-app state goes to ~/.digitorn/app_state/{app_id}/ (writable)
            - /tmp is NOT globally writable - each worker gets a private tmpdir
              via the session_tmpdir field (set during sandbox application)
        """
        import sys

        home = Path.home() / ".digitorn"

        # ~/.digitorn is READ-ONLY (secrets: jwt.key, server.key, credentials.json)
        self.readable_paths.add(str(home))

        # Per-app writable state directory (NOT the secrets dir)
        app_state = home / "app_state" / self.app_id
        app_state.mkdir(parents=True, exist_ok=True)
        self.writable_paths.add(str(app_state))

        # Platform-specific system paths (read-only)
        import platform
        _os = platform.system().lower()

        if _os == "linux":
            for sys_path in (
                "/usr", "/lib", "/lib64", "/bin", "/sbin",
                "/etc",  # DNS nsswitch.conf, ssl certs, hosts, ld.so.cache
                "/run/systemd/resolve",  # systemd-resolved (resolv.conf symlink target)
            ):
                if Path(sys_path).exists():
                    self.readable_paths.add(sys_path)
        elif _os == "darwin":
            for sys_path in (
                "/usr", "/Library", "/System",
                "/etc",  # resolv.conf, hosts
                "/private/etc",  # macOS real /etc location
                "/private/var/run/resolv.conf",  # DNS resolver
            ):
                if Path(sys_path).exists():
                    self.readable_paths.add(sys_path)
        elif _os == "windows":
            # Windows uses Job Objects, not path-based restrictions
            # System root is always accessible
            win_root = os.environ.get("SystemRoot", r"C:\Windows")
            self.readable_paths.add(win_root)

        # Python runtime - stdlib, site-packages, venvs
        for path in sys.path:
            if path and Path(path).exists():
                self.readable_paths.add(str(Path(path).resolve()))

        # Python executable's parent directory and prefix
        if sys.executable:
            exe_dir = Path(sys.executable).resolve().parent
            self.readable_paths.add(str(exe_dir))
            # conda/venv prefix - contains ssl/cert.pem needed for HTTPS
            prefix = Path(sys.prefix).resolve()
            self.readable_paths.add(str(prefix))

        # SSL certificates - needed for any HTTPS connection (LLM APIs, etc.)
        # Different Python installs put certs in different places
        try:
            import ssl as _ssl
            for attr in ("cafile", "openssl_cafile"):
                p = getattr(_ssl.get_default_verify_paths(), attr, None)
                if p and Path(p).exists():
                    self.readable_paths.add(str(Path(p).resolve().parent))
        except Exception:
            pass

        # The digitorn package itself (for imports after sandbox)
        pkg = Path(__file__).resolve().parents[3]  # packages/
        self.readable_paths.add(str(pkg))

    def create_private_tmpdir(self) -> str:
        """Create a private per-session tmpdir and add it to writable paths.

        Returns the path to the private tmpdir. Caller is responsible for
        cleanup (rm -rf) when the session ends.
        """
        import tempfile
        private_tmp = tempfile.mkdtemp(
            prefix=f"digitorn-{self.app_id}-",
        )
        self.writable_paths.add(private_tmp)
        return private_tmp
