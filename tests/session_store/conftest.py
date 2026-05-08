"""Pytest fixtures for session_store tests.

Goal: every test runs in <2 seconds, no network, no Postgres, no Redis.
The whole subsystem is self-contained so the fixtures just point at a
fresh tmp directory and instantiate the relevant class."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the `digitorn` namespace importable when pytest is invoked from
# the repo root. The package lives at packages/digitorn/digitorn/...
ROOT = Path(__file__).resolve().parent.parent.parent
PKG = ROOT / "packages" / "digitorn"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    """Throwaway root for blob_store / session_store on each test."""
    return tmp_path


@pytest.fixture
def make_event():
    """Factory that returns a callable building Event instances with
    sensible defaults. Tests focus on the fields they care about."""
    from digitorn.core.runtime.session_store.types import Event

    def _make(**overrides) -> Event:
        defaults = dict(
            type="user_message",
            role="user",
            content="hello",
        )
        defaults.update(overrides)
        return Event(**defaults)
    return _make
