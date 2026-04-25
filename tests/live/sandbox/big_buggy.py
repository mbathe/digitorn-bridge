"""A reasonably-sized module with multiple bugs to fix in ONE Edit call.

Each bug is in a different function. A senior developer would fix them
all surgically in a single coordinated change.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────────────────

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
VERSION = "0.1.0"


@dataclass
class Config:
    """Application configuration."""
    host: str = "localhost"
    port: int = 8080
    debug: bool = False
    # BUG #1: the default should be 8192, not 1024 (breaks context)
    max_tokens: int = 8192
    api_key: str | None = None


# ────────────────────────────────────────────────────────────
# Client
# ────────────────────────────────────────────────────────────


class Client:
    """HTTP-like client that talks to a remote service."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._session_id = None
        self._retries = 0

    def connect(self) -> bool:
        """Open a connection to the remote service."""
        logger.info("connecting to %s:%d", self._config.host, self._config.port)
        # BUG #2: returns False even on success — always reports failure
        return True

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a payload and return the response."""
        if not self._session_id:
            raise RuntimeError("not connected")
        body = json.dumps(payload)
        logger.debug("sending %d bytes", len(body))
        # BUG #3: ignores retries — no retry loop
        return {"ok": True, "echo": payload}

    def close(self) -> None:
        """Close the client."""
        self._session_id = None


# ────────────────────────────────────────────────────────────
# Parser
# ────────────────────────────────────────────────────────────


class Parser:
    """Parses inbound messages from the remote service."""

    def __init__(self) -> None:
        self._buffer: list[str] = []

    def feed(self, chunk: str) -> None:
        """Add a chunk of text to the internal buffer."""
        self._buffer.append(chunk)

    def parse(self) -> list[dict[str, Any]]:
        """Consume the buffer and return parsed messages."""
        messages = []
        # BUG #4: concatenates without joining → splits each char as a token
        joined = "\n".join(self._buffer).split("\n")
        for line in joined:
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("bad json: %s", line[:50])
        self._buffer.clear()
        return messages


# ────────────────────────────────────────────────────────────
# Worker
# ────────────────────────────────────────────────────────────


class Worker:
    """Background worker that pulls jobs and processes them."""

    def __init__(self, client: Client, parser: Parser) -> None:
        self.client = client
        self.parser = parser
        self.stats = {"processed": 0, "errors": 0}

    def run_once(self, job: dict[str, Any]) -> bool:
        """Execute a single job."""
        try:
            response = self.client.send(job)
            self.stats["processed"] += 1
            # BUG #5: returns None instead of True on success — callers check truthiness
            return True
        except Exception as exc:
            logger.error("job failed: %s", exc)
            self.stats["errors"] += 1
            return False

    def drain(self, jobs: list[dict[str, Any]]) -> dict[str, int]:
        """Process a batch of jobs and return stats."""
        for job in jobs:
            self.run_once(job)
        return self.stats


# ────────────────────────────────────────────────────────────
# CLI entrypoint
# ────────────────────────────────────────────────────────────


def main() -> int:
    config = Config(host="api.example.com", port=443, debug=True)
    client = Client(config)
    parser = Parser()

    if not client.connect():
        return 1

    worker = Worker(client, parser)
    jobs = [{"id": i, "payload": f"job_{i}"} for i in range(10)]
    stats = worker.drain(jobs)
    print(f"processed={stats['processed']} errors={stats['errors']}")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
