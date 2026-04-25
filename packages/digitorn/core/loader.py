"""Digitorn — Module loader.

Scans module directories, reads digitorn-module.toml manifests,
dynamically imports module classes, validates them, and registers
them in the ModuleRegistry.

Supports two isolation modes declared in the TOML:
    shared  — loaded in-process (default, fast)
    process — loaded in a subprocess with its own venv
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from digitorn.core.paths import all_module_dirs
from digitorn.modules.base import BaseModule
from digitorn.modules.exceptions import ModuleLoadError
from digitorn.modules.registry import ModuleRegistry

logger = structlog.get_logger(__name__)

TOML_NAMES = ("digitorn-module.toml",)


@dataclass
class ModuleDescriptor:
    """Parsed metadata from a module's TOML file."""

    module_id: str
    version: str
    description: str = ""
    author: str = ""
    module_class_path: str = ""
    isolation: str = "shared"
    platforms: list[str] = field(default_factory=lambda: ["all"])
    requirements: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    path: Path = field(default_factory=Path)
    docs: dict[str, str] = field(default_factory=dict)


def _find_toml(module_dir: Path) -> Path | None:
    """Find the TOML manifest in a module directory."""
    for name in TOML_NAMES:
        toml_path = module_dir / name
        if toml_path.exists():
            return toml_path
    return None


def _parse_toml(toml_path: Path) -> ModuleDescriptor:
    """Parse a module TOML file into a ModuleDescriptor."""
    with toml_path.open("rb") as f:
        data = tomllib.load(f)

    module_data = data.get("module", {})

    docs_data = module_data.get("docs", {})
    docs = {
        "readme": docs_data.get("readme", "README.md"),
        "actions": docs_data.get("actions", "docs/actions.md"),
        "integration": docs_data.get("integration", "docs/integration.md"),
        "app_config": docs_data.get("app_config", "docs/app-config.yaml"),
    }
    for key, value in docs_data.items():
        if key not in docs:
            docs[key] = value

    return ModuleDescriptor(
        module_id=module_data["module_id"],
        version=module_data.get("version", "0.0.0"),
        description=module_data.get("description", ""),
        author=module_data.get("author", ""),
        module_class_path=module_data.get("module_class_path", "") or module_data.get("entry_point", ""),
        isolation=module_data.get("isolation", "shared"),
        platforms=module_data.get("platforms", ["all"]),
        requirements=module_data.get("requirements", []),
        tags=module_data.get("tags", []),
        enabled=module_data.get("enabled", True),
        path=toml_path.parent,
        docs=docs,
    )


def _validate_docs(descriptor: ModuleDescriptor) -> list[str]:
    """Validate that required documentation files exist.

    Returns:
        List of validation errors. Empty = all good.
    """
    errors: list[str] = []
    module_dir = descriptor.path

    readme_path = module_dir / descriptor.docs.get("readme", "README.md")
    if not readme_path.exists():
        errors.append(f"Missing required {readme_path.name} in {module_dir}")

    actions_path = module_dir / descriptor.docs.get("actions", "docs/actions.md")
    if not actions_path.exists():
        errors.append(f"Missing required {actions_path.name} in {module_dir}")

    # integration.md and docs/app-config.yaml are explicitly optional.
    # Emitting a WARNING for missing optional files on every boot pollutes
    # the daemon log without giving the operator anything actionable —
    # downgraded to DEBUG. Run with ``log_level=DEBUG`` to surface them.
    integration_path = module_dir / descriptor.docs.get("integration", "docs/integration.md")
    if not integration_path.exists():
        logger.debug(
            "module_doc_missing",
            module_id=descriptor.module_id,
            file=str(integration_path),
            hint="Recommended but not required.",
        )

    app_config_path = module_dir / descriptor.docs.get("app_config", "docs/app-config.yaml")
    if not app_config_path.exists():
        logger.debug(
            "module_doc_missing",
            module_id=descriptor.module_id,
            file=str(app_config_path),
            hint="Recommended: example YAML for app configuration.",
        )

    return errors


def _validate_params_exposed(cls: type[BaseModule], module_id: str) -> list[str]:
    """Check that every @action in the module declares a params_model.

    Modules that do not expose their parameter schemas cannot be used by
    LLM agents — the agent would have no way to know which parameters
    to pass.  This is a hard requirement: modules without proper param
    schemas are rejected at load time.

    Returns:
        List of error strings.  Empty = all good.
    """
    registry: dict[str, Any] = getattr(cls, "_action_registry", {})
    if not registry:
        return []  # No @action-decorated methods — nothing to check

    errors: list[str] = []
    for name, entry in registry.items():
        if not hasattr(entry, "params_model") or entry.params_model is None:
            errors.append(
                f"Action '{name}' has no params_model — "
                f"LLM agents cannot discover its parameters"
            )
    return errors


