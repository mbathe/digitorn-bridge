"""Launch a fresh daemon + run the join_session hydration test."""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
PORT = 8285


def _wait_ready(url: str, timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="dg-hydration-live-")
    env = dict(os.environ)
    env["DIGITORN_HOME"] = data_dir
    env["DIGITORN_AUTH_DISABLED"] = "1"
    env["DIGITORN_DISCOVERY__SKIP_EMBEDDINGS"] = "1"

    print(f"[boot] launching daemon on port {PORT} with data_dir={data_dir}")
    proc = subprocess.Popen(
        [sys.executable, "-m", "digitorn.core.server", "start",
         "--port", str(PORT), "--no-sandbox"],
        env=env, cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        if not _wait_ready(f"http://127.0.0.1:{PORT}"):
            print("FAIL: daemon did not become ready in 90s")
            return 1
        print(f"[boot] ready on {PORT}")

        child_env = dict(os.environ)
        child_env["DIGITORN_BASE"] = f"http://127.0.0.1:{PORT}"
        child_env["PYTHONIOENCODING"] = "utf-8"
        test_path = (
            Path(__file__).parent / "verify_join_session_full_hydration.py"
        )
        print(f"[run] {test_path}")
        result = subprocess.run(
            [sys.executable, str(test_path)],
            env=child_env, cwd=str(ROOT), check=False,
        )
        return result.returncode
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
