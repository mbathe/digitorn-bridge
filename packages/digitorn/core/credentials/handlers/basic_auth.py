"""BasicAuthHandler - HTTP Basic auth (RFC 7617).

Username + password fields. The runtime injection encodes them as
`Authorization: Basic base64(user:pass)` when consumed.

Common consumers:
  - Internal HTTP services with no OAuth.
  - IMAP / SMTP servers (with the multi-field-shaped variants
    `imap_auth` / `smtp_auth` for host+port+TLS specifics).
  - Docker registries.
  - Old-school enterprise APIs.
"""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.credentials.handler import CredentialHandler

logger = logging.getLogger(__name__)


class BasicAuthHandler(CredentialHandler):
    provider_type = "basic_auth"
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
                name="username",
                label="Username",
                type=FieldType.TEXT,
                required=True,
                masked=False,
                inject_path_default="{block}.config.username",
            ),
            FieldSpec(
                name="password",
                label="Password",
                type=FieldType.PASSWORD,
                required=True,
                masked=True,
                inject_path_default="{block}.config.password",
            ),
        ]

    async def test_live_connection(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Hit `test_endpoint` with HTTP Basic auth."""
        endpoint = (schema_provider or {}).get("test_endpoint")
        if not endpoint:
            return True, None
        user = (fields or {}).get("username", "")
        pwd = (fields or {}).get("password", "")
        if not user or not pwd:
            return False, "username and password required"
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(endpoint, auth=(user, pwd))
                if 200 <= resp.status_code < 300:
                    return True, None
                return False, f"HTTP {resp.status_code}"
        except Exception as exc:
            return False, str(exc)
