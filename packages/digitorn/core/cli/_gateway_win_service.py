"""Windows Service host for the gateway-go binary.

Wraps gateway-go.exe under the SCM via pywin32. Binary path and env file
location are read from ``~/.digitorn/gateway_service.json`` at start time.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import servicemanager
import win32event
import win32service
import win32serviceutil

SERVICE_NAME = "DigitornGateway"
SERVICE_DISPLAY = "Digitorn Gateway (Go)"
SERVICE_DESCRIPTION = (
    "Digitorn Gateway - high-performance Go HTTP gateway for LLM provider "
    "routing, JWT auth, credentials, quota and admin APIs."
)

CONFIG_PATH = Path.home() / ".digitorn" / "gateway_service.json"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Gateway service config not found: {CONFIG_PATH}. "
            f"Run `digitorn gateway-service install --binary <path>` first."
        )
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a .env-style file into a plain dict. No interpolation."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


class DigitornGatewayService(win32serviceutil.ServiceFramework):
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

        try:
            cfg = _load_config()
        except Exception as exc:
            servicemanager.LogErrorMsg(f"DigitornGateway: config error: {exc}")
            return

        binary = cfg.get("binary")
        env_file = cfg.get("env_file")
        log_path = cfg.get("log_path")
        extra_env = cfg.get("env", {}) or {}

        if not binary or not Path(binary).exists():
            servicemanager.LogErrorMsg(
                f"DigitornGateway: binary not found at {binary!r}"
            )
            return

        env = dict(os.environ)
        if env_file:
            env.update(_load_env_file(Path(env_file)))
        env.update({str(k): str(v) for k, v in extra_env.items()})

        stdout_target = subprocess.DEVNULL
        if log_path:
            try:
                Path(log_path).parent.mkdir(parents=True, exist_ok=True)
                stdout_target = open(log_path, "ab", buffering=0)
            except Exception as exc:
                servicemanager.LogErrorMsg(
                    f"DigitornGateway: cannot open log {log_path}: {exc}"
                )

        cwd = str(Path(binary).parent)
        try:
            self._proc = subprocess.Popen(
                [binary],
                cwd=cwd,
                env=env,
                stdout=stdout_target,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            servicemanager.LogErrorMsg(f"DigitornGateway: spawn failed: {exc}")
            return

        while True:
            if win32event.WaitForSingleObject(self._stop_evt, 3000) == win32event.WAIT_OBJECT_0:
                break
            if self._proc.poll() is not None:
                servicemanager.LogErrorMsg(
                    f"DigitornGateway: gateway-go exited with code {self._proc.returncode}"
                )
                break

        if self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=20)
            except Exception:
                pass

        if log_path and stdout_target not in (subprocess.DEVNULL, None):
            try:
                stdout_target.close()
            except Exception:
                pass


def _dispatch() -> None:
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(DigitornGatewayService)
    servicemanager.StartServiceCtrlDispatcher()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        _dispatch()
    else:
        win32serviceutil.HandleCommandLine(DigitornGatewayService)
