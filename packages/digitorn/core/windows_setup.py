"""Windows-only setup helpers (Defender exclusions + winloop)."""
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


def is_admin() -> bool:
    """True if the current process holds an admin token."""
    if not is_windows():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


def relaunch_as_admin(argv: Sequence[str] | None = None) -> None:
    """Re-execute the current process with UAC elevation."""
    if not is_windows():
        return
    import ctypes

    argv = list(argv) if argv is not None else sys.argv
    params = " ".join(f'"{a}"' for a in argv)
    rc = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
        None, "runas", sys.executable, params, None, 1,
    )
    if int(rc) <= 32:
        raise PermissionError(
            f"UAC elevation refused (ShellExecute rc={int(rc)})",
        )
    sys.exit(0)


def _exclusion_targets() -> tuple[list[str], list[str]]:
    proc = [sys.executable]
    paths = [str(Path.home() / ".digitorn")]
    try:
        pkg_root = Path(__file__).resolve().parents[3]
        if (pkg_root / "pyproject.toml").exists():
            paths.append(str(pkg_root))
    except Exception as exc:
        logger.debug("windows_setup best-effort block failed: %s", exc)
    return proc, paths


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def get_current_exclusions() -> tuple[set[str], set[str]]:
    """Read current Defender exclusion lists."""
    if not is_windows():
        return set(), set()
    try:
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
    """True if every exclusion target is already excluded."""
    if not is_windows():
        return True
    procs, paths = get_current_exclusions()
    want_procs, want_paths = _exclusion_targets()
    procs_ok = all(p.lower() in procs for p in want_procs)
    paths_ok = all(p.lower() in paths for p in want_paths)
    return procs_ok and paths_ok


def install_exclusions() -> dict[str, list[str]]:
    """Add the Defender exclusions. Requires admin on Windows."""
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


def winloop_available() -> bool:
    """True if `import winloop` works in the current interpreter."""
    if not is_windows():
        return False
    try:
        import winloop  # noqa: F401
        return True
    except ImportError:
        return False


def ensure_winloop_installed() -> bool:
    """`pip install winloop` if not already present."""
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
    """Activate winloop as the asyncio event loop policy (call before loop creation)."""
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