def _import_module_class(descriptor: ModuleDescriptor) -> type[BaseModule]:
    """Dynamically import the module class from its class path.

    If module_class_path is set (e.g. "digitorn.modules.filesystem.module:FilesystemModule"),
    use it directly. Otherwise, import module.py from the module directory
    and find the first BaseModule subclass.
    """
    if descriptor.module_class_path:
        module_path, class_name = descriptor.module_class_path.rsplit(":", 1)
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
    else:
        module_py = descriptor.path / "module.py"
        if not module_py.exists():
            raise ModuleLoadError(
                module_id=descriptor.module_id,
                reason=f"Cannot find module.py in {descriptor.path}",
            )

        module_dir = str(descriptor.path)
        added = module_dir not in sys.path
        if added:
            sys.path.insert(0, module_dir)
        try:
            spec = importlib.util.spec_from_file_location(
                f"digitorn_module_{descriptor.module_id}",
                module_py,
            )
            if spec is None or spec.loader is None:
                raise ModuleLoadError(
                    module_id=descriptor.module_id,
                    reason=f"Cannot load module.py from {descriptor.path}",
                )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            cls = None
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BaseModule)
                    and attr is not BaseModule
                ):
                    cls = attr
                    break

            if cls is None:
                raise ModuleLoadError(
                    module_id=descriptor.module_id,
                    reason=f"No BaseModule subclass found in {module_py}",
                )
        finally:
            if added:
                sys.path.remove(module_dir)

    if not issubclass(cls, BaseModule):
        raise ModuleLoadError(
            module_id=descriptor.module_id,
            reason=f"{cls.__name__} does not inherit from BaseModule",
        )

    return cls


def discover_modules(
    extra_dirs: list[Path] | None = None,
) -> dict[str, ModuleDescriptor]:
    """Scan all module directories and return discovered descriptors.

    Higher-priority directories override lower ones (user > system > builtin).
    """
    descriptors: dict[str, ModuleDescriptor] = {}

    dirs = all_module_dirs()
    if extra_dirs:
        dirs.extend(extra_dirs)

    for base_dir in dirs:
        if not base_dir.exists():
            continue

        for child in sorted(base_dir.iterdir()):
            if not child.is_dir():
                continue

            toml_path = _find_toml(child)
            if toml_path is None:
                continue

            try:
                descriptor = _parse_toml(toml_path)
                descriptors[descriptor.module_id] = descriptor
            except Exception as exc:
                logger.warning(
                    "module_toml_parse_error",
                    path=str(toml_path),
                    error=str(exc),
                )

    return descriptors


def load_modules(
    registry: ModuleRegistry,
    extra_dirs: list[Path] | None = None,
    enabled: list[str] | None = None,
    disabled: list[str] | None = None,
    load_all: bool = True,
) -> dict[str, str]:
    """Discover, validate, and load all modules into the registry.

    Args:
        registry:   The ModuleRegistry to populate.
        extra_dirs: Additional directories to scan.
        enabled:    If set, only load these module IDs.
        disabled:   Module IDs to skip (overrides enabled and TOML enabled).
        load_all:   If True (default), load all discovered modules that have
                    enabled=true in their TOML. If False, only load modules
                    explicitly listed in ``enabled``.

    Returns:
        Dict of module_id -> status ("loaded", "isolated", "skipped", "error: ...").
    """
    descriptors = discover_modules(extra_dirs=extra_dirs)
    results: dict[str, str] = {}
    disabled_set = set(disabled or [])
    enabled_set = set(enabled or [])

    for module_id, descriptor in descriptors.items():
        if module_id in disabled_set:
            results[module_id] = "skipped"
            continue

        explicitly_enabled = module_id in enabled_set

        if not explicitly_enabled:
            if not load_all:
                results[module_id] = "skipped"
                continue

            if not descriptor.enabled:
                results[module_id] = "disabled"
                logger.info(
                    "module_disabled",
                    module_id=module_id,
                    hint="Set enabled=true in TOML or add to config enabled list.",
                )
                continue

        try:
            doc_errors = _validate_docs(descriptor)
            if doc_errors:
                raise ModuleLoadError(
                    module_id=module_id,
                    reason="Documentation missing: " + "; ".join(doc_errors),
                )

            if descriptor.isolation == "process":
                try:
                    from digitorn.isolation.proxy import IsolatedModuleProxy  # noqa: F401
                except ImportError:
                    raise ModuleLoadError(
                        module_id=module_id,
                        reason="isolation='process' requires the isolation subsystem (not yet available).",
                    )
                registry.register_isolated(
                    module_id=module_id,
                    module_class_path=descriptor.module_class_path,
                    venv_manager=None,
                    requirements=descriptor.requirements or None,
                    source_path=descriptor.path,
                )
                results[module_id] = "isolated"
            else:
                cls = _import_module_class(descriptor)

                param_errors = _validate_params_exposed(cls, module_id)
                if param_errors:
                    raise ModuleLoadError(
                        module_id=module_id,
                        reason=(
                            "Module actions missing params_model — "
                            "agents cannot discover parameters:\n  "
                            + "\n  ".join(param_errors)
                        ),
                    )

                registry.register(cls)
                registry.get(module_id)  # force instantiation + cache in memory
                results[module_id] = "loaded"

            logger.info(
                "module_loaded",
                module_id=module_id,
                version=descriptor.version,
                isolation=descriptor.isolation,
            )

        except Exception as exc:
            error_msg = str(exc) if str(exc) else type(exc).__name__
            results[module_id] = f"error: {error_msg}"
            logger.error(
                "module_load_failed",
                module_id=module_id,
                error=error_msg,
            )

    return results
