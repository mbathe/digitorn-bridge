"""Pydantic config models for the worker subsystem.

Loaded from the main ``Settings`` under ``workers:``. With an empty
list, no proxies are installed and the daemon runs as today (legacy
in-process mode).

Example YAML (``~/.digitorn/config.yaml``)::

    workers:
      enabled: true
      strict_no_block: false
      timeout_s: 600
      workers:
        - id: heavy
          host: 127.0.0.1
          port: 18000
          modules: [shell, llm_provider, web, mcp, fastembed, rag]
        - id: maintenance
          host: 127.0.0.1
          port: 18001
          modules: [cron]
"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class WorkerConfig(BaseModel):
    """One worker process declaration.

    ``modules`` is the list of module names the worker hosts (e.g.
    ``["shell", "web"]``). At daemon startup, the registry maps each
    of those module names to this endpoint; the dispatcher then
    routes any call to those modules to this worker over HTTP.

    Multiple workers MAY host the same module -- the registry treats
    that as a load-balanced pool (round-robin by default).
    """

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=64)]
    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)]
    modules: list[str] = Field(default_factory=list)
    # Optional override of the shared-secret token. When unset the
    # daemon and worker both read ``~/.digitorn/.workers-secret``
    # (auto-generated on first boot, mode 0600).
    secret: str | None = None

    @property
    def base_url(self) -> str:
        """``http://host:port`` -- no trailing slash."""
        return f"http://{self.host}:{self.port}"


class WorkersConfig(BaseModel):
    """Top-level ``workers:`` block of the daemon settings.

    ``enabled`` is the master switch -- when ``False`` the registry
    initialises empty and the daemon behaviour is identical to the
    legacy in-process flow.

    ``strict_no_block`` (opt-in, default off) refuses to boot if any
    module flagged as "potentially blocking" is not hosted by a
    worker. Use in production deployments where any stall is
    unacceptable.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    strict_no_block: bool = False
    # Default per-request timeout for ``WorkerClient`` calls. The
    # worker enforces its own internal timeouts on top of this.
    timeout_s: float = 600.0
    # Number of retries on transient network errors (connection
    # refused, read timeout). LLM streaming sets its own override.
    retries: int = 2
    # Round-robin pool of workers. Empty list = legacy in-process.
    workers: list[WorkerConfig] = Field(default_factory=list)

    def hosted_module_names(self) -> set[str]:
        """Flat set of every module hosted by at least one worker."""
        out: set[str] = set()
        for w in self.workers:
            out.update(w.modules)
        return out
