"""Structured errors for app YAML compilation and bootstrapping."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class AppCompilationError(Exception):
    """Base error raised when app YAML compilation fails.

    Collects all validation errors so the user sees every problem at once
    rather than fixing them one by one.
    """

    def __init__(self, errors: list[str], *, source: str = "") -> None:
        self.errors = errors
        self.source = source
        detail = "; ".join(errors[:5])
        if len(errors) > 5:
            detail += f" ... and {len(errors) - 5} more"
        super().__init__(f"App compilation failed ({len(errors)} error(s)): {detail}")


class VariableResolutionError(AppCompilationError):
    """One or more ``{{variable}}`` references could not be resolved."""


class ModuleNotFoundError(AppCompilationError):
    """A module referenced in the YAML is not loaded in the registry."""


class ActionNotFoundError(AppCompilationError):
    """An action referenced in ``setup`` does not exist on the target module."""


class ParamsValidationError(AppCompilationError):
    """Action parameters failed Pydantic validation against the params_model."""


class ConstraintValidationError(AppCompilationError):
    """A constraint key is not declared in the module's ``supported_constraints``."""


class AppBootstrapError(Exception):
    """Raised when bootstrapping a compiled app fails at runtime."""

    def __init__(
        self,
        message: str,
        *,
        step_errors: list[dict[str, Any]] | None = None,
    ) -> None:
        self.step_errors = step_errors or []
        super().__init__(message)
