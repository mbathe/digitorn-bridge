"""macOS Service backend - launchd plist management.

Generates and manages a launchd plist for the Digitorn daemon.
Supports user agents (~/Library/LaunchAgents/) and system daemons
(/Library/LaunchDaemons/) depending on privileges.
"""

from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE_LABEL = "dev.digitorn.daemon"
PLIST_FILENAME = f"{SERVICE_LABEL}.plist"


def _is_root() -> bool:
    return os.geteuid() == 0


def _plist_path() -> Path:
    """Return the launchd plist file path."""
    if _is_root():
        return Path("/Library/LaunchDaemons") / PLIST_FILENAME
    return Path.home() / "Library" / "LaunchAgents" / PLIST_FILENAME


def _get_program_args() -> list[str]:
    """Return the ProgramArguments for the plist."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "start", "--no-interactive"]
    digitorn_bin = shutil.which("digitorn")
    if digitorn_bin:
        return [digitorn_bin, "start", "--no-interactive"]
    return [sys.executable, "-m", "digitorn.core.server", "start", "--no-interactive"]


def _log_dir() -> Path:
    """Return the log directory for the daemon."""
    return Path.home() / "Library" / "Logs" / "digitorn"


def _generate_plist() -> dict:
    """Generate the launchd plist as a dictionary."""
    log_path = _log_dir()

    plist = {
        "Label": SERVICE_LABEL,
        "ProgramArguments": _get_program_args(),
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(Path.home() / ".digitorn"),
        "StandardOutPath": str(log_path / "stdout.log"),
        "StandardErrorPath": str(log_path / "stderr.log"),
        "EnvironmentVariables": {
            "DIGITORN_HOST": "127.0.0.1",
            "DIGITORN_PORT": "8000",
        },
        "ThrottleInterval": 5,
    }
    return plist


def install() -> None:
    """Install the launchd plist and load the service."""
    plist_path = _plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # Ensure working and log directories exist
    (Path.home() / ".digitorn").mkdir(parents=True, exist_ok=True)
    _log_dir().mkdir(parents=True, exist_ok=True)

    plist_data = _generate_plist()
    with open(plist_path, "wb") as f:
        plistlib.dump(plist_data, f)

    logger.info("launchd plist installed at %s", plist_path)


def uninstall() -> None:
    """Unload and remove the launchd plist."""
    plist_path = _plist_path()

    # Unload first
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True,
    )

    if plist_path.exists():
        plist_path.unlink()
    else:
        raise RuntimeError(f"Plist not found: {plist_path}")

    logger.info("launchd plist removed")


def start() -> None:
    """Load and start the launchd service."""
    plist_path = _plist_path()
    if not plist_path.exists():
        raise RuntimeError(
            f"Service not installed. Run 'digitorn service install' first."
        )

    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to load service: {result.stderr.strip()}")


def stop() -> None:
    """Unload (stop) the launchd service."""
    plist_path = _plist_path()
    result = subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to unload service: {result.stderr.strip()}")


def status() -> dict[str, str]:
    """Query the launchd service status."""
    result = subprocess.run(
        ["launchctl", "list"],
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        if SERVICE_LABEL in line:
            parts = line.split()
            pid = parts[0] if parts[0] != "-" else None
            if pid:
                return {"status": "running", "pid": pid}
            return {"status": "stopped"}
    return {"status": "not_installed"}


def logs() -> str:
    """Read recent daemon logs."""
    log_file = _log_dir() / "stderr.log"
    if not log_file.exists():
        log_file = _log_dir() / "stdout.log"
    if not log_file.exists():
        return "(No service logs found.)"

    lines = log_file.read_text().splitlines()
    return "\n".join(lines[-50:])
