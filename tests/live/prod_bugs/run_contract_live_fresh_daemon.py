"""Spin up a fresh daemon subprocess with the current code and run
the session-event contract live test against it.

Proves end-to-end that events emitted by the migrated code path
carry op_id/op_type/op_state in both the Socket.IO envelope and the
persisted DB payload, through a real HTTP round-trip against a real
FastAPI lifespan.

Does NOT touch the user's running daemon.
"""
from __future__ import annotations
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
PORT = 8284  # an unused port in the range


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
    # Fresh data dir so we don't collide with the user's daemon.
    data_dir = tempfile.mkdtemp(prefix="dg-contract-live-")
    env = dict(os.environ)
    env["DIGITORN_HOME"] = data_dir
    env["DIGITORN_AUTH_DISABLED"] = "1"  # skip JWT dance for the test
    env["DIGITORN_BASE"] = f"http://127.0.0.1:{PORT}"
    # Disable RAG (qdrant) + preload so the boot is faster.
    env["DIGITORN_DISCOVERY__SKIP_EMBEDDINGS"] = "1"

    print(f"[boot] launching daemon on port {PORT} with data_dir={data_dir}")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "digitorn.core.server", "start",
            "--port", str(PORT), "--no-sandbox",
        ],
        env=env, cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        if not _wait_ready(f"http://127.0.0.1:{PORT}"):
            print("FAIL: daemon did not become ready in 90s")
            return 1
        print(f"[boot] ready on {PORT}")

        # Invoke the live contract test as a subprocess so it picks
        # up the fresh ``DIGITORN_BASE`` from env cleanly.
        os.environ["DIGITORN_BASE"] = f"http://127.0.0.1:{PORT}"
        child_env = dict(os.environ)
        child_env["DIGITORN_BASE"] = f"http://127.0.0.1:{PORT}"
        child_env["PYTHONIOENCODING"] = "utf-8"
        test_path = (
            Path(__file__).parent / "verify_session_event_contract_live.py"
        )
        print(f"[run] {test_path}")
        result = subprocess.run(
            [sys.executable, str(test_path)],
            env=child_env,
            cwd=str(ROOT),
            check=False,
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
