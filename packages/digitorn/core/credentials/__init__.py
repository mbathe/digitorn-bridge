"""Universal credentials subsystem."""

from digitorn.core.credentials.encryption import (
    Cipher,
    CredentialEncryptionError,
    load_or_create_master_key,
    mask_secret,
    rotate_master_key,
)
from digitorn.core.credentials.handler import (
    CredentialHandler,
    HandlerNotAvailable,
    HandlerRegistry,
    ValidationError,
    default_registry,
)
from digitorn.core.credentials.store import (
    CredentialAuthRequired,
    CredentialMissing,
    CredentialNotFound,
    CredentialStore,
    OwnerType,
    Scope,
    Status,
)

# Register built-in handlers on import.
from digitorn.core.credentials import handlers  # noqa: F401

from digitorn.core.credentials.runtime_resolver import (
    collect_unresolved_secrets,
    resolve_runtime_secrets_in_value,
)
from digitorn.core.credentials.session_resolver import (
    ensure_user_credentials_for_app,
)

__all__ = [
    "Cipher",
    "CredentialAuthRequired",
    "CredentialEncryptionError",
    "CredentialHandler",
    "CredentialMissing",
    "CredentialNotFound",
    "CredentialStore",
    "HandlerNotAvailable",
    "HandlerRegistry",
    "OwnerType",
    "Scope",
    "Status",
    "ValidationError",
    "collect_unresolved_secrets",
    "default_registry",
    "ensure_user_credentials_for_app",
    "load_or_create_master_key",
    "mask_secret",
    "resolve_runtime_secrets_in_value",
    "rotate_master_key",
]
