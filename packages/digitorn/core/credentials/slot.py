"""CredentialSlot - declaration of a module's credential need.

A module that consumes a credential at runtime declares one or more
`CredentialSlot` instances on its class. The compiler reads these to:

  1. Generate the app's credential manifest (`/api/apps/{id}/credentials/manifest`).
  2. Validate that the YAML's `credential: <ref>` references a slot
     compatible with what the user picked (right handler_type, right
     scope).
  3. Drive the runtime injector to put the resolved credential
     fields at the expected paths in the module's config.

Example - the LLM provider module declares one slot per agent's brain:

    class LlmProviderModule(BaseModule):
        credential_slots = [
            CredentialSlot(
                id="brain",
                label="LLM provider authentication",
                handler_types=["api_key", "oauth2"],
                providers=["openai", "anthropic", "deepseek", "google_oauth"],
                scopes_preferred=["per_user", "system_wide"],
                scopes_allowed=None,                  # all from handler
                inject={
                    "api_key": "{block}.config.api_key",
                    "access_token": "{block}.config.api_key",
                },
                required=True,
            ),
        ]

The compiler matches the user's chosen credential against the slot:
  - User picks an `api_key` cred → slot accepts it (in handler_types).
  - User picks an `aws_access_key` cred → rejected.
  - User picks `system_wide` scope but slot scopes_allowed restricts
    to `per_user` only → rejected.

The `inject` dict maps each FIELD of the credential to a target path
in the module's compiled config. Path templating uses `{block}` =
the YAML block this slot belongs to (e.g. `agents.main.brain`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CredentialSlot:
    """One slot a module exposes to consume a credential.

    A module class lists its slots in `credential_slots = [...]`.
    """

    # Stable identifier within the module. Must be unique per module.
    # The compiler uses this in the manifest as the slot key, and
    # the YAML can reference `slot: brain` to bind a credential.
    id: str

    # Human label shown in the per-app config UI.
    label: str

    # Which handler types the slot accepts. Empty = accept any.
    # Example: ["api_key", "oauth2"] for LLM brain (some providers
    # use api_key, some use OAuth).
    handler_types: list[str] = field(default_factory=list)

    # Which named providers from the catalog the slot accepts. Empty
    # = accept any provider compatible with `handler_types`. Use this
    # to constrain to vetted providers (e.g. only `openai` and
    # `anthropic`, never random custom api_keys).
    providers: list[str] = field(default_factory=list)

    # Preferred scopes (UI sorts the credential picker by this order).
    # First entry = top of the dropdown. Doesn't reject the others.
    scopes_preferred: list[str] = field(default_factory=list)

    # Hard whitelist of scopes the slot accepts. None = inherit from
    # the handler's `allowed_scopes`. Use to narrow further (e.g. a
    # module that REQUIRES per_user even when the handler allows
    # system_wide).
    scopes_allowed: list[str] | None = None

    # Mapping `field_name → target_path`. After resolution at runtime,
    # for each entry the resolver writes
    # `credential.fields[field_name]` to the path in the compiled
    # module config. Path templating: `{block}` = the YAML location
    # of the block (e.g. `agents.main.brain` or `modules.git`).
    #
    # Example:
    #   inject={"api_key": "{block}.config.api_key"}
    #
    # If the credential has fields the inject dict doesn't mention,
    # they are ignored. If a field listed in `inject` is missing from
    # the credential, the injector logs a warning and continues -
    # the module receives whatever was actually filled.
    inject: dict[str, str] = field(default_factory=dict)

    # If `required=True`, the app cannot deploy / activate when this
    # slot has no compatible credential resolved for the current user.
    # `required=False` = the module degrades gracefully without it
    # (e.g. an optional analytics integration).
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
    """Walk a list of module instances or classes, return (module_id,
    slot) pairs for all declared slots.

    Used by the compiler to build the app's manifest. Tolerates
    modules without `credential_slots` (returns nothing for them) so
    legacy modules that haven't been migrated yet don't crash the
    compile.
    """
    out: list[tuple[str, CredentialSlot]] = []
    for mod in modules:
        slots = getattr(mod, "credential_slots", None) or []
        # `module_id` may live on instance or class. Prefer instance
        # attribute when present (so dynamic instances stay
        # identifiable).
        module_id = getattr(mod, "module_id", None) or getattr(
            type(mod), "__name__", "module",
        )
        for s in slots:
            if isinstance(s, CredentialSlot):
                out.append((module_id, s))
    return out
