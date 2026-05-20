"""Module-to-worker routing table."""
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
    """Maps `module_name -> [WorkerEndpoint, ...]` and picks one."""

    def __init__(self) -> None:
        self._by_module: dict[str, list[WorkerEndpoint]] = {}
        self._rr_iters: dict[str, "itertools.cycle[WorkerEndpoint]"] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_config(
        cls, cfg: WorkersConfig, *, default_secret: str,
    ) -> "WorkerRegistry":
        """Build a registry from the parsed `workers:` block."""
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
        """Drop one endpoint -- used when the supervisor reports."""
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
        """Pick one endpoint hosting `module_name`. Returns `None`."""
        with self._lock:
            it = self._rr_iters.get(module_name)
            if it is None:
                return None
            try:
                return next(it)
            except StopIteration:
                return None

    def endpoints_for(self, module_name: str) -> list[WorkerEndpoint]:
        """Return every endpoint hosting `module_name`."""
        with self._lock:
            return list(self._by_module.get(module_name, []))

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
    """Return the daemon-wide registry, creating an empty one."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = WorkerRegistry()
    return _DEFAULT_REGISTRY

def install_default_registry(reg: WorkerRegistry) -> None:
    """Replace the global registry. Called once at daemon startup."""
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = reg

def ensure_default_registry_from_settings() -> WorkerRegistry:
    """Lazily populate the default registry from current `Settings`."""
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
