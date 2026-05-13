"""Digitorn worker subsystem.

Generic out-of-process container for hosting Digitorn modules so the
main daemon (digitorn-api) never blocks on subprocess close, SSL
handshakes, GIL-heavy CPU work, slow Postgres queries, etc.

Design contract (must hold):
  * Existing modules are NOT modified -- the worker imports and runs
    them via the standard module loader inside its own process.
  * Existing dispatch logic (tool_exec, agent_loop, hooks, middleware)
    sees NO behavioural change. The transparency layer is a
    ``ModuleProxy`` that implements the same interface as a real
    module and forwards calls via HTTP.
  * With ``workers`` config empty / disabled, the daemon behaviour is
    byte-identical to today. Workers are pure opt-in.

Public API:
  * ``WorkerConfig`` / ``WorkersConfig`` -- pydantic models loaded
    from the main config under ``workers:``.
  * ``WorkerRegistry`` -- maps ``module_name -> WorkerEndpoint`` and
    answers ``route(name)`` for the dispatcher's lookup.
  * ``WorkerClient`` -- httpx-based async client used by proxies.
  * ``ModuleProxy`` / ``LLMProviderProxy`` -- drop-in replacements
    for real modules / providers.
  * ``cron_lock.FileLeader`` -- file-based leader election (no
    Postgres dep, works on Windows + Linux + macOS).

Skeleton status: Phase 1 -- files exist, structure is locked in, but
the proxies and routing are stubs to be fleshed out incrementally.
"""
from __future__ import annotations

__all__ = [
    "WorkerConfig",
    "WorkersConfig",
    "WorkerEndpoint",
    "WorkerRegistry",
    "WorkerClient",
    "ModuleProxy",
    "LLMProviderProxy",
]

from .config import WorkerConfig, WorkersConfig
from .registry import WorkerEndpoint, WorkerRegistry
from .client import WorkerClient
from .proxy import LLMProviderProxy, ModuleProxy
