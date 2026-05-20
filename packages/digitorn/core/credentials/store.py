"""CredentialStore - encrypted CRUD over the `credentials` table; single gatekeeper for read/write."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm.attributes import flag_modified

from digitorn.core.credentials.encryption import Cipher, mask_secret

logger = logging.getLogger(__name__)


# Enum-like string constants - keep in sync with the Credential model
class Scope:
    PER_APP_PER_USER = "per_app_per_user"
    PER_USER = "per_user"
    PER_APP_SHARED = "per_app_shared"
    SYSTEM_WIDE = "system_wide"

    ALL = (PER_APP_PER_USER, PER_USER, PER_APP_SHARED, SYSTEM_WIDE)


class Status:
    PENDING = "pending"
    FILLED = "filled"
    VALID = "valid"
    EXPIRED = "expired"
    INVALID = "invalid"
    REFRESHING = "refreshing"
    ERROR = "error"


class CredentialNotFound(Exception):
    """Raised when a requested credential does not exist."""


class CredentialMissing(Exception):
    """Raised by `resolve_field` when no credential in any scope provides the requested field."""

    def __init__(
        self,
        provider: str,
        field: str,
        app_id: str | None,
        user_id: str | None,
        *,
        agent_id: str | None = None,
        provider_id: str | None = None,
    ):
        self.provider = provider
        self.field = field
        self.app_id = app_id
        self.user_id = user_id
        # agent_id / provider_id are tracing fields only - never used as the credential lookup key.
        self.agent_id = agent_id
        self.provider_id = provider_id
        super().__init__(
            f"credential missing: provider={provider!r} field={field!r} "
            f"(app={app_id!r} user={user_id!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "credential_required",
            "category": "credential",
            "provider": self.provider,
            "provider_type": "api_key",
            "app_id": self.app_id,
            "user_id": self.user_id,
            "field": self.field,
            "agent_id": self.agent_id,
            "provider_id": self.provider_id,
            "candidates": [],
            "field_spec": {
                "name": self.provider,
                "label": self.provider,
                "type": "api_key",
                "fields": [
                    {
                        "name": self.field,
                        "label": self.field,
                        "type": "password",
                        "required": True,
                    }
                ],
            },
            "picker_url": "/api/credentials/providers",
        }


class CredentialAuthRequired(Exception):
    """Raised when a user has credentials for a provider but none are granted to the requesting app yet."""

    def __init__(
        self,
        *,
        provider: str,
        provider_type: str,
        app_id: str,
        user_id: str,
        candidates: list[dict[str, Any]] | None = None,
        field_spec: dict[str, Any] | None = None,
        oauth_missing_scopes: list[str] | None = None,
    ) -> None:
        self.provider = provider
        self.provider_type = provider_type
        self.app_id = app_id
        self.user_id = user_id
        self.candidates = list(candidates or [])
        self.field_spec = dict(field_spec or {})
        self.oauth_missing_scopes = list(oauth_missing_scopes or [])
        super().__init__(
            f"credential_auth_required: provider={provider!r} "
            f"app_id={app_id!r} user_id={user_id!r} "
            f"candidates={len(self.candidates)}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "credential_auth_required",
            "category": "auth",
            "provider": self.provider,
            "provider_type": self.provider_type,
            "app_id": self.app_id,
            "user_id": self.user_id,
            "candidates": self.candidates,
            "field_spec": self.field_spec,
            "oauth_missing_scopes": self.oauth_missing_scopes,
        }


class OwnerType:
    """Ownership dimension of a credential: USER (granted per app) or SYSTEM (admin-owned, default-visible)."""

    USER = "user"
    SYSTEM = "system"
    ALL = (USER, SYSTEM)


class _CipherAdapter:
    """Normalise sync access for both legacy `Cipher` and new `VersionedCipher`."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def encrypt(self, fields: dict[str, Any]) -> tuple[bytes, bytes]:
        if hasattr(self._inner, "encrypt_sync"):
            return self._inner.encrypt_sync(fields)
        return self._inner.encrypt(fields)

    def decrypt(self, payload: bytes, nonce: bytes) -> dict[str, Any]:
        if hasattr(self._inner, "decrypt_sync"):
            return self._inner.decrypt_sync(payload, nonce)
        return self._inner.decrypt(payload, nonce)


def _wrap_cipher(c: Any) -> _CipherAdapter:
    if isinstance(c, _CipherAdapter):
        return c
    return _CipherAdapter(c)


