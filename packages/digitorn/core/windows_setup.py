"""Windows-only setup helpers (Defender exclusions + winloop).

Everything in this module is platform-guarded: every public function is
a safe no-op on non-Windows platforms so the daemon code can call them
unconditionally without if-blocks at the call site.

Two responsibilities:

1. **Defender exclusions** -- on Windows the default Defender / firewall
   path adds 100ms-multiple-seconds of latency to every socket()
   creation, file I/O, and process spawn. Exposing ``python.exe`` and
   ``~/.digitorn`` as Defender exclusions removes that overhead. This
   needs admin (``Add-MpPreference``) so we self-elevate once via UAC.

2. **winloop policy** -- libuv-based event loop for Windows that uses
   non-blocking sockets with overlapped I/O completion via IOCP. Avoids
   the ProactorEventLoop pathology where ``WSASend`` / ``socket()``
   block the main loop synchronously when AV / a slow client gets in
   the way. Installed via pip on the user's interpreter and activated
   by setting the policy before ``uvicorn.Config(...)``.

Used by:
  - ``digitorn windows-setup`` CLI command (interactive one-shot)
  - ``digitorn start`` daemon launcher (passive check + winloop policy)
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)


def is_windows() -> bool:
    return sys.platform == "win32"


# ── Admin detection / self-elevation ─────────────────────────────────


def is_admin() -> bool:
    """True if the current process holds an admin token.

    Always ``False`` on non-Windows -- there is no equivalent concept on
    Linux/macOS in this module's scope (we never run anything that
    requires root on those platforms).
    """
    if not is_windows():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


def relaunch_as_admin(argv: Sequence[str] | None = None) -> None:
    """Re-execute the current process with UAC elevation.

    Fires a UAC prompt; on accept, a new process spawns with admin
    rights and the current (non-admin) process exits cleanly. On
    refusal, ``ShellExecuteW`` returns an error code which we surface.

    No-op on non-Windows.
    """
    if not is_windows():
        return
    import ctypes

    argv = list(argv) if argv is not None else sys.argv
    params = " ".join(f'"{a}"' for a in argv)
    # SW_SHOWNORMAL = 1
    rc = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
        None, "runas", sys.executable, params, None, 1,
    )
    if int(rc) <= 32:
        raise PermissionError(
            f"UAC elevation refused (ShellExecute rc={int(rc)})",
        )
    # Parent exits so only the elevated child remains.
    sys.exit(0)


# ── Defender exclusions ──────────────────────────────────────────────


def _exclusion_targets() -> tuple[list[str], list[str]]:
    """Return ``(process_exclusions, path_exclusions)`` for this install.

    Processes: this Python interpreter (covers both system Python and
    any venv that re-uses the same binary).

    Paths: ``~/.digitorn`` is always included (every install writes
    there); the bridge source root is added only when the current
    interpreter actually runs from inside it (editable install / dev).
    """
    proc = [sys.executable]
    paths = [str(Path.home() / ".digitorn")]
    try:
        # Project root = parent of the package directory. Only add it
        # when we can resolve it reliably; otherwise skip silently.
        pkg_root = Path(__file__).resolve().parents[3]
        if (pkg_root / "pyproject.toml").exists():
            paths.append(str(pkg_root))
    except Exception:
        pass
    return proc, paths


def _ps_quote(s: str) -> str:
    """Single-quote a string for safe inclusion in a PowerShell command."""
    return "'" + s.replace("'", "''") + "'"


def get_current_exclusions() -> tuple[set[str], set[str]]:
    """Read current Defender exclusion lists.

    Returns ``(processes, paths)`` lower-cased for case-insensitive
    comparison. Empty sets on non-Windows or on read failure -- callers
    use that as "we don't know, assume missing".
    """
    if not is_windows():
        return set(), set()
    try:
        # ConvertTo-Json -Compress so we can parse the output as JSON
        # without worrying about PowerShell's table formatting.
        out = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-Command",
                "Get-MpPreference | "
                "Select-Object ExclusionProcess, ExclusionPath | "
                "ConvertTo-Json -Compress",
            ],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if out.returncode != 0:
            return set(), set()
        import json
        data = json.loads(out.stdout or "{}") or {}
        procs = {str(p).lower() for p in (data.get("ExclusionProcess") or [])}
        paths = {str(p).lower() for p in (data.get("ExclusionPath") or [])}
        return procs, paths
    except Exception as exc:
        logger.debug("get_current_exclusions failed: %s", exc)
        return set(), set()


def check_exclusions_present() -> bool:
    """Cheap, no-admin check: are our targets already excluded?

    Always ``True`` on non-Windows (nothing to check). On Windows,
    ``True`` only if every entry in :func:`_exclusion_targets` is found
    in the live Defender exclusion lists.
    """
    if not is_windows():
        return True
    procs, paths = get_current_exclusions()
    want_procs, want_paths = _exclusion_targets()
    procs_ok = all(p.lower() in procs for p in want_procs)
    paths_ok = all(p.lower() in paths for p in want_paths)
    return procs_ok and paths_ok


def install_exclusions() -> dict[str, list[str]]:
    """Add the Defender exclusions. Requires admin on Windows.

    Returns a summary dict ``{"added_processes": [...], "added_paths":
    [...], "skipped": [...]}``. Raises ``PermissionError`` on Windows
    when not admin (caller is expected to handle by relaunching with
    elevation). On non-Windows, returns an empty summary without
    touching anything.
    """
    summary: dict[str, list[str]] = {
        "added_processes": [],
        "added_paths": [],
        "skipped": [],
    }
    if not is_windows():
        return summary
    if not is_admin():
        raise PermissionError("install_exclusions requires admin on Windows")

    current_procs, current_paths = get_current_exclusions()
    want_procs, want_paths = _exclusion_targets()

    cmds: list[str] = []
    for p in want_procs:
        if p.lower() in current_procs:
            summary["skipped"].append(f"process:{p}")
            continue
        cmds.append(f"Add-MpPreference -ExclusionProcess {_ps_quote(p)}")
        summary["added_processes"].append(p)
    for p in want_paths:
        if p.lower() in current_paths:
            summary["skipped"].append(f"path:{p}")
            continue
        cmds.append(f"Add-MpPreference -ExclusionPath {_ps_quote(p)}")
        summary["added_paths"].append(p)

    if not cmds:
        return summary

    script = "; ".join(cmds)
    out = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-Command", script,
        ],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"Add-MpPreference failed (rc={out.returncode}): "
            f"{out.stderr.strip() or out.stdout.strip()}"
        )
    return summary


# ── winloop ──────────────────────────────────────────────────────────


def winloop_available() -> bool:
    """``True`` if ``import winloop`` works in the current interpreter.

    Always ``False`` on non-Windows (winloop is Windows-only by design;
    users on Linux/macOS get uvloop instead, which we don't manage here).
    """
    if not is_windows():
        return False
    try:
        import winloop  # noqa: F401
        return True
    except ImportError:
        return False


def ensure_winloop_installed() -> bool:
    """``pip install winloop`` if not already present.

    Returns ``True`` if winloop is importable after this call. No-op +
    ``False`` on non-Windows.
    """
    if not is_windows():
        return False
    if winloop_available():
        return True
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "winloop"],
            check=True, timeout=120,
        )
    except Exception as exc:
        logger.warning("winloop install failed: %s", exc)
        return False
    return winloop_available()


def install_winloop_policy() -> bool:
    """Activate winloop as the asyncio event loop policy.

    Must be called BEFORE the event loop is created (i.e. before
    ``uvicorn.run``). Returns ``True`` if the policy is now winloop,
    ``False`` otherwise (non-Windows, package missing, or install
    error). Safe no-op on non-Windows.
    """
    if not is_windows():
        return False
    if not winloop_available():
        return False
    try:
        import asyncio
        import winloop
        asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
        return True
    except Exception as exc:
        logger.warning("install_winloop_policy failed: %s", exc)
        return False
