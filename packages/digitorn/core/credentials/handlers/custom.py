"""CustomHandler - catch-all for any provider type not covered built-in."""

from __future__ import annotations

from digitorn.core.credentials.handler import CredentialHandler


class CustomHandler(CredentialHandler):
    provider_type = "custom"
    # Catch-all - allow every scope since the catalog declares the
    # actual constraint per-provider.
    allowed_scopes = (
        "system_wide",
        "per_app_shared",
        "per_user",
        "per_app_per_user",
    )
