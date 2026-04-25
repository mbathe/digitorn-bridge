"""Config annotation system for dashboard UI metadata.

Provides:
  - ``ConfigField()`` -- Pydantic ``Field`` wrapper that stores UI metadata
    (label, category, widget hint, restart flag, secret) in ``json_schema_extra``.
  - ``ModuleConfigBase`` -- Base Pydantic model for module configuration.
  - ``configurable()`` -- Class decorator that binds a config model to a module.

Usage::

    from digitorn.module.config import ConfigField, ModuleConfigBase, configurable

    class MyModuleConfig(ModuleConfigBase):
        max_retries: int = ConfigField(3, label="Max Retries", category="network")
        api_key: str = ConfigField("", label="API Key", secret=True)

    @configurable(MyModuleConfig)
    class MyModule(BaseModule):
        ...
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


def ConfigField(
    default: Any = ...,
    *,
    description: str = "",
    label: str = "",
    category: str = "general",
    ui_widget: str = "",
    ui_order: int = 0,
    restart_required: bool = False,
    secret: bool = False,
    ge: float | None = None,
    le: float | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    **kwargs: Any,
) -> Any:
    """Pydantic Field wrapper that adds dashboard UI metadata.

    All standard Pydantic Field args are passed through. The extra
    metadata args are stored in Field's json_schema_extra for
    JSON Schema generation and dashboard form rendering.
    """
    ui_meta: dict[str, Any] = {
        "x-ui-label": label or None,
        "x-ui-category": category,
        "x-ui-widget": ui_widget or None,
        "x-ui-order": ui_order,
        "x-ui-restart-required": restart_required,
        "x-ui-secret": secret,
    }
    ui_meta = {k: v for k, v in ui_meta.items() if v is not None and v is not False and v != 0}

    field_kwargs: dict[str, Any] = {"description": description, **kwargs}
    if default is not ...:
        field_kwargs["default"] = default
    if ge is not None:
        field_kwargs["ge"] = ge
    if le is not None:
        field_kwargs["le"] = le
    if min_length is not None:
        field_kwargs["min_length"] = min_length
    if max_length is not None:
        field_kwargs["max_length"] = max_length
    if ui_meta:
        field_kwargs["json_schema_extra"] = ui_meta

    return Field(**field_kwargs)


class ModuleConfigBase(BaseModel):
    """Base class for module configuration models."""

    model_config = {"extra": "forbid"}

    @classmethod
    def to_config_schema(cls) -> dict[str, Any]:
        """Generate a JSON Schema with UI metadata for the dashboard."""
        return cls.model_json_schema()


def configurable(config_cls: type[ModuleConfigBase]):
    """Class decorator that sets CONFIG_MODEL on a BaseModule subclass."""

    def decorator(module_cls):
        module_cls.CONFIG_MODEL = config_cls
        return module_cls

    return decorator
