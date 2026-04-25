"""Module type system — enums and constants for Module Spec v2.

Defines the fundamental types used across the module lifecycle:
  - ModuleType: system (non-uninstallable) vs user (community-managed)
  - ModuleState: lifecycle state machine states
  - SYSTEM_MODULE_IDS: protected modules that cannot be disabled/uninstalled

All enums use ``StrEnum`` (Python 3.11+) so their values serialize to
plain strings in JSON/YAML without a custom encoder, and comparisons
against string literals work directly (``ModuleState.ACTIVE == "active"``).
"""

from __future__ import annotations

from enum import StrEnum


class ModuleType(StrEnum):
    """Classification of a module's role in the system.

    System modules provide core OS-level capabilities and cannot be
    disabled or uninstalled.  User modules are community-installable
    and can be freely managed at runtime.
    """

    SYSTEM = "system"
    USER = "user"


class ModuleState(StrEnum):
    """Lifecycle state of a module instance.

    State machine::

        LOADED ─→ STARTING ─→ ACTIVE ─→ PAUSED
                                │  ↑       │
                                │  └───────┘ (resume)
                                ↓
                             STOPPING ─→ DISABLED
                                ↓
                              ERROR (on failure at any transition)
    """

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
