"""CredentialSlot - declaration of a module's credential need."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CredentialSlot:
    """One slot a module exposes to consume a credential."""

    id: str

    # Human label shown in the per-app config UI.
    label: str

    handler_types: list[str] = field(default_factory=list)

    providers: list[str] = field(default_factory=list)

    # Preferred scopes (UI sorts the credential picker by this order).
    # First entry = top of the dropdown. Doesn't reject the others.
    scopes_preferred: list[str] = field(default_factory=list)

    scopes_allowed: list[str] | None = None

    inject: dict[str, str] = field(default_factory=dict)

    required: bool = True

    # Optional short help text shown in the UI under the slot label.
    help: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the manifest endpoint."""
        return {
            "id": self.id,
            "label": self.label,
            "handler_types": list(self.handler_types),
            "providers": list(self.providers),
            "scopes_preferred": list(self.scopes_preferred),
            "scopes_allowed": (
                list(self.scopes_allowed)
                if self.scopes_allowed is not None
                else None
            ),
            "inject": dict(self.inject),
            "required": self.required,
            "help": self.help,
        }


def collect_slots_from_modules(
    modules: list[Any],
) -> list[tuple[str, CredentialSlot]]:
    """Walk a list of module instances or classes, return (module_id,"""
    out: list[tuple[str, CredentialSlot]] = []
    for mod in modules:
        slots = getattr(mod, "credential_slots", None) or []
        module_id = getattr(mod, "module_id", None) or getattr(
            type(mod), "__name__", "module",
        )
        for s in slots:
            if isinstance(s, CredentialSlot):
                out.append((module_id, s))
    return out
