"""Daemon instance identifier for client restart detection.

A monotonically-fresh UUID generated at process start. Surfaced to
clients via:

  - The Socket.IO `connected` handshake payload.
  - Every Socket.IO `heartbeat` event.
  - Every HTTP response header `X-Digitorn-Instance`.

Clients compare the value they observe against their stored copy.
Mismatch → daemon restarted → wipe local state and re-seed via the
snapshot endpoint. This is the foundation of the daemon-resource
protocol that drives every real-time client hook.

Why a UUID instead of e.g. the boot timestamp:
  - Two restarts within the same second still produce different ids.
  - The id is opaque to the client (no parsing needed).
  - It survives across timezones, NTP jumps, monotonic-clock resets.
"""

from __future__ import annotations

import uuid

_INSTANCE_ID: str | None = None


def get_instance_id() -> str:
    """Return the daemon's process-wide instance id.

    Generated lazily on first access and cached for the process life.
    Restart = new Python process = new id (this module is reloaded
    fresh every time the daemon comes up).
    """
    global _INSTANCE_ID
    if _INSTANCE_ID is None:
        _INSTANCE_ID = uuid.uuid4().hex
    return _INSTANCE_ID


def reset_instance_id_for_test() -> str:
    """Force a new id (for unit tests that need a fresh boot scenario)."""
    global _INSTANCE_ID
    _INSTANCE_ID = uuid.uuid4().hex
    return _INSTANCE_ID
