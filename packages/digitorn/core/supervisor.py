"""External watchdog / supervisor for the Digitorn daemon."""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as _urlerr
from urllib import request as _urlreq

logger = logging.getLogger("digitorn.supervisor")

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_HEALTH_INTERVAL_S = 10.0
_HEALTH_TIMEOUT_S = 5.0
_BOOT_GRACE_S = 180.0
_MAX_FAIL_PROBES = 5
_MAX_DEGRADED_PROBES = 6
_RESTART_BACKOFF = [2.0, 5.0, 15.0, 30.0, 60.0]
_STDERR_RING = 200
_INCIDENTS_DIR = Path.home() / ".digitorn" / "incidents"


def _setup_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [supervisor] %(levelname)s %(message)s",
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _probe_health(host: str, port: int) -> dict[str, Any] | None:
    """Hit /healthz and return the parsed body, or None on failure."""
    url = f"http://{host}:{port}/healthz"
    try:
        with _urlreq.urlopen(url, timeout=_HEALTH_TIMEOUT_S) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (_urlerr.URLError, OSError, json.JSONDecodeError):
        return None


def _write_incident(
    reason: str,
    *,
    health: dict[str, Any] | None,
    stderr_tail: list[str],
    exit_code: int | None,
    pid: int | None,
) -> Path:
    """Persist a JSON incident report to ~/.digitorn/incidents/."""
    _INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_reason = "".join(c if c.isalnum() else "_" for c in reason)[:40]
    path = _INCIDENTS_DIR / f"{ts}_{safe_reason}.json"
    payload = {
        "ts": ts,
        "reason": reason,
        "exit_code": exit_code,
        "pid": pid,
        "last_health": health,
        "stderr_tail": stderr_tail,
    }
    try:
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("incident_write_failed path=%s err=%s", path, exc)
    return path


