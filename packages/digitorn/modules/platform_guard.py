"""Module layer — Platform detection and compatibility guards.

This module provides:

1. **PlatformInfo** — Detects the current OS and hardware at startup, including
   Raspberry Pi detection via ``/proc/cpuinfo``.

2. **PlatformGuard** — Validates that a module's declared ``SUPPORTED_PLATFORMS``
   includes the current platform before allowing it to load.  Raises
   ``ModuleLoadError`` for incompatible modules.

3. **PLATFORM_COMPATIBILITY_MATRIX** — Documents which built-in modules are
   available on each platform, used to generate diagnostic messages and to
   drive graceful degradation in the registry.

Usage::

    info = PlatformInfo.detect()

    guard = PlatformGuard(info)
    guard.assert_compatible(filesystem_module)
    guard.assert_compatible(iot_module)

    if guard.is_compatible(excel_module):
        registry.register(ExcelModule)

Design decisions:
  - Detection happens once at startup and is cached on the PlatformInfo
    singleton.  Do not re-detect per-request.
  - We detect Raspberry Pi by checking ``/proc/cpuinfo`` for the
    ``Raspberry Pi`` model string.  This is the standard approach and works
    across all Pi generations.
  - The compatibility matrix is advisory — modules can still declare
    ``SUPPORTED_PLATFORMS = [Platform.ALL]`` to bypass it.  The matrix is
    used for diagnostic messages and health endpoint reporting.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from digitorn.modules.exceptions import ModuleLoadError
from digitorn.modules.base import BaseModule, Platform


@dataclass(frozen=True)
class PlatformInfo:
    """Immutable snapshot of the current platform.

    Use :meth:`detect` to create an instance; do not instantiate directly.
    """

    os_type: Platform
    os_name: str
    os_version: str
    python_version: str
    is_raspberry_pi: bool
    architecture: str
    extra: dict[str, str] = field(default_factory=dict)

    _cache: ClassVar[PlatformInfo | None] = None

    @classmethod
    def detect(cls) -> "PlatformInfo":
        """Detect and cache the current platform.

        Returns:
            A frozen :class:`PlatformInfo` instance.
        """
        if cls._cache is not None:
            return cls._cache

        os_name = platform.system()
        os_version = platform.release()
        python_version = platform.python_version()
        architecture = platform.machine()
        is_rpi = _detect_raspberry_pi()

        if is_rpi:
            os_type = Platform.RASPBERRY_PI
        elif os_name == "Linux":
            os_type = Platform.LINUX
        elif os_name == "Windows":
            os_type = Platform.WINDOWS
        elif os_name == "Darwin":
            os_type = Platform.MACOS
        else:
            os_type = Platform.LINUX

        info = cls(
            os_type=os_type,
            os_name=os_name,
            os_version=os_version,
            python_version=python_version,
            is_raspberry_pi=is_rpi,
            architecture=architecture,
        )
        object.__setattr__(cls, "_cache", info)
        return info

    @classmethod
    def reset_cache(cls) -> None:
        """Clear the cached platform info.  Useful in tests."""
        object.__setattr__(cls, "_cache", None)

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "os_type": self.os_type.value,
            "os_name": self.os_name,
            "os_version": self.os_version,
            "python_version": self.python_version,
            "is_raspberry_pi": self.is_raspberry_pi,
            "architecture": self.architecture,
        }


def _detect_raspberry_pi() -> bool:
    """Return True if we are running on a Raspberry Pi."""
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        try:
            content = cpuinfo.read_text(errors="replace")
            if "Raspberry Pi" in content:
                return True
        except OSError:
            pass

    model_file = Path("/proc/device-tree/model")
    if model_file.exists():
        try:
            content = model_file.read_text(errors="replace")
            if "Raspberry Pi" in content:
                return True
        except OSError:
            pass

    return False


class PlatformGuard:
    """Validates module platform compatibility before loading.

    Usage::

        guard = PlatformGuard()
        guard = PlatformGuard(platform_info)

    Args:
        platform_info: Optional pre-detected :class:`PlatformInfo`.  If not
            provided, :meth:`PlatformInfo.detect` is called automatically.
    """

    def __init__(self, platform_info: PlatformInfo | None = None) -> None:
        self._info = platform_info or PlatformInfo.detect()

    @property
    def platform_info(self) -> PlatformInfo:
        return self._info

    def is_compatible(self, module: type[BaseModule] | BaseModule) -> bool:
        """Return True if the module supports the current platform."""
        supported = module.SUPPORTED_PLATFORMS
        if Platform.ALL in supported:
            return True
        return self._info.os_type in supported

    def assert_compatible(self, module: type[BaseModule] | BaseModule) -> None:
        """Raise :class:`~digitorn.module.exceptions.ModuleLoadError` if incompatible.

        Args:
            module: A :class:`BaseModule` subclass or instance.

        Raises:
            ModuleLoadError: If the module does not support the current OS.
        """
        if not self.is_compatible(module):
            module_id = getattr(module, "MODULE_ID", str(module))
            supported = [p.value for p in module.SUPPORTED_PLATFORMS]
            raise ModuleLoadError(
                module_id=module_id,
                reason=(
                    f"Module '{module_id}' requires platform(s) {supported} "
                    f"but the current platform is '{self._info.os_type.value}' "
                    f"({self._info.os_name} {self._info.os_version})."
                ),
            )

    def filter_compatible(
        self, module_classes: list[type[BaseModule]]
    ) -> list[type[BaseModule]]:
        """Return only the modules compatible with the current platform."""
        return [cls for cls in module_classes if self.is_compatible(cls)]


PLATFORM_COMPATIBILITY_MATRIX: dict[str, set[Platform]] = {
    "filesystem": {Platform.LINUX, Platform.WINDOWS, Platform.MACOS, Platform.RASPBERRY_PI},
    "os_exec": {Platform.LINUX, Platform.WINDOWS, Platform.MACOS, Platform.RASPBERRY_PI},
    "api_http": {Platform.LINUX, Platform.WINDOWS, Platform.MACOS, Platform.RASPBERRY_PI},
    "database": {Platform.LINUX, Platform.WINDOWS, Platform.MACOS},
    "excel": {Platform.LINUX, Platform.WINDOWS, Platform.MACOS},
    "word": {Platform.LINUX, Platform.WINDOWS, Platform.MACOS},
    "browser": {Platform.LINUX, Platform.WINDOWS, Platform.MACOS},
    "gui": {Platform.LINUX, Platform.WINDOWS, Platform.MACOS},
    "iot": {Platform.RASPBERRY_PI, Platform.LINUX},
}


def get_module_platforms(module_id: str) -> set[Platform]:
    """Return the set of platforms for *module_id* from the compatibility matrix.

    Falls back to ``{Platform.ALL}`` if the module is not listed.
    """
    return PLATFORM_COMPATIBILITY_MATRIX.get(module_id, {Platform.ALL})


def list_available_modules_for_platform(platform_info: PlatformInfo) -> list[str]:
    """Return module IDs available on *platform_info*.

    Modules not in the matrix are assumed available everywhere.
    """
    result: list[str] = []
    for module_id, platforms in PLATFORM_COMPATIBILITY_MATRIX.items():
        if Platform.ALL in platforms or platform_info.os_type in platforms:
            result.append(module_id)
    return sorted(result)
