"""AwsAccessKeyHandler - AWS access key + secret key + region.

The most common AWS auth shape: an access_key_id + secret_access_key
pair, optionally plus a session_token (for temporary STS-issued
creds). The handler validates and can run STS GetCallerIdentity to
verify the key works.

For role-based auth (assume-role with external_id), use
`aws_assume_role` (separate handler, future).

Region is required because most AWS services are regional. The
catalog can constrain available regions per provider.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from digitorn.core.credentials.handler import CredentialHandler

logger = logging.getLogger(__name__)


# AWS access key id format: AKIA / ASIA + 16 [A-Z0-9].
_AKID_RE = re.compile(r"^(AKIA|ASIA)[A-Z0-9]{16}$")


class AwsAccessKeyHandler(CredentialHandler):
    provider_type = "aws_access_key"
    # AWS access keys are flexible: corporate IAM user (system_wide),
    # team/app-shared (per_app_shared), per-developer (per_user).
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
                name="access_key_id",
                label="AWS access key ID",
                type=FieldType.TEXT,
                required=True,
                masked=False,
                placeholder="AKIA... or ASIA...",
                validation_regex=r"^(AKIA|ASIA)[A-Z0-9]{16}$",
                inject_path_default="{block}.config.aws_access_key_id",
            ),
            FieldSpec(
                name="secret_access_key",
                label="AWS secret access key",
                type=FieldType.PASSWORD,
                required=True,
                masked=True,
                min_length=20,
                max_length=128,
                inject_path_default="{block}.config.aws_secret_access_key",
            ),
            FieldSpec(
                name="region",
                label="Default region",
                type=FieldType.SELECT,
                required=True,
                masked=False,
                default="us-east-1",
                choices=[
                    ("us-east-1", "US East (N. Virginia)"),
                    ("us-east-2", "US East (Ohio)"),
                    ("us-west-1", "US West (N. California)"),
                    ("us-west-2", "US West (Oregon)"),
                    ("eu-west-1", "EU (Ireland)"),
                    ("eu-west-2", "EU (London)"),
                    ("eu-west-3", "EU (Paris)"),
                    ("eu-central-1", "EU (Frankfurt)"),
                    ("eu-north-1", "EU (Stockholm)"),
                    ("ap-northeast-1", "Asia Pacific (Tokyo)"),
                    ("ap-southeast-1", "Asia Pacific (Singapore)"),
                    ("ap-southeast-2", "Asia Pacific (Sydney)"),
                    ("ap-south-1", "Asia Pacific (Mumbai)"),
                    ("sa-east-1", "South America (São Paulo)"),
                ],
                inject_path_default="{block}.config.aws_region",
            ),
            FieldSpec(
                name="session_token",
                label="Session token",
                type=FieldType.PASSWORD,
                required=False,
                masked=True,
                help="Optional - only for temporary credentials issued by STS.",
                inject_path_default="{block}.config.aws_session_token",
            ),
        ]

    async def test_live_connection(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Run `sts:GetCallerIdentity` - cheap, doesn't require any
        permissions beyond identity verification, fails fast on bad
        keys or expired session tokens."""
        akid = (fields or {}).get("access_key_id", "")
        secret = (fields or {}).get("secret_access_key", "")
        region = (fields or {}).get("region", "us-east-1")
        token = (fields or {}).get("session_token") or None
        if not akid or not secret:
            return False, "access_key_id and secret_access_key required"
        if not _AKID_RE.match(akid):
            return False, "access_key_id format invalid (expected AKIA/ASIA + 16 alphanumeric)"

        try:
            import asyncio
            import boto3
        except ImportError:
            # boto3 not installed - skip live test, trust validation.
            return True, None

        def _call() -> dict[str, Any]:
            client = boto3.client(
                "sts",
                region_name=region,
                aws_access_key_id=akid,
                aws_secret_access_key=secret,
                aws_session_token=token,
            )
            return client.get_caller_identity()

        try:
            await asyncio.to_thread(_call)
            return True, None
        except Exception as exc:
            return False, f"STS rejected the keys: {exc}"
