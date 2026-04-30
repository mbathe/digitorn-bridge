"""AzureAdHandler - Azure Active Directory app registration credential.

Three fields: tenant_id + client_id + client_secret. The most common
auth shape for Azure services (Azure OpenAI, Azure Storage with
app-based auth, Microsoft Graph). A managed identity flow exists
but is implicit (no credential to store) - this handler covers
explicit app registration creds only.

Validation: tenant_id and client_id are GUIDs; the handler checks
the format and runs an OAuth2 client_credentials grant against AAD
to verify the secret if `test_live_connection` is requested.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from digitorn.core.credentials.handler import CredentialHandler

logger = logging.getLogger(__name__)


_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
)


class AzureAdHandler(CredentialHandler):
    provider_type = "azure_ad"
    allowed_scopes = (
        "system_wide",
        "per_app_shared",
        "per_user",
        "per_app_per_user",
    )

    @classmethod
    def schema_fields(cls) -> list[Any]:
        from digitorn.core.credentials.field_spec import FieldSpec, FieldType
        return [
            FieldSpec(
                name="tenant_id",
                label="Tenant ID",
                type=FieldType.TEXT,
                required=True,
                masked=False,
                placeholder="00000000-0000-0000-0000-000000000000",
                validation_regex=_GUID_RE.pattern,
                inject_path_default="{block}.config.azure_tenant_id",
            ),
            FieldSpec(
                name="client_id",
                label="Client ID",
                type=FieldType.TEXT,
                required=True,
                masked=False,
                placeholder="00000000-0000-0000-0000-000000000000",
                validation_regex=_GUID_RE.pattern,
                inject_path_default="{block}.config.azure_client_id",
            ),
            FieldSpec(
                name="client_secret",
                label="Client secret",
                type=FieldType.PASSWORD,
                required=True,
                masked=True,
                inject_path_default="{block}.config.azure_client_secret",
            ),
        ]

    async def test_live_connection(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Run a client_credentials grant against `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`."""
        tenant = (fields or {}).get("tenant_id", "")
        client_id = (fields or {}).get("client_id", "")
        client_secret = (fields or {}).get("client_secret", "")
        if not (tenant and client_id and client_secret):
            return False, "tenant_id, client_id, client_secret required"
        # Default scope - the catalog can override per provider.
        scope = (schema_provider or {}).get("test_scope", "https://graph.microsoft.com/.default")
        try:
            import httpx
            url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
            data = {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": scope,
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, data=data)
                if resp.status_code == 200:
                    return True, None
                return False, f"AAD rejected: HTTP {resp.status_code}"
        except Exception as exc:
            return False, str(exc)
