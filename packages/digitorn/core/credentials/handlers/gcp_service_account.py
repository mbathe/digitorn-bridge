"""GcpServiceAccountHandler - Google Cloud service account JSON key."""

from __future__ import annotations

import json
import logging
from typing import Any

from digitorn.core.credentials.handler import CredentialHandler, ValidationError

logger = logging.getLogger(__name__)


class GcpServiceAccountHandler(CredentialHandler):
    provider_type = "gcp_service_account"
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
                name="service_account_json",
                label="Service account JSON",
                type=FieldType.JSON,
                required=True,
                masked=True,
                help="Paste the full JSON downloaded from the GCP console.",
                placeholder='{ "type": "service_account", ... }',
                inject_path_default="{block}.config.gcp_credentials_path",
            ),
            FieldSpec(
                name="project_id",
                label="Project ID",
                type=FieldType.TEXT,
                required=False,
                masked=False,
                help="Auto-detected from the JSON if left empty.",
            ),
        ]

    def validate_fields(
        self,
        fields: dict[str, Any],
        schema_fields: list[dict[str, Any]],
    ) -> None:
        super().validate_fields(fields, schema_fields)
        sa = (fields or {}).get("service_account_json", "")
        if not sa:
            return
        try:
            doc = json.loads(sa)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "service_account_json", f"not valid JSON: {exc}",
            ) from exc
        if not isinstance(doc, dict):
            raise ValidationError(
                "service_account_json", "must be a JSON object",
            )
        required = {
            "type", "project_id", "private_key", "client_email", "token_uri",
        }
        missing = required - set(doc.keys())
        if missing:
            raise ValidationError(
                "service_account_json",
                f"missing required keys: {sorted(missing)}",
            )
        if doc.get("type") != "service_account":
            raise ValidationError(
                "service_account_json",
                f"type must be 'service_account', got {doc.get('type')!r}",
            )
        priv = doc.get("private_key", "")
        if "BEGIN PRIVATE KEY" not in priv:
            raise ValidationError(
                "service_account_json",
                "private_key does not look like a PEM private key",
            )

    async def test_live_connection(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Parse the JSON and exchange it for an access token if possible."""
        sa = (fields or {}).get("service_account_json", "")
        if not sa:
            return False, "service_account_json is empty"
        try:
            doc = json.loads(sa)
        except Exception as exc:
            return False, f"JSON parse failed: {exc}"
        if not isinstance(doc, dict) or doc.get("type") != "service_account":
            return False, "not a valid service_account JSON"
        client_email = doc.get("client_email") or "(unknown)"
        # Try google-auth for a real token exchange.
        try:
            from google.oauth2 import service_account
            from google.auth.transport.requests import Request as _GAuthRequest
            import asyncio
        except ImportError:
            return True, f"JSON shape valid (client_email={client_email})"
        try:
            def _do_refresh() -> None:
                creds = service_account.Credentials.from_service_account_info(
                    doc,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
                creds.refresh(_GAuthRequest())
            await asyncio.wait_for(asyncio.to_thread(_do_refresh), 10.0)
            return True, f"Token issued (client_email={client_email})"
        except asyncio.TimeoutError:
            return False, "Timeout exchanging service account for token"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
