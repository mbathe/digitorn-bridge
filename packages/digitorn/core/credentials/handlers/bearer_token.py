"""BearerTokenHandler - pre-issued bearer tokens (PAT, JWT, opaque).

Same shape as ApiKey but semantically distinct: a Bearer token is
typically a longer-lived secret issued by a manual flow (GitHub PAT
generation, GitLab Personal Access Token, internal JWT signed
elsewhere). Unlike OAuth2 there's no refresh dance - the user copy-
pastes the token in once and revokes manually when needed.

The header injection is `Authorization: Bearer <token>` - that's the
distinction from `api_key` (which can be sent in custom headers like
`x-api-key`, `xi-api-key`, query strings, etc.). The catalog declares
the exact header for each provider.
"""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.credentials.handler import CredentialHandler

logger = logging.getLogger(__name__)


class BearerTokenHandler(CredentialHandler):
    provider_type = "bearer_token"
    # PATs / pre-issued tokens are personal artefacts - each user
    # generates their own. system_wide is allowed for org-wide service
    # tokens but rare.
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
                name="token",
                label="Bearer token",
                type=FieldType.PASSWORD,
                required=True,
                masked=True,
                help="Long-lived token issued by the provider (PAT, JWT).",
                placeholder="ghp_... / eyJ... / hvs....",
                min_length=8,
                inject_path_default="{block}.config.token",
            ),
        ]

    async def test_live_connection(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Hit `test_endpoint` (or fields.base_url) with Bearer auth.

        Falls back to ``fields.base_url`` when the schema provides
        no recipe (custom-template path). 401/403 = auth rejected;
        anything else reachable counts as success.
        """
        endpoint = (schema_provider or {}).get("test_endpoint") or \
                   str((fields or {}).get("base_url") or "").strip()
        token = (fields or {}).get("token", "")
        if not token:
            return False, "no token to test"
        if not endpoint:
            return True, "Token set (no base_url to ping)"
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(
                    endpoint,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if 200 <= resp.status_code < 300:
                    return True, f"HTTP {resp.status_code}"
                if resp.status_code in (401, 403):
                    return False, f"Auth rejected (HTTP {resp.status_code})"
                return True, f"Reachable (HTTP {resp.status_code})"
        except Exception as exc:
            return False, str(exc)