class CredentialStore:
    """Async store wrapping the `credentials` table with encryption."""

    def __init__(self, session_factory: Any, cipher: Any) -> None:
        self._session_factory = session_factory
        self._cipher = _wrap_cipher(cipher)


    async def upsert_credential(
        self,
        *,
        provider_name: str,
        provider_type: str,
        scope: str,
        fields: dict[str, Any],
        user_id: str | None = None,
        app_id: str | None = None,
        status: str = Status.FILLED,
        expires_at: datetime | str | None = None,
        display_metadata: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Create or overwrite a credential for the given scope tuple."""
        from digitorn.core.models import Credential

        expires_at_dt = self._normalise_datetime(expires_at)

        self._validate_scope(scope, user_id, app_id)

        # display_metadata holds masked previews so the UI can say "set" without decrypting.
        display = dict(display_metadata or {})
        masked = {k: mask_secret(str(v)) for k, v in fields.items() if v is not None}
        display.setdefault("masked_fields", masked)

        ciphertext, nonce = self._cipher.encrypt(fields)
        slug = (name or "").strip() or provider_name

        async with self._session_factory() as db:
            existing = await self._fetch_exact(
                db, user_id, app_id, provider_name,
            )
            if existing is None:
                row = Credential(
                    user_id=user_id,
                    app_id=app_id,
                    name=slug,
                    provider_name=provider_name,
                    provider_type=provider_type,
                    scope=scope,
                    encrypted_fields=ciphertext,
                    nonce=nonce,
                    status=status,
                    expires_at=expires_at_dt,
                    display_metadata=display,
                )
                db.add(row)
            else:
                row = existing
                if not row.name:
                    row.name = slug
                row.provider_type = provider_type
                row.scope = scope
                row.encrypted_fields = ciphertext
                row.nonce = nonce
                row.status = status
                # upsert with expires_at=None keeps the previous expiry rather than wiping it.
                if expires_at is not None:
                    row.expires_at = expires_at_dt
                row.display_metadata = display
                row.updated_at = datetime.now(timezone.utc)
                flag_modified(row, "display_metadata")

            await db.commit()
            await db.refresh(row)
            return self._row_to_dict(row, include_fields=False)

    async def update_status(
        self,
        credential_id: str,
        *,
        status: str,
        expires_at: datetime | str | None = None,
        last_validated_at: datetime | str | None = None,
        last_error: str | None = None,
    ) -> bool:
        """Update lifecycle state after a refresh / live test."""
        from digitorn.core.models import Credential

        expires_at_dt = self._normalise_datetime(expires_at)
        last_validated_dt = self._normalise_datetime(last_validated_at)

        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(Credential).where(Credential.id == credential_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.status = status
            if expires_at is not None:
                row.expires_at = expires_at_dt
            if last_validated_at is not None:
                row.last_validated_at = last_validated_dt
            row.last_error = last_error
            row.updated_at = datetime.now(timezone.utc)
            await db.commit()
            return True

    async def delete_credential(
        self,
        *,
        user_id: str | None,
        app_id: str | None,
        provider_name: str,
    ) -> bool:
        """Delete one credential scoped by the tuple."""
        from digitorn.core.models import Credential

        async with self._session_factory() as db:
            stmt = delete(Credential).where(
                _eq_or_null(Credential.user_id, user_id),
                _eq_or_null(Credential.app_id, app_id),
                Credential.provider_name == provider_name,
            )
            result = await db.execute(stmt)
            await db.commit()
            return (result.rowcount or 0) > 0


    async def get_credential(
        self,
        *,
        user_id: str | None,
        app_id: str | None,
        provider_name: str,
        decrypt: bool = False,
    ) -> dict[str, Any] | None:
        """Fetch a single credential by its scope tuple."""
        async with self._session_factory() as db:
            row = await self._fetch_exact(db, user_id, app_id, provider_name)
            if row is None:
                return None
            return self._row_to_dict(row, include_fields=decrypt)

    async def list_credentials(
        self,
        *,
        user_id: str | None = None,
        app_id: str | None = None,
        scope: str | None = None,
        provider_types: list[str] | None = None,
        provider_names: list[str] | None = None,
        name_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        """List credentials matching any subset of filters."""
        from digitorn.core.models import Credential

        async with self._session_factory() as db:
            stmt = select(Credential)
            if user_id is not None:
                stmt = stmt.where(Credential.user_id == user_id)
            if app_id is not None:
                stmt = stmt.where(Credential.app_id == app_id)
            if scope is not None:
                stmt = stmt.where(Credential.scope == scope)
            if provider_types:
                stmt = stmt.where(Credential.provider_type.in_(provider_types))
            if provider_names:
                stmt = stmt.where(Credential.provider_name.in_(provider_names))
            if name_prefix:
                stmt = stmt.where(Credential.name.startswith(name_prefix))
            stmt = stmt.order_by(Credential.provider_name)
            result = await db.execute(stmt)
            return [
                self._row_to_dict(row, include_fields=False)
                for row in result.scalars().all()
            ]

    async def get_credential_by_name(
        self,
        *,
        name: str,
        scope: str,
        user_id: str | None = None,
        app_id: str | None = None,
        decrypt: bool = False,
    ) -> dict[str, Any] | None:
        """Strict-scope lookup by user-facing name slug."""
        from digitorn.core.models import Credential

        async with self._session_factory() as db:
            stmt = (
                select(Credential)
                .where(Credential.name == name)
                .where(Credential.scope == scope)
            )
            # Apply scope-specific filters.
            if scope == Scope.SYSTEM_WIDE:
                stmt = stmt.where(Credential.user_id.is_(None))
                stmt = stmt.where(Credential.app_id.is_(None))
            elif scope == Scope.PER_APP_SHARED:
                if not app_id:
                    return None
                stmt = stmt.where(Credential.app_id == app_id)
                stmt = stmt.where(Credential.user_id.is_(None))
            elif scope == Scope.PER_USER:
                if not user_id:
                    return None
                stmt = stmt.where(Credential.user_id == user_id)
                stmt = stmt.where(Credential.app_id.is_(None))
            elif scope == Scope.PER_APP_PER_USER:
                if not user_id or not app_id:
                    return None
                stmt = stmt.where(Credential.user_id == user_id)
                stmt = stmt.where(Credential.app_id == app_id)
            else:
                raise ValueError(f"unknown scope: {scope!r}")

            result = await db.execute(stmt.limit(1))
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_dict(row, include_fields=decrypt)


    async def resolve_field(
        self,
        *,
        provider_or_field: str,
        user_id: str | None,
        app_id: str | None,
    ) -> str | None:
        """4-scope lookup for a single field value."""
        if "." in provider_or_field:
            provider, field = provider_or_field.split(".", 1)
        else:
            # Legacy flat secret name - the provider IS the field name.
            provider = provider_or_field
            field = provider_or_field

        # Walk the 4 scopes in order. The first hit wins.
        candidates = [
            (user_id, app_id),   # per_app_per_user
            (user_id, None),     # per_user
            (None, app_id),      # per_app_shared
            (None, None),        # system_wide
        ]
        for u, a in candidates:
            if u is not None and user_id is None:
                continue
            if a is not None and app_id is None:
                continue

            cred = await self.get_credential(
                user_id=u,
                app_id=a,
                provider_name=provider,
                decrypt=True,
            )
            if cred is None:
                continue

            fields = cred.get("fields") or {}
            # Direct field lookup, or legacy single-field fallback.
            if field in fields:
                return str(fields[field])
            # If the provider has a single field and the caller asked
            # for the provider name itself (legacy flat lookup), take it.
            if len(fields) == 1 and provider_or_field == provider:
                return str(next(iter(fields.values())))
        return None


    async def list_for_schema(
        self,
        *,
        user_id: str,
        app_id: str,
        schema_providers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return the credentials state needed to render the form."""
        response: dict[str, Any] = {
            "providers": [],
            "missing_required": [],
            "complete": True,
        }

        for schema_p in schema_providers:
            name = schema_p.get("name")
            if not name:
                continue
            p_scope = schema_p.get("scope", Scope.PER_USER)
            required = bool(schema_p.get("required", False))

            stored = await self._find_effective_credential(
                provider_name=name,
                declared_scope=p_scope,
                user_id=user_id,
                app_id=app_id,
            )

            entry: dict[str, Any] = {
                "name": name,
                "label": schema_p.get("label", name),
                "type": schema_p.get("type", "api_key"),
                "scope": p_scope,
                "required": required,
                "filled": stored is not None,
                "status": stored["status"] if stored else "pending",
                "masked_fields": (
                    (stored.get("display_metadata") or {}).get("masked_fields", {})
                    if stored else {}
                ),
                "last_updated": stored["updated_at"] if stored else None,
            }
            # OAuth-specific display
            if entry["type"] == "oauth2":
                meta = (stored.get("display_metadata") or {}) if stored else {}
                entry["oauth_status"] = (
                    "connected" if stored and stored["status"] == Status.VALID
                    else "disconnected"
                )
                entry["oauth_account"] = meta.get("oauth_account", "")
                entry["oauth_scopes"] = meta.get("oauth_scopes", [])
                entry["oauth_expires_at"] = (
                    stored.get("expires_at") if stored else None
                )

            response["providers"].append(entry)

            if required and not entry["filled"]:
                response["missing_required"].append(name)
                response["complete"] = False

        return response

    async def _find_effective_credential(
        self,
        *,
        provider_name: str,
        declared_scope: str,
        user_id: str,
        app_id: str,
    ) -> dict[str, Any] | None:
        """Return the most specific credential that covers a declared scope."""
        order: list[tuple[str | None, str | None]]
        if declared_scope == Scope.PER_APP_PER_USER:
            order = [(user_id, app_id)]
        elif declared_scope == Scope.PER_USER:
            order = [(user_id, app_id), (user_id, None)]
        elif declared_scope == Scope.PER_APP_SHARED:
            order = [(None, app_id)]
        elif declared_scope == Scope.SYSTEM_WIDE:
            order = [(None, None)]
        else:
            order = [(user_id, app_id), (user_id, None), (None, app_id), (None, None)]

        for u, a in order:
            cred = await self.get_credential(
                user_id=u, app_id=a,
                provider_name=provider_name,
                decrypt=False,
            )
            if cred is not None:
                return cred
        return None


    async def _fetch_exact(
        self,
        db,
        user_id: str | None,
        app_id: str | None,
        provider_name: str,
    ):
        from digitorn.core.models import Credential

        result = await db.execute(
            select(Credential).where(
                _eq_or_null(Credential.user_id, user_id),
                _eq_or_null(Credential.app_id, app_id),
                Credential.provider_name == provider_name,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _normalise_datetime(
        value: datetime | str | None,
    ) -> datetime | None:
        """Accept either a datetime or an ISO 8601 string and return a datetime."""
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as exc:
                logger.warning(
                    "CredentialStore: invalid ISO 8601 expires_at %r: %s",
                    value, exc,
                )
                return None
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        logger.warning(
            "CredentialStore: unexpected expires_at type %s",
            type(value).__name__,
        )
        return None

    def _validate_scope(
        self, scope: str, user_id: str | None, app_id: str | None,
    ) -> None:
        if scope not in Scope.ALL:
            raise ValueError(f"unknown credential scope: {scope!r}")
        expected = {
            Scope.PER_APP_PER_USER: (True, True),
            Scope.PER_USER: (True, False),
            Scope.PER_APP_SHARED: (False, True),
            Scope.SYSTEM_WIDE: (False, False),
        }[scope]
        has_user = user_id is not None
        has_app = app_id is not None
        if (has_user, has_app) != expected:
            raise ValueError(
                f"scope {scope!r} requires user_id={'set' if expected[0] else 'NULL'} "
                f"and app_id={'set' if expected[1] else 'NULL'}, "
                f"got user_id={user_id!r} app_id={app_id!r}"
            )

    def _row_to_dict(self, row: Any, *, include_fields: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": row.id,
            "user_id": row.user_id,
            "app_id": row.app_id,
            "name": getattr(row, "name", None) or "",
            "provider_name": row.provider_name,
            "provider_type": row.provider_type,
            "scope": row.scope,
            "owner_type": getattr(row, "owner_type", None) or "user",
            "label": getattr(row, "label", None) or "default",
            "status": row.status,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "last_validated_at": (
                row.last_validated_at.isoformat() if row.last_validated_at else None
            ),
            "last_error": row.last_error,
            "display_metadata": dict(row.display_metadata or {}),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        if include_fields:
            broken = getattr(self, "_decrypt_broken_ids", None)
            if broken is None:
                broken = set()
                self._decrypt_broken_ids = broken
            # Short-circuit: row already marked broken (in DB or in
            # this process's cache).
            existing_err = (row.last_error or "")
            already_broken = (
                row.id in broken
                or (
                    row.status == Status.ERROR
                    and "decrypt" in existing_err.lower()
                )
            )
            if already_broken:
                out["fields"] = {}
                out.setdefault("status", Status.ERROR)
                out.setdefault(
                    "last_error",
                    existing_err or "decryption failed (cached)",
                )
                return out
            try:
                out["fields"] = self._cipher.decrypt(row.encrypted_fields, row.nonce)
            except Exception as exc:
                broken.add(row.id)
                logger.warning(
                    "decryption failed for credential %s: %s "
                    "(rotate or delete it; suppressing further logs)",
                    row.id, exc,
                )
                out["fields"] = {}
                out["status"] = Status.ERROR
                out["last_error"] = f"decryption failed: {exc}"
        return out


def _eq_or_null(column, value):
    if value is None:
        return column.is_(None)
    return column == value


def _install_grant_methods() -> None:
    """Attach grant-aware methods to CredentialStore at import time."""
    from digitorn.core.models import Credential, CredentialGrant

    async def upsert_user_credential(
        self: CredentialStore,
        *,
        user_id: str,
        provider_name: str,
        provider_type: str,
        fields: dict[str, Any],
        label: str = "default",
        status: str = Status.FILLED,
        expires_at: datetime | str | None = None,
        display_metadata: dict[str, Any] | None = None,
        credential_id: str | None = None,
        name: str | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Create or overwrite a user-owned credential."""
        expires_at_dt = self._normalise_datetime(expires_at)
        display = dict(display_metadata or {})
        masked = {k: mask_secret(str(v)) for k, v in fields.items() if v is not None}
        display.setdefault("masked_fields", masked)
        display.setdefault("label", label)

        ciphertext, nonce = self._cipher.encrypt(fields)

        async with self._session_factory() as db:
            existing = None
            if credential_id:
                id_row = (
                    await db.execute(
                        select(Credential).where(Credential.id == credential_id)
                    )
                ).scalar_one_or_none()
                if id_row is not None and id_row.owner_type == OwnerType.USER:
                    existing = id_row
            if existing is None:
                # Fallback: find existing by (user_id, provider_name, label)
                stmt = select(Credential).where(
                    Credential.user_id == user_id,
                    Credential.provider_name == provider_name,
                    Credential.label == label,
                    Credential.owner_type == OwnerType.USER,
                )
                existing = (await db.execute(stmt)).scalar_one_or_none()
            slug = (name or "").strip() or provider_name
            effective_scope = scope or Scope.PER_USER
            if existing is None:
                row = Credential(
                    id=credential_id,
                    user_id=user_id,
                    app_id=None,
                    provider_name=provider_name,
                    provider_type=provider_type,
                    scope=effective_scope,
                    owner_type=OwnerType.USER,
                    name=slug,
                    label=label,
                    encrypted_fields=ciphertext,
                    nonce=nonce,
                    status=status,
                    expires_at=expires_at_dt,
                    display_metadata=display,
                )
                db.add(row)
            else:
                row = existing
                row.provider_type = provider_type
                row.label = label
                if name is not None:
                    row.name = slug
                row.scope = effective_scope
                row.encrypted_fields = ciphertext
                row.nonce = nonce
                row.status = status
                if expires_at is not None:
                    row.expires_at = expires_at_dt
                row.display_metadata = display
                row.updated_at = datetime.now(timezone.utc)
                flag_modified(row, "display_metadata")
            await db.commit()
            await db.refresh(row)
            return self._row_to_dict(row, include_fields=False)

    async def upsert_system_credential(
        self: CredentialStore,
        *,
        provider_name: str,
        provider_type: str,
        fields: dict[str, Any],
        app_id: str | None = None,
        label: str = "default",
        status: str = Status.FILLED,
        expires_at: datetime | str | None = None,
        display_metadata: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Create or overwrite an admin/system-owned credential."""
        expires_at_dt = self._normalise_datetime(expires_at)
        display = dict(display_metadata or {})
        masked = {k: mask_secret(str(v)) for k, v in fields.items() if v is not None}
        display.setdefault("masked_fields", masked)
        display.setdefault("label", label)

        ciphertext, nonce = self._cipher.encrypt(fields)

        async with self._session_factory() as db:
            stmt = select(Credential).where(
                _eq_or_null(Credential.user_id, None),
                _eq_or_null(Credential.app_id, app_id),
                Credential.provider_name == provider_name,
                Credential.label == label,
                Credential.owner_type == OwnerType.SYSTEM,
            )
            existing = (await db.execute(stmt)).scalar_one_or_none()
            effective_scope = (
                Scope.PER_APP_SHARED if app_id is not None else Scope.SYSTEM_WIDE
            )
            slug = (name or "").strip() or provider_name
            if existing is None:
                row = Credential(
                    user_id=None,
                    app_id=app_id,
                    provider_name=provider_name,
                    provider_type=provider_type,
                    scope=effective_scope,
                    owner_type=OwnerType.SYSTEM,
                    name=slug,
                    label=label,
                    encrypted_fields=ciphertext,
                    nonce=nonce,
                    status=status,
                    expires_at=expires_at_dt,
                    display_metadata=display,
                )
                db.add(row)
            else:
                row = existing
                row.provider_type = provider_type
                if name is not None:
                    row.name = slug
                row.encrypted_fields = ciphertext
                row.nonce = nonce
                row.status = status
                if expires_at is not None:
                    row.expires_at = expires_at_dt
                row.display_metadata = display
                row.updated_at = datetime.now(timezone.utc)
                flag_modified(row, "display_metadata")
            await db.commit()
            await db.refresh(row)
            return self._row_to_dict(row, include_fields=False)

    async def get_credential_by_id(
        self: CredentialStore,
        credential_id: str,
        *,
        decrypt: bool = False,
    ) -> dict[str, Any] | None:
        """Fetch one credential by its UUID regardless of scope."""
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(Credential).where(Credential.id == credential_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_dict(row, include_fields=decrypt)

    async def delete_credential_by_id(
        self: CredentialStore,
        credential_id: str,
    ) -> bool:
        """Hard-delete a credential by id. Cascades to its grants."""
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(Credential).where(Credential.id == credential_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            await db.execute(
                delete(CredentialGrant).where(
                    CredentialGrant.credential_id == credential_id
                )
            )
            await db.delete(row)
            await db.commit()
            return True

    async def list_user_credentials(
        self: CredentialStore,
        *,
        user_id: str,
        provider_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """List the user-owned credentials belonging to `user_id`."""
        async with self._session_factory() as db:
            stmt = select(Credential).where(
                Credential.user_id == user_id,
                Credential.owner_type == OwnerType.USER,
            )
            if provider_name is not None:
                stmt = stmt.where(Credential.provider_name == provider_name)
            stmt = stmt.order_by(Credential.provider_name, Credential.label)
            result = await db.execute(stmt)
            return [
                self._row_to_dict(r, include_fields=False)
                for r in result.scalars().all()
            ]

    async def list_system_credentials(
        self: CredentialStore,
        *,
        provider_name: str | None = None,
        app_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List admin/system credentials. Filters are optional."""
        async with self._session_factory() as db:
            stmt = select(Credential).where(
                Credential.owner_type == OwnerType.SYSTEM,
            )
            if provider_name is not None:
                stmt = stmt.where(Credential.provider_name == provider_name)
            if app_id is not None:
                stmt = stmt.where(
                    or_(Credential.app_id == app_id, Credential.app_id.is_(None))
                )
            stmt = stmt.order_by(Credential.provider_name, Credential.label)
            result = await db.execute(stmt)
            return [
                self._row_to_dict(r, include_fields=False)
                for r in result.scalars().all()
            ]


    async def create_grant(
        self: CredentialStore,
        *,
        credential_id: str,
        user_id: str,
        app_id: str,
        scopes_granted: list[str] | None = None,
    ) -> dict[str, Any]:
        """Authorize `app_id` to use `credential_id`."""
        async with self._session_factory() as db:
            # Verify the credential exists and is user-owned by this user
            cred_row = (
                await db.execute(
                    select(Credential).where(Credential.id == credential_id)
                )
            ).scalar_one_or_none()
            if cred_row is None:
                raise CredentialNotFound(credential_id)
            if cred_row.owner_type != OwnerType.USER:
                raise ValueError(
                    f"cannot grant a non-user credential "
                    f"(owner_type={cred_row.owner_type!r})"
                )
            if cred_row.user_id != user_id:
                raise PermissionError(
                    "grant must be created by the credential's owner"
                )

            existing = (
                await db.execute(
                    select(CredentialGrant).where(
                        CredentialGrant.credential_id == credential_id,
                        CredentialGrant.app_id == app_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.revoked_at = None
                if scopes_granted is not None:
                    existing.scopes_granted = list(scopes_granted)
                    flag_modified(existing, "scopes_granted")
                await db.commit()
                await db.refresh(existing)
                return _grant_to_dict(existing)

            grant = CredentialGrant(
                credential_id=credential_id,
                user_id=user_id,
                app_id=app_id,
                scopes_granted=list(scopes_granted or []),
            )
            db.add(grant)
            await db.commit()
            await db.refresh(grant)
            return _grant_to_dict(grant)

    async def revoke_grant(
        self: CredentialStore,
        *,
        credential_id: str,
        app_id: str,
        user_id: str,
        hard: bool = False,
    ) -> bool:
        """Revoke a grant."""
        async with self._session_factory() as db:
            stmt = select(CredentialGrant).where(
                CredentialGrant.credential_id == credential_id,
                CredentialGrant.app_id == app_id,
                CredentialGrant.user_id == user_id,
            )
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row is None:
                return False
            if hard:
                await db.delete(row)
            else:
                row.revoked_at = datetime.now(timezone.utc)
            await db.commit()
            return True

    async def list_grants(
        self: CredentialStore,
        *,
        user_id: str,
        app_id: str | None = None,
        credential_id: str | None = None,
        include_revoked: bool = False,
    ) -> list[dict[str, Any]]:
        """List grants owned by `user_id`."""
        async with self._session_factory() as db:
            stmt = select(CredentialGrant, Credential).join(
                Credential, Credential.id == CredentialGrant.credential_id,
            ).where(CredentialGrant.user_id == user_id)
            if app_id is not None:
                stmt = stmt.where(CredentialGrant.app_id == app_id)
            if credential_id is not None:
                stmt = stmt.where(CredentialGrant.credential_id == credential_id)
            if not include_revoked:
                stmt = stmt.where(CredentialGrant.revoked_at.is_(None))
            stmt = stmt.order_by(CredentialGrant.granted_at.desc())
            rows = (await db.execute(stmt)).all()
            out: list[dict[str, Any]] = []
            for grant, cred in rows:
                d = _grant_to_dict(grant)
                d["provider_name"] = cred.provider_name
                d["provider_type"] = cred.provider_type
                d["credential_label"] = cred.label
                out.append(d)
            return out

    async def is_granted(
        self: CredentialStore,
        *,
        credential_id: str,
        app_id: str,
    ) -> bool:
        """Return True if credential is granted to app and not revoked."""
        async with self._session_factory() as db:
            stmt = select(CredentialGrant).where(
                CredentialGrant.credential_id == credential_id,
                CredentialGrant.app_id == app_id,
                CredentialGrant.revoked_at.is_(None),
            )
            return (await db.execute(stmt)).scalar_one_or_none() is not None


    async def resolve_for_app(
        self: CredentialStore,
        *,
        provider_name: str,
        user_id: str,
        app_id: str,
        decrypt: bool = False,
        requested_oauth_scopes: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Find the effective credential for (user, app, provider)."""
        async with self._session_factory() as db:
            # (1) User-owned + granted
            stmt = (
                select(Credential, CredentialGrant)
                .join(
                    CredentialGrant,
                    CredentialGrant.credential_id == Credential.id,
                )
                .where(
                    Credential.user_id == user_id,
                    Credential.owner_type == OwnerType.USER,
                    Credential.provider_name == provider_name,
                    CredentialGrant.app_id == app_id,
                    CredentialGrant.user_id == user_id,
                    CredentialGrant.revoked_at.is_(None),
                )
                .order_by(CredentialGrant.granted_at.desc())
            )
            rows = (await db.execute(stmt)).all()
            for cred_row, grant_row in rows:
                if requested_oauth_scopes and cred_row.provider_type == "oauth2":
                    granted = set(grant_row.scopes_granted or [])
                    needed = set(requested_oauth_scopes)
                    if not needed.issubset(granted):
                        continue  # scope-upgrade flow will handle it
                meta = getattr(cred_row, "display_metadata", None) or {}
                masked = (meta or {}).get("masked_fields") or {}
                if not masked:
                    continue
                cred_dict = self._row_to_dict(cred_row, include_fields=decrypt)
                if decrypt:
                    if cred_dict.get("status") == Status.ERROR:
                        logger.warning(
                            "resolve_for_app: skipping cred %s with decrypt error: %s",
                            cred_row.id, cred_dict.get("last_error"),
                        )
                        continue
                    if not cred_dict.get("fields"):
                        logger.warning(
                            "resolve_for_app: skipping cred %s with empty fields after decrypt",
                            cred_row.id,
                        )
                        continue
                return cred_dict

            # (2) System credential scoped to this app
            row = (
                await db.execute(
                    select(Credential).where(
                        Credential.owner_type == OwnerType.SYSTEM,
                        Credential.provider_name == provider_name,
                        Credential.app_id == app_id,
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                cred_dict = self._row_to_dict(row, include_fields=decrypt)
                if not (decrypt and (
                    cred_dict.get("status") == Status.ERROR
                    or not cred_dict.get("fields")
                )):
                    return cred_dict
                logger.warning(
                    "resolve_for_app(system,app=%s): skipping cred %s",
                    app_id, row.id,
                )

            # (3) System credential - daemon-wide
            row = (
                await db.execute(
                    select(Credential).where(
                        Credential.owner_type == OwnerType.SYSTEM,
                        Credential.provider_name == provider_name,
                        Credential.app_id.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                cred_dict = self._row_to_dict(row, include_fields=decrypt)
                if not (decrypt and (
                    cred_dict.get("status") == Status.ERROR
                    or not cred_dict.get("fields")
                )):
                    return cred_dict
                logger.warning(
                    "resolve_for_app(system,daemon-wide): skipping cred %s",
                    row.id,
                )

        return None

    async def resolve_field_for_app(
        self: CredentialStore,
        *,
        provider_or_field: str,
        user_id: str,
        app_id: str,
        raise_on_auth_required: bool = True,
        field_spec: dict[str, Any] | None = None,
        requested_oauth_scopes: list[str] | None = None,
    ) -> str | None:
        """Grant-aware single-field lookup used by the runtime resolver."""
        if "." in provider_or_field:
            provider, field = provider_or_field.split(".", 1)
        else:
            provider = provider_or_field
            field = provider_or_field

        cred = await self.resolve_for_app(
            provider_name=provider,
            user_id=user_id,
            app_id=app_id,
            decrypt=True,
            requested_oauth_scopes=requested_oauth_scopes,
        )
        if cred is not None:
            fields = cred.get("fields") or {}
            if field in fields:
                return str(fields[field])
            if len(fields) == 1:
                return str(next(iter(fields.values())))
            field_lower = field.lower()
            for k, v in fields.items():
                if k.lower() == field_lower:
                    return str(v)
                # Strip the provider prefix from the YAML name so
                # `DEEPSEEK_API_KEY` matches the canonical `api_key`.
                if "_" in field_lower:
                    suffix = field_lower.split("_", 1)[1]
                    if k.lower() == suffix or k.lower() == suffix.replace("_", ""):
                        return str(v)
            # Truly unmatched.
            return None

        # Nothing matched. Do we have candidates to propose?
        candidates = await self.list_user_credentials(
            user_id=user_id, provider_name=provider,
        )
        if candidates and raise_on_auth_required:
            # Keep only the fields the picker needs - no decrypt.
            picker_candidates = [
                {
                    "id": c["id"],
                    "label": c.get("display_metadata", {}).get("label")
                             or c.get("display_metadata", {}).get("masked_fields", {})
                             or "default",
                    "provider_name": c["provider_name"],
                    "provider_type": c["provider_type"],
                    "status": c["status"],
                    "masked_fields": c.get("display_metadata", {}).get(
                        "masked_fields", {}
                    ),
                    "updated_at": c.get("updated_at"),
                }
                for c in candidates
            ]
            raise CredentialAuthRequired(
                provider=provider,
                provider_type=candidates[0]["provider_type"],
                app_id=app_id,
                user_id=user_id,
                candidates=picker_candidates,
                field_spec=field_spec,
            )
        return None


    async def migrate_legacy_scopes(self: CredentialStore) -> dict[str, int]:
        """One-shot migration: set owner_type and create grants for"""
        counts = {"user_rows": 0, "system_rows": 0, "grants_created": 0}
        async with self._session_factory() as db:
            stmt = select(Credential).where(
                or_(Credential.owner_type == "", Credential.owner_type.is_(None))
            )
            rows = (await db.execute(stmt)).scalars().all()
            for row in rows:
                legacy_scope = row.scope
                if legacy_scope in (Scope.PER_USER, Scope.PER_APP_PER_USER):
                    row.owner_type = OwnerType.USER
                    if not row.label:
                        row.label = "default"
                    counts["user_rows"] += 1
                    if legacy_scope == Scope.PER_APP_PER_USER and row.app_id:
                        # Create grant for the original app
                        orig_app_id = row.app_id
                        # Detach app_id on user credentials - they are
                        # user-owned now and live independent of apps.
                        row.app_id = None
                        db.add(
                            CredentialGrant(
                                credential_id=row.id,
                                user_id=row.user_id,
                                app_id=orig_app_id,
                                scopes_granted=(row.display_metadata or {}).get(
                                    "oauth_scopes", []
                                ),
                            )
                        )
                        counts["grants_created"] += 1
                elif legacy_scope in (Scope.PER_APP_SHARED, Scope.SYSTEM_WIDE):
                    row.owner_type = OwnerType.SYSTEM
                    if not row.label:
                        row.label = "default"
                    counts["system_rows"] += 1
                else:
                    # Unknown - set a safe default
                    row.owner_type = OwnerType.USER
                    if not row.label:
                        row.label = "default"
                    counts["user_rows"] += 1
            await db.commit()
        return counts

    # Attach everything
    CredentialStore.upsert_user_credential = upsert_user_credential
    CredentialStore.upsert_system_credential = upsert_system_credential
    CredentialStore.get_credential_by_id = get_credential_by_id
    CredentialStore.delete_credential_by_id = delete_credential_by_id
    CredentialStore.list_user_credentials = list_user_credentials
    CredentialStore.list_system_credentials = list_system_credentials
    CredentialStore.create_grant = create_grant
    CredentialStore.revoke_grant = revoke_grant
    CredentialStore.list_grants = list_grants
    CredentialStore.is_granted = is_granted
    CredentialStore.resolve_for_app = resolve_for_app
    CredentialStore.resolve_field_for_app = resolve_field_for_app
    CredentialStore.migrate_legacy_scopes = migrate_legacy_scopes


def _grant_to_dict(grant: Any) -> dict[str, Any]:
    return {
        "id": grant.id,
        "credential_id": grant.credential_id,
        "user_id": grant.user_id,
        "app_id": grant.app_id,
        "scopes_granted": list(grant.scopes_granted or []),
        "granted_at": grant.granted_at.isoformat() if grant.granted_at else None,
        "revoked_at": grant.revoked_at.isoformat() if grant.revoked_at else None,
        "active": grant.revoked_at is None,
    }


_install_grant_methods()
