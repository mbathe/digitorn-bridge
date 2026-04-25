"""Module layer — BaseModule interface, registry, manifest system, and cache decorators."""

try:
    from digitorn.cache import cacheable, invalidates_cache
except ImportError:
    def cacheable(*args, **kwargs):  # type: ignore[misc]
        def decorator(fn):
            return fn
        if args and callable(args[0]):
            return args[0]
        return decorator

    def invalidates_cache(*args, **kwargs):  # type: ignore[misc]
        def decorator(fn):
            return fn
        if args and callable(args[0]):
            return args[0]
        return decorator

from digitorn.modules.base import ActionResult, BaseModule, ExecutionContext, Platform, ResourceEstimate
from digitorn.modules.decorators import ActionEntry, action
from digitorn.modules.executor import ActionTask, ActionTaskResult, ModuleExecutor, gather_actions
from digitorn.modules.manifest import ActionSpec, ModuleManifest
from digitorn.modules.protocol import IModule
from digitorn.modules.registry import ModuleRegistry
from digitorn.modules.types import ModuleState, ModuleType

__all__ = [
    "IModule",
    "BaseModule",
    "Platform",
    "ExecutionContext",
    "ActionResult",
    "ResourceEstimate",
    "action",
    "ActionEntry",
    "ModuleManifest",
    "ActionSpec",
    "ModuleRegistry",
    "ModuleExecutor",
    "ActionTask",
    "ActionTaskResult",
    "gather_actions",
    "ModuleState",
    "ModuleType",
    "cacheable",
    "invalidates_cache",
]
