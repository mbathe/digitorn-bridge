"""Module-to-worker routing table.

The registry is the only piece of glue the daemon needs to consult
when deciding "where does this module live?". The dispatcher asks
``registry.route("shell")`` -- if it gets an endpoint, it routes via
HTTP; otherwise it falls back to the legacy in-process path.

Routing strategy: a module may be hosted by multiple workers (a
load-balanced pool). The registry picks one per call. The default
strategy is round-robin; future strategies (least-busy, sticky-by-
session) plug in via ``WorkerRegistry.select_strategy``.
"""
from __future__ import annotations

import itertools
import logging
import threading
from dataclasses import dataclass

from .config import WorkerConfig, WorkersConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerEndpoint:
    """One concrete worker the dispatcher can reach."""

    worker_id: str
    base_url: str    # http://host:port (no trailing slash)
    secret: str      # shared secret for the Authorization header


class WorkerRegistry:
    """Maps ``module_name -> [WorkerEndpoint, ...]`` and picks one
    per call. Thread-safe (the dispatcher may live on the main loop
    but background pools or registration hooks may mutate it).

    Empty registry = no routing = legacy in-process behaviour. The
    dispatcher integration MUST check ``route()`` first and fall back
    on ``None`` so the in-process path stays the default.
    """

    def __init__(self) -> None:
        self._by_module: dict[str, list[WorkerEndpoint]] = {}
        self._rr_iters: dict[str, "itertools.cycle[WorkerEndpoint]"] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_config(
        cls, cfg: WorkersConfig, *, default_secret: str,
    ) -> "WorkerRegistry":
        """Build a registry from the parsed ``workers:`` block.

        ``default_secret`` is used when a worker entry doesn't carry
        its own (loaded from ``~/.digitorn/.workers-secret``).
        Returns an empty registry when ``cfg.enabled`` is False.
        """
        reg = cls()
        if not cfg.enabled or not cfg.workers:
            return reg
        for w in cfg.workers:
            ep = WorkerEndpoint(
                worker_id=w.id,
                base_url=w.base_url,
                secret=w.secret or default_secret,
            )
            for mod_name in w.modules:
                reg.add(mod_name, ep)
        return reg

    def add(self, module_name: str, endpoint: WorkerEndpoint) -> None:
        with self._lock:
            self._by_module.setdefault(module_name, []).append(endpoint)
            # Rebuild the round-robin iterator on every add so the
            # new endpoint joins the rotation immediately.
            self._rr_iters[module_name] = itertools.cycle(
                self._by_module[module_name],
            )

    def remove(self, module_name: str, worker_id: str) -> None:
        """Drop one endpoint -- used when the supervisor reports a
        worker has crashed and needs to be evicted from the pool.
        """
        with self._lock:
            eps = self._by_module.get(module_name)
            if not eps:
                return
            self._by_module[module_name] = [
                e for e in eps if e.worker_id != worker_id
            ]
            if self._by_module[module_name]:
                self._rr_iters[module_name] = itertools.cycle(
                    self._by_module[module_name],
                )
            else:
                self._by_module.pop(module_name, None)
                self._rr_iters.pop(module_name, None)

    def route(self, module_name: str) -> WorkerEndpoint | None:
        """Pick one endpoint hosting ``module_name``. Returns ``None``
        when no worker hosts it -- the caller MUST fall back to the
        in-process path on ``None``.
        """
        with self._lock:
            it = self._rr_iters.get(module_name)
            if it is None:
                return None
            try:
                return next(it)
            except StopIteration:
                return None

    def hosted_modules(self) -> list[str]:
        with self._lock:
            return sorted(self._by_module.keys())

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {m: len(eps) for m, eps in self._by_module.items()}

    def is_empty(self) -> bool:
        with self._lock:
            return not self._by_module


# Module-level singleton (lazy-initialised by the daemon at boot).
# Stays empty when workers are disabled; the dispatcher integration
# is a no-op in that case.
_DEFAULT_REGISTRY: WorkerRegistry | None = None


def get_default_registry() -> WorkerRegistry:
    """Return the daemon-wide registry, creating an empty one on
    first call. Threadsafe by virtue of GIL+ordering -- worst case
    two callers race to install their own empty registry, which is
    benign because empty registries are interchangeable.
    """
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = WorkerRegistry()
    return _DEFAULT_REGISTRY


def install_default_registry(reg: WorkerRegistry) -> None:
    """Replace the global registry. Called once at daemon startup
    after parsing the ``workers:`` config. Idempotent."""
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = reg


def ensure_default_registry_from_settings() -> WorkerRegistry:
    """Lazily populate the default registry from current ``Settings``.

    Returns the (possibly empty) default registry. Safe to call from
    anywhere on the hot path: subsequent calls after the first
    successful populate are O(1) early returns. When workers are
    disabled or settings can't be loaded, returns the empty registry
    (i.e. all ``route()`` calls return None, dispatcher falls through
    to in-process execution).

    Designed to be the single call site bootstrap needs:

        from digitorn.workers.registry import (
            ensure_default_registry_from_settings,
        )
        reg = ensure_default_registry_from_settings()
        if endpoint := reg.route(module_id):
            wrap_module_for_worker(module, endpoint)
    """
    reg = get_default_registry()
    if not reg.is_empty():
        return reg
    try:
        from digitorn.core.config import get_settings
        cfg = get_settings().workers
    except Exception as exc:
        logger.warning(
            "workers registry: cannot load Settings (%s) -- "
            "running with empty routing table",
            exc,
        )
        return reg
    if not cfg.enabled or not cfg.workers:
        return reg
    try:
        from .app import _load_shared_secret
        secret = _load_shared_secret()
    except Exception as exc:
        logger.warning(
            "workers registry: shared secret unavailable (%s) -- "
            "running with empty routing table",
            exc,
        )
        return reg
    new_reg = WorkerRegistry.from_config(cfg, default_secret=secret)
    install_default_registry(new_reg)
    logger.info(
        "workers_registry_populated workers=%d modules=%s",
        len(cfg.workers), sorted(cfg.hosted_module_names()),
    )
    return new_reg
