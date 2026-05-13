"""Generic worker binary -- ``digitorn-worker``.

One binary, configurable module set. Launched by the supervisor
(or manually by ops) with an env-var or CLI flag pointing at its
config block in ``~/.digitorn/config.yaml``.

CLI::

    digitorn-worker --id heavy --port 18000 \
                    --modules shell,llm_provider,web,mcp

Environment::

    DIGITORN_WORKER_ID=heavy
    DIGITORN_WORKER_PORT=18000
    DIGITORN_WORKER_MODULES=shell,llm_provider,web,mcp
    DIGITORN_WORKERS_SECRET=<32-byte b64url>

At startup the worker:
  1. Reads its config from CLI flags / env / settings file.
  2. Loads the requested modules via the existing module loader
     (Phase 2 -- the wiring is not in this skeleton).
  3. Runs ``module.on_start()`` for each loaded module.
  4. Mounts the routes from ``routes.py``.
  5. Binds 127.0.0.1:<port> with winloop on Windows / uvloop on
     Linux (same loop policy as ``digitorn-api``).

Phase 1 status: the app boots, exposes /health and /modules, and
serves the route placeholders. Real module loading is Phase 2.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import typer
import uvicorn
from fastapi import FastAPI

from .routes import router

logger = logging.getLogger(__name__)

cli = typer.Typer(add_completion=False, no_args_is_help=False)


def _load_shared_secret(override: str | None = None) -> str:
    """Read the daemon/worker shared secret. ``override`` wins; else
    ``DIGITORN_WORKERS_SECRET`` env; else
    ``~/.digitorn/.workers-secret`` (auto-created on first boot).

    The supervisor is responsible for creating that file on first
    install with 32 random bytes (mode 0600) and propagating it via
    env to spawned workers. The worker keeps a fallback file-read
    so a manual ``digitorn-worker`` invocation Just Works.
    """
    if override:
        return override
    env = os.environ.get("DIGITORN_WORKERS_SECRET")
    if env:
        return env
    secret_path = Path.home() / ".digitorn" / ".workers-secret"
    if secret_path.exists():
        return secret_path.read_text(encoding="utf-8").strip()
    # Generate one. The daemon (Phase 4) will normally have done
    # this at install time; this branch is defensive for ad-hoc
    # ``digitorn-worker`` runs in dev.
    import secrets
    secret = secrets.token_urlsafe(32)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(secret, encoding="utf-8")
    try:
        os.chmod(secret_path, 0o600)
    except OSError:
        pass
    logger.warning(
        "worker_generated_fresh_secret at=%s -- the daemon should "
        "create this file at install time, regenerating it for "
        "every fresh process is fine but inconsistent secrets "
        "across daemon+workers will break auth.",
        secret_path,
    )
    return secret


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Worker process lifecycle.

    Boot path:
      1. Discover and load only the modules in ``app.state.hosted_modules``
         via the standard ``digitorn.core.loader.load_modules`` -- the
         same loader the daemon uses, so module discovery, platform
         guards, and TOML-declared dependencies behave identically.
      2. Build a ``ServiceBus`` and register every loaded module so
         cross-module calls inside the worker work natively.
      3. Run each module's ``on_start()`` lifecycle hook. Failures
         are logged but don't abort the worker boot -- the
         offending module's actions return errors at dispatch time.
      4. Stash everything on ``app.state.modules`` /
         ``app.state.service_bus`` so the route handlers can dispatch.

    Shutdown reverses the above: call ``on_stop()`` on every module
    in registration order, swallowing exceptions so a misbehaving
    module can't block the worker exit.
    """
    app.state.started_at = time.monotonic()
    app.state.modules = {}
    app.state.service_bus = None
    app.state.phase = "2-loading-modules"
    logger.info(
        "worker_loading id=%s modules=%s port=%s",
        app.state.worker_id, sorted(app.state.hosted_modules),
        getattr(app.state, "port", "?"),
    )

    if app.state.hosted_modules:
        try:
            from digitorn.modules.registry import ModuleRegistry
            from digitorn.core.loader import load_modules
            registry = ModuleRegistry()
            # ``enabled=`` + ``load_all=False`` restricts the loader
            # to exactly the modules this worker is responsible for.
            load_results = load_modules(
                registry,
                enabled=list(app.state.hosted_modules),
                load_all=False,
            )
            app.state.module_registry = registry
            logger.info(
                "worker_load_results id=%s results=%s",
                app.state.worker_id, load_results,
            )
        except Exception as exc:
            logger.exception(
                "worker_module_load_failed id=%s err=%s",
                app.state.worker_id, exc,
            )
            registry = None
            app.state.module_registry = None

        if registry is not None:
            try:
                from digitorn.modules.service_bus import ServiceBus
                service_bus = ServiceBus()
                app.state.service_bus = service_bus
            except Exception as exc:
                logger.warning(
                    "worker_service_bus_init_failed err=%s -- "
                    "cross-module calls inside this worker will fail",
                    exc,
                )
                service_bus = None

            for module_id in app.state.hosted_modules:
                try:
                    module = registry.get(module_id)
                except Exception as exc:
                    logger.warning(
                        "worker_module_instantiate_failed id=%s "
                        "module=%s err=%s -- skipping",
                        app.state.worker_id, module_id, exc,
                    )
                    continue
                if service_bus is not None:
                    service_bus.register_service(module_id, module)
                    module._service_bus = service_bus
                app.state.modules[module_id] = module

            for module_id, module in app.state.modules.items():
                try:
                    await module.on_start()
                    module._started_ok = True  # type: ignore[attr-defined]
                except Exception as exc:
                    logger.warning(
                        "worker_module_on_start_failed id=%s "
                        "module=%s err=%s",
                        app.state.worker_id, module_id, exc,
                    )
                    module._started_ok = False  # type: ignore[attr-defined]
                    module._start_error = str(exc)  # type: ignore[attr-defined]

    app.state.phase = "ready"
    logger.info(
        "worker_started id=%s loaded=%d ports=%s",
        app.state.worker_id, len(app.state.modules),
        getattr(app.state, "port", "?"),
    )

    try:
        yield
    finally:
        app.state.phase = "stopping"
        logger.info(
            "worker_stopping id=%s uptime_s=%.1f",
            app.state.worker_id,
            time.monotonic() - app.state.started_at,
        )
        for module_id, module in list(app.state.modules.items()):
            try:
                if hasattr(module, "on_stop"):
                    await module.on_stop()
            except Exception as exc:
                logger.warning(
                    "worker_module_on_stop_failed module=%s err=%s",
                    module_id, exc,
                )


