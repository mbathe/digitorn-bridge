"""Linux Service backend - systemd unit management."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE_NAME = "digitorn"
UNIT_FILENAME = f"{SERVICE_NAME}.service"


def _is_root() -> bool:
    return os.geteuid() == 0


def _unit_path() -> Path:
    """Return the systemd unit file path (user or system)."""
    if _is_root():
        return Path("/etc/systemd/system") / UNIT_FILENAME
    return Path.home() / ".config" / "systemd" / "user" / UNIT_FILENAME


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    """Run systemctl with user/system flag."""
    cmd = ["systemctl"]
    if not _is_root():
        cmd.append("--user")
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def _get_exec_start() -> str:
    """Return the ExecStart command for the unit file."""
    if getattr(sys, "frozen", False):
        return f"{sys.executable} start"
    digitorn_bin = shutil.which("digitorn")
    if digitorn_bin:
        return f"{digitorn_bin} start"
    return f"{sys.executable} -m digitorn.core.server start"


def _generate_unit() -> str:
    """Generate the systemd unit file content."""
    exec_start = _get_exec_start()
    home = Path.home()
    working_dir = home / ".digitorn"

    return f"""\
[Unit]
Description=Digitorn Agent Server
Documentation=https://docs.digitorn.dev
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
ExecStart={exec_start}
WorkingDirectory={working_dir}
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

# Environment
Environment=DIGITORN_HOST=127.0.0.1
Environment=DIGITORN_PORT=8000

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={home}/.digitorn

[Install]
WantedBy={"multi-user.target" if _is_root() else "default.target"}
"""


def install() -> None:
    """Install the systemd unit file and enable the service."""
    unit_path = _unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)

    # Ensure working directory exists
    work_dir = Path.home() / ".digitorn"
    work_dir.mkdir(parents=True, exist_ok=True)

    unit_content = _generate_unit()
    unit_path.write_text(unit_content)

    _systemctl("daemon-reload")
    _systemctl("enable", SERVICE_NAME)

    logger.info("systemd unit installed at %s", unit_path)


def uninstall() -> None:
    """Disable and remove the systemd unit file."""
    _systemctl("stop", SERVICE_NAME)
    _systemctl("disable", SERVICE_NAME)

    unit_path = _unit_path()
    if unit_path.exists():
        unit_path.unlink()
    else:
        raise RuntimeError(f"Unit file not found: {unit_path}")

    _systemctl("daemon-reload")
    logger.info("systemd unit removed")


def start() -> None:
    """Start the Digitorn service."""
    result = _systemctl("start", SERVICE_NAME)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to start service: {result.stderr.strip()}")


def stop() -> None:
    """Stop the Digitorn service."""
    result = _systemctl("stop", SERVICE_NAME)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to stop service: {result.stderr.strip()}")


def status() -> dict[str, str]:
    """Query the systemd service status."""
    result = _systemctl("is-active", SERVICE_NAME)
    state = result.stdout.strip()

    status_map = {
        "active": "running",
        "inactive": "stopped",
        "failed": "failed",
        "activating": "starting",
        "deactivating": "stopping",
    }
    return {"status": status_map.get(state, "not_installed")}


def logs() -> str:
    """Retrieve recent service logs via journalctl."""
    cmd = ["journalctl"]
    if not _is_root():
        cmd.append("--user")
    cmd.extend(["-u", SERVICE_NAME, "-n", "50", "--no-pager"])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return "(No service logs found.)"
    return result.stdout
