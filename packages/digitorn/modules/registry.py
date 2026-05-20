"""Module layer - Module registry."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Type

from digitorn.modules.exceptions import ModuleLoadError, ModuleNotFoundError
from digitorn.modules.log import get_logger
from digitorn.modules.manifest import ModuleManifest

if TYPE_CHECKING:
    from digitorn.modules.base import BaseModule
    from digitorn.modules.platform_guard import PlatformGuard

log = get_logger(__name__)

class ModuleRegistry:
    """Runtime registry for Digitorn modules."""

    def __init__(self, platform_guard: "PlatformGuard | None" = None) -> None:
        self._classes: dict[str, Type["BaseModule"]] = {}
        self._instances: dict[str, "BaseModule"] = {}
        self._failed: dict[str, str] = {}
        self._platform_excluded: dict[str, str] = {}
        self._guard = platform_guard
        self._lifecycle: Any | None = None

    def set_lifecycle_manager(self, manager: Any) -> None:
        """Attach a ModuleLifecycleManager to this registry."""
        self._lifecycle = manager

    @property
    def lifecycle(self) -> Any | None:
        """Return the attached ModuleLifecycleManager, or None."""
        return self._lifecycle

    def register(self, module_class: Type["BaseModule"]) -> None:
        """Register a module class.  Instantiation is deferred until first use."""
        module_id = module_class.MODULE_ID
        if not module_id:
            raise ValueError(f"Module class {module_class.__name__} has no MODULE_ID.")

        if self._guard is not None and not self._guard.is_compatible(module_class):
            reason = (
                f"Platform '{self._guard.platform_info.os_type.value}' is not in "
                f"SUPPORTED_PLATFORMS {[p.value for p in module_class.SUPPORTED_PLATFORMS]}."
            )
            self._platform_excluded[module_id] = reason
            log.info(
                "module_platform_excluded",
                module_id=module_id,
                reason=reason,
            )
            return

        if module_id in self._classes:
            log.warning("module_already_registered", module_id=module_id)

        # Modules with @action tools should provide get_prompt_sections()
        # so the LLM receives usage instructions automatically.
        _has_actions = bool(getattr(module_class, "_action_registry", None))
        _overrides_prompt = (
            "get_prompt_sections" in module_class.__dict__
            or any(
                "get_prompt_sections" in klass.__dict__
                for klass in module_class.__mro__[1:]
                if klass.__name__ not in ("BaseModule", "IModule", "ABC", "object")
            )
        )
        if _has_actions and not _overrides_prompt and module_class.MODULE_TYPE != "system":
            log.debug(
                "module_missing_prompt_sections: %s has %d actions but does not "
                "override get_prompt_sections(). The LLM won't receive usage "
                "instructions for this module's tools.",
                module_id,
                len(module_class._action_registry),
            )

        self._classes[module_id] = module_class
        log.debug("module_registered", module_id=module_id, version=module_class.VERSION)

    def get(self, module_id: str) -> "BaseModule":
        """Return the module instance for *module_id*."""
        if module_id in self._platform_excluded:
            raise ModuleLoadError(
                module_id=module_id,
                reason=self._platform_excluded[module_id],
            )
        if module_id in self._failed:
            raise ModuleLoadError(module_id=module_id, reason=self._failed[module_id])

        if module_id not in self._instances:
            self._instances[module_id] = self._instantiate(module_id)

        return self._instances[module_id]

    def create(self, module_id: str) -> "BaseModule":
        """Create a **fresh** module instance (no caching)."""
        if module_id in self._platform_excluded:
            raise ModuleLoadError(
                module_id=module_id,
                reason=self._platform_excluded[module_id],
            )
        if module_id in self._failed:
            raise ModuleLoadError(module_id=module_id, reason=self._failed[module_id])

        return self._instantiate(module_id)

    def _instantiate(self, module_id: str) -> "BaseModule":
        if module_id not in self._classes:
            raise ModuleNotFoundError(module_id=module_id)

        module_class = self._classes[module_id]
        try:
            instance = module_class()
            log.info("module_loaded: %s v%s", module_id, module_class.VERSION)
            return instance
        except BaseException as exc:
            reason = str(exc) if str(exc) else type(exc).__name__
            self._failed[module_id] = reason
            log.error("module_load_failed: %s - %s", module_id, reason)
            raise ModuleLoadError(module_id=module_id, reason=reason) from exc

    def register_instance(self, instance: "BaseModule") -> None:
        """Register a pre-constructed module instance directly."""
        module_id = instance.MODULE_ID
        if not module_id:
            raise ValueError(f"Module instance {type(instance).__name__} has no MODULE_ID.")
        self._classes[module_id] = type(instance)
        self._instances[module_id] = instance
        log.debug("module_instance_registered", module_id=module_id, version=instance.VERSION)

    def register_isolated(
        self,
        module_id: str,
        module_class_path: str,
        venv_manager: "Any",
        requirements: "list[str] | None" = None,
        env_vars: "dict[str, str] | None" = None,
        source_path: "Path | None" = None,
        timeout: float = 30.0,
        max_restarts: int = 3,
    ) -> None:
        """Register a module that runs in an isolated subprocess."""
        from digitorn.isolation.proxy import IsolatedModuleProxy

        merged_env: dict[str, str] = dict(env_vars or {})
        if source_path is not None:
            source_str = str(source_path)
            existing = merged_env.get("PYTHONPATH", "")
            merged_env["PYTHONPATH"] = (
                f"{source_str}{os.pathsep}{existing}" if existing else source_str
            )

        proxy = IsolatedModuleProxy(
            module_id=module_id,
            module_class_path=module_class_path,
            venv_manager=venv_manager,
            requirements=requirements,
            env_vars=merged_env if merged_env else None,
            timeout=timeout,
            max_restarts=max_restarts,
            source_path=source_path,
        )
        self.register_instance(proxy)
        log.info(
            "module_registered_isolated",
            module_id=module_id,
            class_path=module_class_path,
            source_path=str(source_path) if source_path else None,
        )

    def is_available(self, module_id: str) -> bool:
        """Return True if the module is registered and can be instantiated."""
        if module_id in self._failed or module_id in self._platform_excluded:
            return False
        if module_id not in self._classes:
            return False
        try:
            self.get(module_id)
            return True
        except (ModuleNotFoundError, ModuleLoadError):
            return False

    def list_modules(self) -> list[str]:
        """Return IDs of all registered modules (including failed and excluded)."""
        all_ids = set(self._classes.keys()) | set(self._platform_excluded.keys())
        return sorted(all_ids)

    def list_available(self) -> list[str]:
        """Return IDs of modules that loaded successfully."""
        return [
            mid for mid in self._classes
            if mid not in self._failed and mid not in self._platform_excluded
        ]

    def list_failed(self) -> dict[str, str]:
        """Return module_id -> reason for modules that failed to load at runtime."""
        return dict(self._failed)

    def list_platform_excluded(self) -> dict[str, str]:
        """Return module_id -> reason for modules excluded due to platform mismatch."""
        return dict(self._platform_excluded)

    def get_manifest(self, module_id: str) -> ModuleManifest:
        return self.get(module_id).get_manifest()

    def all_manifests(self) -> list[ModuleManifest]:
        manifests = []
        for module_id in self.list_available():
            try:
                manifests.append(self.get_manifest(module_id))
            except Exception as exc:
                log.warning("manifest_fetch_failed", module_id=module_id, error=str(exc))
        return manifests

    def unregister(self, module_id: str) -> None:
        """Remove a module from the registry (used in tests)."""
        self._classes.pop(module_id, None)
        self._instances.pop(module_id, None)
        self._failed.pop(module_id, None)
        self._platform_excluded.pop(module_id, None)

    def status_report(self) -> dict[str, dict[str, str | list[str]]]:
        """Return a structured status report for the health endpoint."""
        return {
            "available": self.list_available(),
            "failed": self.list_failed(),
            "platform_excluded": self.list_platform_excluded(),
        }
