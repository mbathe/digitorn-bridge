"""AppBootstrapper — execute a compiled app definition against live modules.

Takes a ``CompiledApp`` (produced by the compiler) and:
    1. Executes setup actions on each module (in declaration order)
    2. Stores module constraints for runtime enforcement
    3. Returns a ``BootstrapResult`` with per-step outcomes

All setup actions route through ``module.execute()`` so the full security
gate, audit trail, and param validation pipeline applies.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from digitorn.core.app.compiler import CompiledApp, CompiledModuleConfig, CompiledSetupStep
from digitorn.core.app.errors import AppBootstrapError
from digitorn.modules.log import get_logger

if TYPE_CHECKING:
    from digitorn.core.app.runtime import AppRuntimeStore
    from digitorn.modules.registry import ModuleRegistry
    from digitorn.modules.service_bus import ServiceBus

log = get_logger(__name__)


@dataclass
class SetupStepResult:
    """Outcome of a single setup action execution."""

    module_id: str
    action: str
    success: bool
    duration_ms: float = 0.0
    result: Any = None
    error: str | None = None


@dataclass
class BootstrapResult:
    """Full outcome of bootstrapping a compiled app."""

    app_id: str
    success: bool
    setup_results: list[SetupStepResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    constraints: dict[str, dict[str, Any]] = field(default_factory=dict)
    duration_ms: float = 0.0

    @property
    def failed_steps(self) -> list[SetupStepResult]:
        return [r for r in self.setup_results if not r.success]

    @property
    def step_count(self) -> int:
        return len(self.setup_results)

    def summary(self) -> dict[str, Any]:
        """Compact summary for logging / API responses."""
        return {
            "app_id": self.app_id,
            "success": self.success,
            "total_steps": self.step_count,
            "failed_steps": len(self.failed_steps),
            "modules": list(self.constraints.keys()),
            "duration_ms": round(self.duration_ms, 1),
            "errors": self.errors[:5],
        }


class AppBootstrapper:
    """Execute compiled app definitions against live modules.

    Usage::

        bootstrapper = AppBootstrapper(registry, service_bus)
        result = await bootstrapper.bootstrap(compiled_app)

        if not result.success:
            for step in result.failed_steps:
                print(f"FAILED: {step.module_id}.{step.action}: {step.error}")
    """

    def __init__(
        self,
        registry: "ModuleRegistry",
        service_bus: "ServiceBus | None" = None,
        *,
        stop_on_error: bool = False,
        runtime_store: "AppRuntimeStore | None" = None,
    ) -> None:
        self._registry = registry
        self._service_bus = service_bus
        self._stop_on_error = stop_on_error
        self._runtime_store = runtime_store

    async def bootstrap(self, compiled: CompiledApp) -> BootstrapResult:
        """Execute all setup steps and store constraints.

        Steps are executed in module declaration order, and within each module
        in setup list order.  Each step goes through ``module.execute()`` so
        the full security and audit pipeline applies.

        Args:
            compiled: A ``CompiledApp`` produced by ``AppYAMLCompiler.compile()``.

        Returns:
            ``BootstrapResult`` with per-step outcomes and stored constraints.
        """
        t0 = time.monotonic()
        all_results: list[SetupStepResult] = []
        all_errors: list[str] = []
        stored_constraints: dict[str, dict[str, Any]] = {}

        log.info(
            "app_bootstrap_start: app_id=%s modules=%s",
            compiled.app_id,
            compiled.module_ids,
        )

        for module_id, module_config in compiled.modules.items():
            step_results = await self._bootstrap_module(
                module_id, module_config, all_errors
            )
            all_results.extend(step_results)

            if module_config.constraints:
                stored_constraints[module_id] = module_config.constraints

            if self._stop_on_error and any(not r.success for r in step_results):
                all_errors.append(
                    f"Stopped: module '{module_id}' had setup failures"
                )
                break

        duration = (time.monotonic() - t0) * 1000
        success = len(all_errors) == 0 and all(r.success for r in all_results)

        result = BootstrapResult(
            app_id=compiled.app_id,
            success=success,
            setup_results=all_results,
            errors=all_errors,
            constraints=stored_constraints,
            duration_ms=duration,
        )

        if self._runtime_store is not None:
            try:
                runtime = self._runtime_store.register(compiled)
                log.info(
                    "app_runtime_ready: %s actions=%d",
                    compiled.app_id,
                    len(runtime._action_policies),
                )
            except Exception as exc:
                log.warning("app_runtime_build_failed: %s — %s", compiled.app_id, exc)

        try:
            from digitorn.core.app.syncer import AppSyncer

            syncer = AppSyncer()
            synced = await syncer.sync(compiled)
            if synced:
                log.info("app_db_synced: %s", compiled.app_id)
        except Exception as exc:
            log.warning("app_db_sync_failed: %s — %s", compiled.app_id, exc)

        if success:
            log.info("app_bootstrap_ok: %s", result.summary())
        else:
            log.warning("app_bootstrap_partial: %s", result.summary())

        return result

    async def _bootstrap_module(
        self,
        module_id: str,
        config: CompiledModuleConfig,
        errors: list[str],
    ) -> list[SetupStepResult]:
        """Execute all setup steps for a single module."""
        results: list[SetupStepResult] = []

        try:
            module = self._registry.create(module_id)
        except Exception as exc:
            errors.append(f"Cannot create module '{module_id}': {exc}")
            return results

        if config.config:
            try:
                await module.on_config_update(config.config)
                log.debug(
                    "module_config_applied: %s keys=%s",
                    module_id, list(config.config.keys()),
                )
            except Exception as exc:
                errors.append(f"{module_id}.config: {exc}")
                if self._stop_on_error:
                    return results

        for step in config.setup_steps:
            result = await self._execute_step(module, step)
            results.append(result)

            if not result.success:
                errors.append(
                    f"{module_id}.{step.action}: {result.error}"
                )
                if self._stop_on_error:
                    break

        return results

    async def _execute_step(
        self,
        module: Any,
        step: CompiledSetupStep,
    ) -> SetupStepResult:
        """Execute a single setup step via module.execute()."""
        t0 = time.monotonic()
        try:
            action_result = await module.execute(step.action, step.resolved_params)
            duration = (time.monotonic() - t0) * 1000

            success = getattr(action_result, "success", True)
            error = getattr(action_result, "error", None) if not success else None

            result = SetupStepResult(
                module_id=step.module_id,
                action=step.action,
                success=success,
                duration_ms=duration,
                result=getattr(action_result, "data", action_result),
                error=error,
            )

            if success:
                log.debug(
                    "setup_step_ok: %s.%s (%.1fms)",
                    step.module_id, step.action, duration,
                )
            else:
                log.warning(
                    "setup_step_failed: %s.%s — %s",
                    step.module_id, step.action, error,
                )

            return result

        except Exception as exc:
            duration = (time.monotonic() - t0) * 1000
            log.error(
                "setup_step_exception: %s.%s — %s",
                step.module_id, step.action, exc,
            )
            return SetupStepResult(
                module_id=step.module_id,
                action=step.action,
                success=False,
                duration_ms=duration,
                error=str(exc),
            )
