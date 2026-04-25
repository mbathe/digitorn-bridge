"""Module Spec v3 — Composition / Meta-Module Pattern.

A ``CompositeModule`` defines actions as declarative pipelines of other
module actions.  This enables:
  - Reusable workflows composed from existing modules
  - Meta-modules that orchestrate cross-module operations
  - Higher-level abstractions without duplicating code

Usage::

    composite = CompositeModule.build(
        module_id="data_pipeline",
        version="1.0.0",
        description="ETL pipeline: extract, transform, load",
        pipelines={
            "run_etl": [
                PipelineStep("database", "run_query", param_map={"query": "extract_query"}),
                PipelineStep("api_http", "send_request", param_map={
                    "url": "transform_url",
                    "body": "{{prev.result}}",
                }),
                PipelineStep("database", "execute", param_map={
                    "query": "load_query",
                    "params": "{{prev.result}}",
                }),
            ],
        },
    )
    registry.register_instance(composite)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from digitorn.modules.log import get_logger
from digitorn.modules.base import BaseModule, Platform
from digitorn.modules.manifest import ActionSpec, ModuleManifest, ParamSpec

log = get_logger(__name__)


@dataclass
class PipelineStep:
    """A single step in a composite action pipeline."""

    module: str
    action: str
    param_map: dict[str, str] = field(default_factory=dict)
    condition: str = ""
    on_error: str = "abort"


class CompositeModule(BaseModule):
    """A module whose actions are composed from pipelines of other module actions.

    CompositeModule uses the ServiceBus (via ModuleContext) to call
    actions on other modules.  Each action is a declarative pipeline
    of steps.
    """

    MODULE_ID: str = ""
    VERSION: str = "0.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]
    MODULE_TYPE: str = "user"

    def __init__(
        self,
        module_id: str,
        version: str,
        description: str,
        pipelines: dict[str, list[PipelineStep]],
        author: str = "",
        tags: list[str] | None = None,
    ) -> None:
        self.MODULE_ID = module_id
        self.VERSION = version
        self._description = description
        self._pipelines = pipelines
        self._author = author
        self._tags = tags or []
        super().__init__()

        for action_name, steps in self._pipelines.items():
            self.register_action(
                action_name,
                self._make_pipeline_handler(action_name, steps),
                spec=self._make_action_spec(action_name, steps),
            )

    def _make_pipeline_handler(
        self, action_name: str, steps: list[PipelineStep]
    ) -> Any:
        """Create an async handler for a composite pipeline."""

        async def handler(params: dict[str, Any]) -> dict[str, Any]:
            return await self._execute_pipeline(action_name, steps, params)

        handler.__qualname__ = f"CompositeModule._pipeline_{action_name}"
        return handler

    async def _execute_pipeline(
        self,
        action_name: str,
        steps: list[PipelineStep],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a pipeline of steps sequentially."""
        results: list[Any] = []
        prev_result: Any = None

        for i, step in enumerate(steps):
            resolved = self._resolve_step_params(step, params, prev_result)

            if self.ctx is None or self.ctx.service_bus is None:
                raise RuntimeError(
                    f"CompositeModule '{self.MODULE_ID}' requires ModuleContext "
                    f"with ServiceBus to execute pipeline steps."
                )

            try:
                result = await self.ctx.service_bus.call(
                    step.module, step.action, resolved
                )
                prev_result = result
                results.append({"step": i, "module": step.module, "action": step.action, "result": result})
            except Exception as exc:
                if step.on_error == "continue":
                    prev_result = {"error": str(exc)}
                    results.append({"step": i, "error": str(exc)})
                    continue
                elif step.on_error == "skip":
                    results.append({"step": i, "skipped": True, "reason": str(exc)})
                    continue
                else:
                    return {
                        "success": False,
                        "completed_steps": len(results),
                        "total_steps": len(steps),
                        "error": f"Step {i} ({step.module}.{step.action}) failed: {exc}",
                        "results": results,
                    }

        return {
            "success": True,
            "completed_steps": len(results),
            "total_steps": len(steps),
            "final_result": prev_result,
            "results": results,
        }

    def _resolve_step_params(
        self,
        step: PipelineStep,
        input_params: dict[str, Any],
        prev_result: Any,
    ) -> dict[str, Any]:
        """Resolve parameter templates for a pipeline step."""
        resolved: dict[str, Any] = {}
        for key, value in step.param_map.items():
            if isinstance(value, str):
                if value.startswith("{{prev.result") and value.endswith("}}"):
                    path = value[2:-2]
                    parts = path.split(".", 2)
                    if len(parts) == 2:
                        resolved[key] = prev_result
                    elif len(parts) == 3 and isinstance(prev_result, dict):
                        resolved[key] = prev_result.get(parts[2], value)
                    else:
                        resolved[key] = prev_result
                elif value.startswith("{{input.") and value.endswith("}}"):
                    field_name = value[8:-2]
                    resolved[key] = input_params.get(field_name, value)
                else:
                    resolved[key] = input_params.get(value, value)
            else:
                resolved[key] = value
        return resolved

    def _make_action_spec(self, action_name: str, steps: list[PipelineStep]) -> ActionSpec:
        """Generate an ActionSpec for a composite pipeline action."""
        step_desc = " -> ".join(f"{s.module}.{s.action}" for s in steps)
        all_params: set[str] = set()
        for step in steps:
            for v in step.param_map.values():
                if isinstance(v, str) and v.startswith("{{input.") and v.endswith("}}"):
                    all_params.add(v[8:-2])
                elif isinstance(v, str) and not v.startswith("{{"):
                    all_params.add(v)

        return ActionSpec(
            name=action_name,
            description=f"Composite pipeline: {step_desc}",
            params=[ParamSpec(p, "string", f"Input for pipeline step") for p in sorted(all_params)],
            returns="object",
            returns_description="Pipeline execution result with step-by-step outputs.",
            permission_required="local_worker",
            execution_mode="async",
        )

    def get_manifest(self) -> ModuleManifest:
        actions = list(self._dynamic_specs.values())
        return ModuleManifest(
            module_id=self.MODULE_ID,
            version=self.VERSION,
            description=self._description,
            author=self._author,
            tags=self._tags,
            actions=actions,
            module_type="user",
        )

    @classmethod
    def build(
        cls,
        module_id: str,
        version: str = "1.0.0",
        description: str = "",
        pipelines: dict[str, list[PipelineStep]] | None = None,
        author: str = "",
        tags: list[str] | None = None,
    ) -> "CompositeModule":
        """Factory method to create a CompositeModule."""
        return cls(
            module_id=module_id,
            version=version,
            description=description,
            pipelines=pipelines or {},
            author=author,
            tags=tags,
        )
