"""GcpServiceAccountHandler - Google Cloud service account JSON key.

A GCP service account is provisioned in the Cloud Console; downloading
its key produces a JSON document like::

    {
      "type": "service_account",
      "project_id": "...",
      "private_key_id": "...",
      "private_key": "-----BEGIN PRIVATE KEY-----\n...",
      "client_email": "sa@...iam.gserviceaccount.com",
      "client_id": "...",
      "auth_uri": "https://accounts.google.com/o/oauth2/auth",
      "token_uri": "https://oauth2.googleapis.com/token",
      ...
    }

The user uploads the JSON; the handler stores it encrypted. The
runtime injector writes it to a temp file and exports
GOOGLE_APPLICATION_CREDENTIALS=<path> so the GCP SDKs find it
transparently.

The handler validates the JSON shape and key format before saving.
"""

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
