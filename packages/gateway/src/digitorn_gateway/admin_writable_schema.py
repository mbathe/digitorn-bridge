"""Pydantic request / response schemas for the writable admin
endpoints. Keep aligned with the SQLAlchemy models in
``models_db.py`` and with the TypeScript types under
``astro-quickstart-buddy/src/lib/api/config.ts``.

The credential endpoints split the contract:
  * the WRITE side accepts ``raw_value`` (the cleartext key the operator
    pasted),
  * the READ side returns ``masked_value`` only - the plaintext is
    encrypted with the gateway master key the moment it lands in
    the route handler and only ever decrypted at LLM dispatch time.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ── Providers ───────────────────────────────────────────────────────


class ProviderIn(BaseModel):
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_\-]+$")
    name: str = Field(min_length=1, max_length=128)
    base_url: str | None = None
    compat: str = Field(default="openai", max_length=32)
    env_var: str | None = Field(default=None, max_length=64)
    auth_type: str = Field(default="api_key", max_length=32)
    auth_schema: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: str | None = None
    compat: str | None = Field(default=None, max_length=32)
    env_var: str | None = Field(default=None, max_length=64)
    auth_type: str | None = Field(default=None, max_length=32)
    auth_schema: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None


class ProviderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    name: str
    base_url: str | None
    compat: str
    env_var: str | None
    auth_type: str = "api_key"
    auth_schema: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(alias="extra_metadata")
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    # Live status (set by the route handler after consulting the env +
    # the credentials table).
    configured: bool = False
    credential_count: int = 0


# ── Credentials ─────────────────────────────────────────────────────


class CredentialCreateIn(BaseModel):
    """Either ``raw_value`` (single-string back-compat shortcut) OR
    ``secret_data`` (multi-field) must be supplied. The route validates
    against the provider's ``auth_schema`` before storing."""

    provider_slug: str
    label: str = Field(min_length=1, max_length=128)
    raw_value: str | None = None
    secret_data: dict[str, str] | None = None


class CredentialRotateIn(BaseModel):
    raw_value: str | None = None
    secret_data: dict[str, str] | None = None


class CredentialPatchIn(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=128)
    status: str | None = Field(default=None, pattern=r"^(active|disabled)$")


class CredentialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider_slug: str
    label: str
    # ``masked_value`` is the legacy single-string preview; new code
    # should look at ``masked_fields`` which renders one preview per
    # auth_schema field (so the dashboard can show "access_token: ***",
    # "refresh_token: ***" instead of just one row).
    masked_value: str
    masked_fields: dict[str, str] = Field(default_factory=dict)
    status: str
    last_used_at: datetime | None
    created_at: datetime
    created_by: str | None


# ── Models (catalogue) ──────────────────────────────────────────────


class ModelIn(BaseModel):
    alias: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_./\-]+$")
    provider_slug: str
    real_model_id: str = Field(min_length=1)
    cost_per_1k_input_tokens: float = 0.0
    cost_per_1k_output_tokens: float = 0.0
    max_context_tokens: int | None = None
    is_custom: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelUpdate(BaseModel):
    provider_slug: str | None = None
    real_model_id: str | None = None
    cost_per_1k_input_tokens: float | None = None
    cost_per_1k_output_tokens: float | None = None
    max_context_tokens: int | None = None
    is_custom: bool | None = None
    metadata: dict[str, Any] | None = None


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    alias: str
    provider_slug: str
    real_model_id: str
    cost_per_1k_input_tokens: float
    cost_per_1k_output_tokens: float
    max_context_tokens: int | None
    is_custom: bool
    metadata: dict[str, Any] = Field(alias="extra_metadata")
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ── Routes ──────────────────────────────────────────────────────────


class RouteSetIn(BaseModel):
    """Set the primary (priority=0) route for a model. Replaces any
    existing routes at priority 0; leaves higher-priority fallbacks
    untouched. Use ``RouteCreateIn`` / ``RoutePatchIn`` for explicit
    multi-route control."""

    credential_id: UUID


class RouteCreateIn(BaseModel):
    model_alias: str
    credential_id: UUID
    priority: int = 0


class RoutePatchIn(BaseModel):
    credential_id: UUID | None = None
    priority: int | None = None


class RouteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    model_alias: str
    credential_id: UUID
    priority: int
    updated_at: datetime
    # Hydrated for dashboard convenience.
    provider_slug: str | None = None
    credential_label: str | None = None
    # Live health (mirrored from the cache).
    is_blocked: bool = False
    consecutive_failures: int = 0
    last_error: str = ""