def create_app(
    *,
    worker_id: str,
    modules: list[str],
    shared_secret: str,
    port: int | None = None,
) -> FastAPI:
    """Factory: returns a FastAPI app configured for this worker."""
    app = FastAPI(
        title=f"digitorn-worker[{worker_id}]",
        version="1.0.0",
        lifespan=_lifespan,
    )
    app.state.worker_id = worker_id
    app.state.hosted_modules = list(modules)
    app.state.shared_secret = shared_secret
    app.state.port = port
    app.state.phase = "1-skeleton"
    app.include_router(router)
    return app


@cli.command()
def run(
    worker_id: str = typer.Option(
        os.environ.get("DIGITORN_WORKER_ID", "default"),
        "--id", help="Worker identifier (must match the config block).",
    ),
    host: str = typer.Option(
        os.environ.get("DIGITORN_WORKER_HOST", "127.0.0.1"),
        "--host", help="Bind address. Default: loopback only.",
    ),
    port: int = typer.Option(
        int(os.environ.get("DIGITORN_WORKER_PORT", "18000")),
        "--port", help="Bind port.",
    ),
    modules: str = typer.Option(
        os.environ.get("DIGITORN_WORKER_MODULES", ""),
        "--modules",
        help="Comma-separated list of modules to host (e.g. shell,web).",
    ),
    secret: str = typer.Option(
        "",
        "--secret",
        help=(
            "Shared secret. Defaults to DIGITORN_WORKERS_SECRET env, "
            "then ~/.digitorn/.workers-secret."
        ),
    ),
    log_level: str = typer.Option(
        "info", "--log-level",
    ),
) -> None:
    """Boot one worker process. Idempotent: safe to invoke under
    a supervisor that restarts on crash.
    """
    module_list = [m.strip() for m in modules.split(",") if m.strip()]
    if not module_list:
        # An empty worker is valid (e.g. early dev / health-only smoke
        # test) but warn loudly so a misconfigured spawn is visible.
        logger.warning(
            "worker_has_no_modules id=%s -- the worker will boot but "
            "serve no tool calls. Check --modules / "
            "DIGITORN_WORKER_MODULES.",
            worker_id,
        )

    shared_secret = _load_shared_secret(secret or None)
    app = create_app(
        worker_id=worker_id,
        modules=module_list,
        shared_secret=shared_secret,
        port=port,
    )

    # Match the loop policy used by ``digitorn-api`` so the worker
    # gets winloop on Windows and uvloop / asyncio on POSIX. The
    # daemon's policy installer is the canonical implementation;
    # import here to keep the worker import-light when not on
    # Windows.
    try:
        from digitorn.core.server import (  # noqa: F401  (side-effects)
            _install_windows_event_loop_policy,
            _select_uvicorn_loop_name,
        )
        loop_name = _select_uvicorn_loop_name()
    except Exception:
        loop_name = "asyncio"

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        loop=loop_name,
    )


def main() -> None:
    """Entry point for the ``digitorn-worker`` script. Wired in
    ``pyproject.toml`` under ``[tool.poetry.scripts]``.
    """
    cli()


if __name__ == "__main__":
    main()
