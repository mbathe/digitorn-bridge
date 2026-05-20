"""Module type system - enums and constants for Module Spec v2."""

from __future__ import annotations

from enum import StrEnum

class ModuleType(StrEnum):
    """Classification of a module's role in the system."""

    SYSTEM = "system"
    USER = "user"

class ModuleState(StrEnum):
    """Lifecycle state of a module instance."""

    LOADED = "loaded"
    STARTING = "starting"
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPING = "stopping"
    DISABLED = "disabled"
    ERROR = "error"

SYSTEM_MODULE_IDS: frozenset[str] = frozenset({
    "filesystem",
    "os_exec",
    "security",
    "module_manager",
})

VALID_TRANSITIONS: dict[ModuleState, set[ModuleState]] = {
    ModuleState.LOADED:    {ModuleState.STARTING, ModuleState.ERROR},
    ModuleState.STARTING:  {ModuleState.ACTIVE,   ModuleState.ERROR},
    ModuleState.ACTIVE:    {ModuleState.PAUSED,   ModuleState.STOPPING, ModuleState.ERROR},
    ModuleState.PAUSED:    {ModuleState.STARTING, ModuleState.STOPPING, ModuleState.ERROR},
    ModuleState.STOPPING:  {ModuleState.DISABLED, ModuleState.ERROR},
    ModuleState.DISABLED:  {ModuleState.STARTING, ModuleState.ERROR},
    ModuleState.ERROR:     {ModuleState.STARTING},
}
