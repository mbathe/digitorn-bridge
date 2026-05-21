"""Windows Service backend, driven by pywin32 around DigitornService."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _require_pywin32() -> None:
    try:
        import win32service  # noqa: F401
        import win32serviceutil  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "pywin32 is required for Windows service support.\n"
            "Install it with: pip install pywin32"
        )


def install() -> None:
    """Register Digitorn as a Windows Service (delayed auto-start)."""
    _require_pywin32()
    import sys
    from pathlib import Path

    import win32service
    import win32serviceutil

    from digitorn.core.cli import _win_service as svc

    # Host the service with this venv's python running the script directly,
    # not pythonservice.exe - the latter often fails to load the class in an
    # isolated venv (error 1053).
    script = str(Path(svc.__file__).resolve())
    win32serviceutil.InstallService(
        pythonClassString=f"{svc.__name__}.DigitornService",
        serviceName=svc.SERVICE_NAME,
        displayName=svc.SERVICE_DISPLAY,
        description=svc.SERVICE_DESCRIPTION,
        startType=win32service.SERVICE_AUTO_START,
        delayedstart=True,
        exeName=sys.executable,
        exeArgs=f'"{script}"',
    )
    logger.info("Windows service '%s' installed", svc.SERVICE_NAME)


def uninstall() -> None:
    """Stop (if running) and remove the Digitorn Windows Service."""
    _require_pywin32()
    import win32serviceutil

    from digitorn.core.cli import _win_service as svc

    try:
        stop()
    except Exception:
        pass
    win32serviceutil.RemoveService(svc.SERVICE_NAME)
    logger.info("Windows service '%s' removed", svc.SERVICE_NAME)


def start() -> None:
    """Start the Digitorn Windows Service."""
    _require_pywin32()
    import win32serviceutil

    from digitorn.core.cli import _win_service as svc

    win32serviceutil.StartService(svc.SERVICE_NAME)


def stop() -> None:
    """Stop the Digitorn Windows Service."""
    _require_pywin32()
    import win32serviceutil

    from digitorn.core.cli import _win_service as svc

    win32serviceutil.StopService(svc.SERVICE_NAME)


def status() -> dict[str, str]:
    """Query the Windows Service status."""
    try:
        import win32service
        import win32serviceutil
    except ImportError:
        return {"status": "not_installed"}

    from digitorn.core.cli import _win_service as svc

    try:
        state = win32serviceutil.QueryServiceStatus(svc.SERVICE_NAME)[1]
    except Exception:
        return {"status": "not_installed"}

    mapping = {
        win32service.SERVICE_RUNNING: "running",
        win32service.SERVICE_STOPPED: "stopped",
        win32service.SERVICE_START_PENDING: "starting",
        win32service.SERVICE_STOP_PENDING: "stopping",
    }
    return {"status": mapping.get(state, "unknown")}


def logs() -> str:
    """Retrieve recent service logs from the Windows Event Log."""
    import subprocess

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
