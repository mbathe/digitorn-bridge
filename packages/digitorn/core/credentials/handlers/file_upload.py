"""FileUploadHandler - binary file blob credential.

Covers credentials that are file-shaped: kubeconfig, GCP service
account JSON, client certificates (PEM), JKS keystores, .p12 PKCS#12,
PGP private keys, etc.

The user uploads a file via the UI; the handler stores its bytes
encrypted. At runtime, the injector writes the bytes to a temp file
in the session's tmpdir (mode 0600), passes the path to the consumer
module, and deletes the file at session end.

Fields:
  - `content_b64`: base64-encoded file bytes (so JSON-serializable
    inside the encrypted_fields blob).
  - `mime_type`: optional MIME hint.
  - `filename`: original filename (for the injector's temp file
    extension).
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from digitorn.core.credentials.handler import CredentialHandler, ValidationError

logger = logging.getLogger(__name__)


# 10 MiB - typical file-based credentials are kilobytes; this caps
# accidental uploads of huge unrelated files.
_MAX_BYTES = 10 * 1024 * 1024


class FileUploadHandler(CredentialHandler):
    provider_type = "file_upload"
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
                name="content_b64",
                label="File content",
                type=FieldType.FILE,
                required=True,
                masked=True,
                help="Upload a file; the daemon stores its content encrypted.",
                inject_path_default="{block}.config.file_path",
            ),
            FieldSpec(
                name="filename",
                label="Filename",
                type=FieldType.TEXT,
                required=False,
                masked=False,
                help="Original filename (used for the temp file extension).",
            ),
            FieldSpec(
                name="mime_type",
                label="MIME type",
                type=FieldType.TEXT,
                required=False,
                masked=False,
                placeholder="application/json, application/x-pem-file",
            ),
        ]

    def validate_fields(
        self,
        fields: dict[str, Any],
        schema_fields: list[dict[str, Any]],
    ) -> None:
        super().validate_fields(fields, schema_fields)
        content = (fields or {}).get("content_b64", "")
        if not content:
            return
        try:
            raw = base64.b64decode(content, validate=True)
        except Exception as exc:
            raise ValidationError(
                "content_b64", f"not valid base64: {exc}",
            ) from exc
        if len(raw) > _MAX_BYTES:
            raise ValidationError(
                "content_b64",
                f"file too large: {len(raw)} bytes (max {_MAX_BYTES})",
            )
        if len(raw) == 0:
            raise ValidationError("content_b64", "file is empty")
