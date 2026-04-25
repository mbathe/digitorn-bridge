"""Digitorn — Application YAML compiler and bootstrapper.

Translates declarative app YAML definitions into validated action sequences
and runtime constraints, then executes them against live modules.

Usage::

    from digitorn.core.app import AppYAMLCompiler, AppBootstrapper

    compiler = AppYAMLCompiler(registry)
    compiled = compiler.compile_file(Path("my-app.yaml"))

    bootstrapper = AppBootstrapper(registry, service_bus)
    result = await bootstrapper.bootstrap(compiled)
"""

from digitorn.core.app.bootstrapper import AppBootstrapper, BootstrapResult
from digitorn.core.app.compiler import AppYAMLCompiler, CompiledApp
from digitorn.core.app.runtime import AppRuntimeStore, RuntimeApp
from digitorn.core.app.syncer import AppSyncer
from digitorn.core.app.errors import (
    ActionNotFoundError,
    AppCompilationError,
    AppBootstrapError,
    ConstraintValidationError,
    ModuleNotFoundError,
    ParamsValidationError,
    VariableResolutionError,
)
from digitorn.core.app.schema import AppDefinition
from digitorn.core.app.variables import resolve_variables

__all__ = [
    "AppBootstrapper",
    "AppDefinition",
    "AppYAMLCompiler",
    "BootstrapResult",
    "CompiledApp",
    "ActionNotFoundError",
    "AppBootstrapError",
    "AppCompilationError",
    "AppRuntimeStore",
    "AppSyncer",
    "ConstraintValidationError",
    "RuntimeApp",
    "ModuleNotFoundError",
    "ParamsValidationError",
    "VariableResolutionError",
    "resolve_variables",
]
