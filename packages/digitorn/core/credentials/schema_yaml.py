"""Pydantic models for the YAML `credential:` block."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# Same set as `digitorn.core.credentials.store.Scope.ALL`. Hard-coded
# here to avoid a circular import; kept in sync via test.
_ALL_SCOPES = ("system_wide", "per_app_shared", "per_user", "per_app_per_user")
_SCOPE_LITERAL = Literal[
    "system_wide", "per_app_shared", "per_user", "per_app_per_user",
]

_REF_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class CredentialReference(BaseModel):
    """One `credential:` reference inline in an app.yaml block."""

    model_config = {"extra": "forbid"}

    ref: str = Field(
        ...,
        description=(
            "Stable name of the credential in the user's vault. "
            "Slug format: lowercase alphanumeric + underscore/dash, "
            "1-64 characters."
        ),
    )
    scope: _SCOPE_LITERAL = Field(
        ...,
        description=(
            "Mandatory. Which scope to look the credential up at. "
            "OAuth handlers ignore non-per_user scopes (compiler "
            "rejects mismatch)."
        ),
    )
    provider: str | None = Field(
        default=None,
        description=(
            "Optional sanity check: name of the provider template this "
            "credential MUST match (e.g. 'openai'). Useful when a slot "
            "accepts multiple providers and you want to lock to one."
        ),
    )

    @field_validator("ref", mode="after")
    @classmethod
    def _validate_ref(cls, v: str) -> str:
        if not _REF_RE.match(v):
            raise ValueError(
                f"credential ref {v!r} is not a valid slug "
                f"(must match ^[a-z][a-z0-9_-]{{0,63}}$)",
            )
        return v


def parse_credential_ref(raw: Any) -> CredentialReference | None:
    """Coerce a YAML value into a CredentialReference, or None."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        return CredentialReference(**raw)
    if isinstance(raw, str):
        # Compact form. We default scope to per_user as the most common
        # case; a compiler warning will recommend the explicit shape.
        return CredentialReference(ref=raw, scope="per_user")
    raise ValueError(
        f"credential reference must be a string or a mapping, "
        f"got {type(raw).__name__}",
    )


class CredentialsManifestEntry(BaseModel):
    """One entry in the per-app credentials manifest."""

    block: str = Field(..., description="Dotted YAML path of the consumer block.")
    slot_id: str = Field(..., description="The CredentialSlot.id within that module.")
    label: str = Field(..., description="Human label from the slot.")
    handler_types: list[str] = Field(default_factory=list)
    providers_accepted: list[str] = Field(default_factory=list)
    scopes_preferred: list[str] = Field(default_factory=list)
    scopes_allowed: list[str] | None = Field(default=None)
    required: bool = True
    help: str = ""

    # The reference declared in the YAML (may be None if the YAML
    # didn't bind anything yet - the user picks via the UI).
    ref: str | None = Field(default=None)
    ref_scope: str | None = Field(default=None)
    ref_provider: str | None = Field(default=None)

    # Resolution status, computed for the calling user at request time.
    resolved: bool = False
    resolved_credential_id: str | None = None
    resolved_status: str | None = None  # filled / valid / expired / invalid
    resolution_error: str | None = None

    # Compatible alternatives in the user's vault (for the UI's picker).
    available: list[dict[str, Any]] = Field(default_factory=list)


class CredentialsAppManifest(BaseModel):
    """Full manifest for one app: all credential needs + their state."""

    app_id: str
    user_id: str | None = None
    entries: list[CredentialsManifestEntry] = Field(default_factory=list)
    # Aggregate flags for the UI's "can I activate this app yet" gate.
    all_required_resolved: bool = False
    missing_required: list[str] = Field(default_factory=list)


def all_scopes() -> tuple[str, ...]:
    """The 4 valid scope values - exported for tests / catalog code."""
    return _ALL_SCOPES
