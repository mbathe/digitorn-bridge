"""BasicAuthHandler - HTTP Basic auth (RFC 7617)."""

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
        """Hit `test_endpoint` (or fields.base_url for custom creds) with HTTP Basic."""
        endpoint = (schema_provider or {}).get("test_endpoint") or \
                   str((fields or {}).get("base_url") or "").strip()
        if not endpoint:
            user_set = bool((fields or {}).get("username"))
            pwd_set = bool((fields or {}).get("password"))
            if user_set and pwd_set:
                return True, "Credentials set (no base_url to ping)"
            return False, "username and password required"
        user = (fields or {}).get("username", "")
        pwd = (fields or {}).get("password", "")
        if not user or not pwd:
            return False, "username and password required"
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(endpoint, auth=(user, pwd))
                if 200 <= resp.status_code < 300:
                    return True, f"HTTP {resp.status_code}"
                if resp.status_code in (401, 403):
                    return False, f"Auth rejected (HTTP {resp.status_code})"
                return True, f"Reachable (HTTP {resp.status_code})"
        except Exception as exc:
            return False, str(exc)
