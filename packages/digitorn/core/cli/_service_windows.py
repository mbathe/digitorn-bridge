"""Windows Service backend — pywin32 ServiceFramework.

Installs Digitorn as a Windows Service that:
  - Starts automatically (Delayed Start) on boot
  - Restarts on failure (3 attempts, 5s delay)
  - Runs the daemon with --no-interactive flag
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE_NAME = "DigitornDaemon"
SERVICE_DISPLAY = "Digitorn Agent Server"
SERVICE_DESCRIPTION = (
    "Digitorn AI agent server — provides FastAPI + Socket.IO daemon "
    "for agent application management."
)


def _get_executable() -> str:
    """Return the command to run the Digitorn daemon."""
    if getattr(sys, "frozen", False):
        # PyInstaller frozen exe
        return sys.executable
    # Normal Python: find the digitorn script
    digitorn_bin = shutil.which("digitorn")
    if digitorn_bin:
        return digitorn_bin
    # Fallback: python -m
    return f'"{sys.executable}" -m digitorn.core.server'


def install() -> None:
    """Install Digitorn as a Windows Service."""
    try:
        import win32service  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "pywin32 is required for Windows service support.\n"
            "Install it with: pip install pywin32"
        )

    exe = _get_executable()

    # Create the service using sc.exe for simplicity and reliability
    cmd = [
        "sc", "create", SERVICE_NAME,
        f"binPath= {exe} start --no-interactive",
        f"DisplayName= {SERVICE_DISPLAY}",
        "start= delayed-auto",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if "exists" in result.stderr.lower():
            raise RuntimeError(f"Service '{SERVICE_NAME}' already exists. Uninstall it first.")
        raise RuntimeError(f"Failed to create service: {result.stderr.strip()}")

    # Set description
    subprocess.run(
        ["sc", "description", SERVICE_NAME, SERVICE_DESCRIPTION],
        capture_output=True,
    )

    # Configure recovery: restart on failure (3 times, 5000ms delay)
    subprocess.run(
        ["sc", "failure", SERVICE_NAME, "reset=", "86400", "actions=", "restart/5000/restart/5000/restart/5000"],
        capture_output=True,
    )

    logger.info("Windows service '%s' installed", SERVICE_NAME)


def uninstall() -> None:
    """Remove the Digitorn Windows Service."""
    # Stop first if running
    subprocess.run(["sc", "stop", SERVICE_NAME], capture_output=True)

    result = subprocess.run(
        ["sc", "delete", SERVICE_NAME],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        if "not exist" in result.stderr.lower() or "1060" in result.stderr:
            raise RuntimeError(f"Service '{SERVICE_NAME}' does not exist.")
        raise RuntimeError(f"Failed to delete service: {result.stderr.strip()}")

    logger.info("Windows service '%s' removed", SERVICE_NAME)


def start() -> None:
    """Start the Digitorn Windows Service."""
    result = subprocess.run(
        ["sc", "start", SERVICE_NAME],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to start service: {result.stderr.strip()}")


def stop() -> None:
    """Stop the Digitorn Windows Service."""
    result = subprocess.run(
        ["sc", "stop", SERVICE_NAME],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        if "not been started" in result.stderr.lower() or "1062" in result.stderr:
            return  # Already stopped
        raise RuntimeError(f"Failed to stop service: {result.stderr.strip()}")


def status() -> dict[str, str]:
    """Query the Windows Service status."""
    result = subprocess.run(
        ["sc", "query", SERVICE_NAME],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {"status": "not_installed"}

    output = result.stdout
    if "RUNNING" in output:
        return {"status": "running"}
    elif "STOPPED" in output:
        return {"status": "stopped"}
    elif "START_PENDING" in output:
        return {"status": "starting"}
    elif "STOP_PENDING" in output:
        return {"status": "stopping"}
    return {"status": "unknown"}


def logs() -> str:
    """Retrieve recent service logs from Windows Event Log."""
    result = subprocess.run(
        [
            "wevtutil", "qe", "Application",
            "/q:*[System[Provider[@Name='DigitornDaemon']]]",
            "/c:50", "/f:text", "/rd:true",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return "(No service logs found. Check daemon logs in ~/.digitorn/)"
    return result.stdout