class Supervisor:
    """Spawn-and-watch loop for `digitorn start`."""

    def __init__(
        self,
        *,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        daemon_args: list[str] | None = None,
        max_failures: int = _MAX_FAIL_PROBES,
        max_degraded: int = _MAX_DEGRADED_PROBES,
        memory_cap_mb: float | None = 4096.0,
        max_restarts_per_hour: int = 10,
    ) -> None:
        self.host = host
        self.port = port
        self.daemon_args = daemon_args or []
        self.max_failures = max_failures
        self.max_degraded = max_degraded
        self.memory_cap_mb = memory_cap_mb
        self.max_restarts_per_hour = max_restarts_per_hour
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr_ring: deque[str] = deque(maxlen=_STDERR_RING)
        self._restart_count = 0
        self._restart_history: deque[float] = deque(maxlen=64)
        self._stop_requested = False
        self._stalls_baseline = 0

    def _spawn(self) -> subprocess.Popen[bytes]:
        digitorn = shutil.which("digitorn")
        if digitorn:
            cmd = [digitorn, "start", "--host", self.host, "--port", str(self.port)]
        else:
            cmd = [
                sys.executable, "-m", "digitorn.core.server", "start",
                "--host", self.host, "--port", str(self.port),
            ]
        cmd.extend(self.daemon_args)
        logger.info("spawning daemon: %s", " ".join(cmd))

        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=False,
        )
        import threading
        def _drain() -> None:
            assert proc.stdout is not None
            for raw in proc.stdout:
                try:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                except Exception:
                    continue
                self._stderr_ring.append(line)
                print(line, file=sys.stderr, flush=True)
        t = threading.Thread(target=_drain, daemon=True, name="sup-drain")
        t.start()
        return proc

    def _kill(self, sig: int = signal.SIGTERM) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                # POSIX signals unavailable on Windows subprocesses; Job Object cleans children
                self._proc.terminate()
            else:
                self._proc.send_signal(sig)
        except Exception as exc:
            logger.warning("kill failed: %s", exc)

    def _wait_exit(self, timeout: float) -> int | None:
        if self._proc is None:
            return None
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def _should_throttle_restart(self) -> bool:
        now = time.monotonic()
        self._restart_history = deque(
            (t for t in self._restart_history if t > now - 3600),
            maxlen=64,
        )
        return len(self._restart_history) >= self.max_restarts_per_hour

    def _backoff_for_restart(self) -> float:
        idx = min(self._restart_count, len(_RESTART_BACKOFF) - 1)
        return _RESTART_BACKOFF[idx]

    def _restart(self, reason: str, health: dict[str, Any] | None) -> None:
        if self._should_throttle_restart():
            logger.error(
                "restart_throttled too_many_restarts_in_last_hour=%d - "
                "supervisor exiting",
                len(self._restart_history),
            )
            self._stop_requested = True
            return
        pid = self._proc.pid if self._proc else None
        exit_code = self._proc.poll() if self._proc else None
        if exit_code is None and self._proc is not None:
            self._kill()
            exit_code = self._wait_exit(timeout=10.0)
            if exit_code is None:
                logger.warning("daemon did not exit gracefully - sending SIGKILL")
                try:
                    self._proc.kill()
                except Exception as exc:
                    logger.debug("supervisor best-effort block failed: %s", exc)
                exit_code = self._wait_exit(timeout=5.0)
        path = _write_incident(
            reason,
            health=health,
            stderr_tail=list(self._stderr_ring),
            exit_code=exit_code,
            pid=pid,
        )
        delay = self._backoff_for_restart()
        logger.warning(
            "restart reason=%s exit_code=%s incident=%s backoff_s=%.0f",
            reason, exit_code, path.name, delay,
        )
        self._restart_count += 1
        self._restart_history.append(time.monotonic())
        time.sleep(delay)
        self._stderr_ring.clear()
        self._stalls_baseline = 0
        self._proc = self._spawn()

    def _install_signal_handlers(self) -> None:
        def _handle(signum: int, _frame: Any) -> None:
            logger.info("signal received signum=%d - graceful shutdown", signum)
            self._stop_requested = True
        signal.signal(signal.SIGINT, _handle)
        if hasattr(signal, "SIGTERM"):
            try:
                signal.signal(signal.SIGTERM, _handle)
            except (ValueError, OSError):
                pass

    def run(self) -> int:
        _setup_logging()
        self._install_signal_handlers()
        logger.info(
            "supervisor_starting host=%s port=%d max_failures=%d "
            "max_degraded=%d memory_cap_mb=%s",
            self.host, self.port,
            self.max_failures, self.max_degraded, self.memory_cap_mb,
        )

        self._proc = self._spawn()
        proc_started_at = time.monotonic()
        consecutive_failures = 0
        consecutive_degraded = 0

        try:
            while not self._stop_requested:
                time.sleep(_HEALTH_INTERVAL_S)

                rc = self._proc.poll()
                if rc is not None:
                    self._restart(
                        reason=f"process_exited_code_{rc}",
                        health=None,
                    )
                    proc_started_at = time.monotonic()
                    consecutive_failures = 0
                    consecutive_degraded = 0
                    continue

                health = _probe_health(self.host, self.port)
                if health is None:
                    consecutive_failures += 1
                    if consecutive_failures >= self.max_failures:
                        self._restart(
                            reason="health_probe_unresponsive",
                            health=None,
                        )
                        proc_started_at = time.monotonic()
                        consecutive_failures = 0
                        consecutive_degraded = 0
                    continue
                consecutive_failures = 0

                status = str(health.get("status", "unknown"))
                stalls = int(health.get("loop_stalls_total", 0))

                in_boot_grace = (
                    time.monotonic() - proc_started_at < _BOOT_GRACE_S
                )

                if status == "degraded" and not in_boot_grace:
                    consecutive_degraded += 1
                    if consecutive_degraded >= self.max_degraded:
                        self._restart(
                            reason="health_degraded_persistent",
                            health=health,
                        )
                        proc_started_at = time.monotonic()
                        consecutive_failures = 0
                        consecutive_degraded = 0
                    continue
                else:
                    consecutive_degraded = 0

                if not in_boot_grace:
                    delta_stalls = stalls - self._stalls_baseline
                    if delta_stalls >= 3:
                        self._restart(
                            reason="event_loop_stalls_repeated",
                            health=health,
                        )
                        proc_started_at = time.monotonic()
                        continue
                self._stalls_baseline = stalls

                if self.memory_cap_mb is not None:
                    rss_mb = self._proc_memory_mb()
                    if rss_mb is not None and rss_mb > self.memory_cap_mb:
                        self._restart(
                            reason=f"memory_exceeded_{int(rss_mb)}mb",
                            health=health,
                        )
                        proc_started_at = time.monotonic()
                        continue

                if status == "alive" and not in_boot_grace:
                    self._restart_count = max(0, self._restart_count - 1)

        finally:
            logger.info("supervisor_stopping - terminating daemon")
            self._kill()
            self._wait_exit(timeout=30.0)

        logger.info("supervisor_stopped")
        return 0

    def _proc_memory_mb(self) -> float | None:
        if self._proc is None:
            return None
        try:
            import psutil
            p = psutil.Process(self._proc.pid)
            return round(p.memory_info().rss / 1024 / 1024, 1)
        except Exception:
            return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="digitorn-supervisor",
        description="Watchdog wrapper around `digitorn start`.",
    )
    parser.add_argument("--host", default=_DEFAULT_HOST,
                        help="Daemon host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT,
                        help="Daemon port (default 8000)")
    parser.add_argument("--max-failures", type=int, default=_MAX_FAIL_PROBES,
                        help="Restart after N consecutive failed health probes")
    parser.add_argument("--max-degraded", type=int, default=_MAX_DEGRADED_PROBES,
                        help="Restart after N consecutive 'degraded' probes")
    parser.add_argument("--memory-cap-mb", type=float, default=4096.0,
                        help="Restart when daemon RSS exceeds this (set 0 to disable)")
    parser.add_argument("--max-restarts-per-hour", type=int, default=10,
                        help="Stop restarting after this many crashes in 1h")
    args, daemon_args = parser.parse_known_args(argv)
    sup = Supervisor(
        host=args.host,
        port=args.port,
        daemon_args=daemon_args,
        max_failures=args.max_failures,
        max_degraded=args.max_degraded,
        memory_cap_mb=args.memory_cap_mb if args.memory_cap_mb > 0 else None,
        max_restarts_per_hour=args.max_restarts_per_hour,
    )
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
