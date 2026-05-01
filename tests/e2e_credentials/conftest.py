"""Pytest fixtures for the e2e_credentials suite.

Each scenario file imports `from .shared` for helpers. The fixtures
here ensure:
  - the daemon is reachable on port 8765 (started externally)
  - mock servers can be started/stopped per-test
  - the JWT cache exists (otherwise tests can't auth)
"""

from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path

import pytest


# Make `tests/e2e_credentials/shared` importable from inside scenarios
# without forcing a setup.py.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))


def _port_open(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


@pytest.fixture(scope="session")
def daemon_url() -> str:
    """The daemon is started externally - we just verify reachable."""
    url = os.environ.get("DIGITORN_DAEMON", "http://127.0.0.1:8765")
    if not _port_open("127.0.0.1", 8765):
        pytest.skip(
            "daemon not running on :8765 - "
            "start with `digitorn start --port 8765` first",
        )
    return url


@pytest.fixture(scope="session")
def jwt_present() -> bool:
    p = os.path.expanduser("~/.digitorn/credentials.json")
    if not os.path.isfile(p):
        pytest.skip(
            "no JWT at ~/.digitorn/credentials.json - "
            "log in first via `digitorn auth login`",
        )
    return True


@pytest.fixture
def mock_llm():
    """Start the mock OpenAI-compatible LLM server and stop it after."""
    import subprocess
    import sys as _sys
    proc = subprocess.Popen(
        [_sys.executable, str(_HERE.parent / "mock_llm_server.py")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait until ready.
    deadline = time.time() + 10
    while time.time() < deadline:
        if _port_open("127.0.0.1", 9999):
            break
        time.sleep(0.2)
    yield "http://127.0.0.1:9999"
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
