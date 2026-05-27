"""Windows Service backend for the gateway-go binary."""

from __future__ import annotations

import json
import logging
from pathlib import Path

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


def _write_config(binary: Path, env_file: Path | None, log_path: Path | None,
                  extra_env: dict[str, str] | None) -> Path:
    from digitorn.core.cli import _gateway_win_service as svc

    cfg = {
        "binary": str(binary.resolve()),
        "env_file": str(env_file.resolve()) if env_file else None,
        "log_path": str(log_path.resolve()) if log_path else None,
        "env": extra_env or {},
    }
    svc.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    svc.CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return svc.CONFIG_PATH


def install(binary: Path, env_file: Path | None = None,
            log_path: Path | None = None,
            extra_env: dict[str, str] | None = None) -> None:
    """Register gateway-go as the DigitornGateway Windows Service."""
    _require_pywin32()
    import sys

    import win32service
    import win32serviceutil

    from digitorn.core.cli import _gateway_win_service as svc

    if not binary.exists():
        raise FileNotFoundError(f"gateway binary not found: {binary}")

    _write_config(binary, env_file, log_path, extra_env)

    script = str(Path(svc.__file__).resolve())
    win32serviceutil.InstallService(
        pythonClassString=f"{svc.__name__}.DigitornGatewayService",
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
    """Stop (if running) and remove the DigitornGateway service."""
    _require_pywin32()
    import win32serviceutil

    from digitorn.core.cli import _gateway_win_service as svc

    try:
        stop()
    except Exception:
        pass
    win32serviceutil.RemoveService(svc.SERVICE_NAME)
    logger.info("Windows service '%s' removed", svc.SERVICE_NAME)


def start() -> None:
    _require_pywin32()
    import win32serviceutil

    from digitorn.core.cli import _gateway_win_service as svc

    win32serviceutil.StartService(svc.SERVICE_NAME)


def stop() -> None:
    _require_pywin32()
    import win32serviceutil

    from digitorn.core.cli import _gateway_win_service as svc

    win32serviceutil.StopService(svc.SERVICE_NAME)


def status() -> dict[str, str]:
    try:
        import win32service
        import win32serviceutil
    except ImportError:
        return {"status": "not_installed"}

    from digitorn.core.cli import _gateway_win_service as svc

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
    import subprocess

    result = subprocess.run(
        [
            "wevtutil", "qe", "Application",
            "/q:*[System[Provider[@Name='DigitornGateway']]]",
            "/c:50", "/f:text", "/rd:true",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        from digitorn.core.cli import _gateway_win_service as svc
        try:
            cfg = json.loads(svc.CONFIG_PATH.read_text(encoding="utf-8"))
            lp = cfg.get("log_path")
            if lp and Path(lp).exists():
                tail = Path(lp).read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
                return "\n".join(tail) or "(log file is empty)"
        except Exception:
            pass
        return "(No service logs found.)"
    return result.stdout
