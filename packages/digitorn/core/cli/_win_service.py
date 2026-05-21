"""Windows Service host: runs the Digitorn daemon under the SCM via pywin32.

A plain console process cannot be a Windows service - the Service Control
Manager expects the binary to register and report status. This module wraps
the daemon in a real ``ServiceFramework`` so the SCM can start, stop and
auto-restart it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import servicemanager
import win32event
import win32service
import win32serviceutil

SERVICE_NAME = "DigitornDaemon"
SERVICE_DISPLAY = "Digitorn Agent Server"
SERVICE_DESCRIPTION = (
    "Digitorn AI agent server - FastAPI + Socket.IO daemon for agent "
    "application management."
)


def _daemon_command() -> list[str]:
    """Resolve the command that launches the daemon, from this venv.

    Runs under pythonservice.exe, so sys.executable is unreliable; walk up
    from this module to the venv and use its ``digitorn`` entry point.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "Scripts" / "digitorn.exe"
        if candidate.exists():
            return [str(candidate), "start"]
    import shutil

    found = shutil.which("digitorn")
    if found:
        return [found, "start"]
    return [sys.executable, "-m", "digitorn.core.server", "start"]


class DigitornService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY
    _svc_description_ = SERVICE_DESCRIPTION

    def __init__(self, args):
        super().__init__(args)
        self._stop_evt = win32event.CreateEvent(None, 0, 0, None)
        self._proc = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self._stop_evt)
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def SvcDoRun(self):
        import subprocess

        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self._proc = subprocess.Popen(
            _daemon_command(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # Wake every 3s to also exit if the daemon process dies on its own.
        while True:
            if win32event.WaitForSingleObject(self._stop_evt, 3000) == win32event.WAIT_OBJECT_0:
                break
            if self._proc.poll() is not None:
                break
        if self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=20)
            except Exception:
                pass


def _dispatch() -> None:
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(DigitornService)
    servicemanager.StartServiceCtrlDispatcher()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        _dispatch()
    else:
        win32serviceutil.HandleCommandLine(DigitornService)
