"""Boot fresh daemon + run the wire-contract scout."""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
PORT = 8286


def _wait_ready(url, timeout=90.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=2.0).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="dg-scout-wire-")
    env = dict(os.environ)
    env["DIGITORN_HOME"] = data_dir
    env["DIGITORN_AUTH_DISABLED"] = "1"
    env["DIGITORN_DISCOVERY__SKIP_EMBEDDINGS"] = "1"

    print(f"[boot] daemon on port {PORT} data_dir={data_dir}")
    proc = subprocess.Popen(
        [sys.executable, "-m", "digitorn.core.server", "start",
         "--port", str(PORT), "--no-sandbox"],
        env=env, cwd=str(ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    try:
        if not _wait_ready(f"http://127.0.0.1:{PORT}"):
            print("FAIL: daemon boot timeout")
            return 1
        child_env = dict(os.environ)
        child_env["DIGITORN_BASE"] = f"http://127.0.0.1:{PORT}"
        child_env["PYTHONIOENCODING"] = "utf-8"
        script = Path(__file__).parent / "scout_wire_contract_after_fix.py"
        r = subprocess.run(
            [sys.executable, str(script)],
            env=child_env, cwd=str(ROOT), check=False,
        )
        return r.returncode
    finally:
        try:
            proc.terminate(); proc.wait(timeout=5.0)
        except Exception:
            try: proc.kill()
            except Exception: pass


if __name__ == "__main__":
    sys.exit(main())
