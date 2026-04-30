"""McpHttpHandler - remote MCP server reached over HTTP / SSE.

Distinguished from `mcp_server` (alias for `mcp_stdio`) which spawns
a local process. The HTTP variant connects to a remote MCP endpoint
and authenticates via a delegated method (api_key / oauth2 / custom
headers).

Fields:
  - `url`: base URL of the MCP server.
  - `transport`: `http` or `sse`.
  - `auth_mode`: one of {`none`, `api_key`, `bearer`, `basic`, `oauth2`}.
  - Plus the auth fields matching the chosen mode.

Validation: the URL must parse, the auth_mode must come with the
right correlated fields. The handler can optionally probe the
server's `/health` or perform an MCP `initialize` round-trip.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from digitorn.core.credentials.handler import CredentialHandler, ValidationError

logger = logging.getLogger(__name__)


class McpHttpHandler(CredentialHandler):
    provider_type = "mcp_http"
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
                name="url",
                label="MCP server URL",
                type=FieldType.URL,
                required=True,
                masked=False,
                placeholder="https://mcp.example.com/v1",
                inject_path_default="{block}.config.url",
            ),
            FieldSpec(
                name="transport",
                label="Transport",
                type=FieldType.SELECT,
                required=False,
                masked=False,
                default="http",
                choices=[
                    ("http", "HTTP (request/response)"),
                    ("sse", "Server-Sent Events"),
                ],
                inject_path_default="{block}.config.transport",
            ),
            FieldSpec(
                name="auth_mode",
                label="Authentication",
                type=FieldType.SELECT,
                required=False,
                masked=False,
                default="none",
                choices=[
                    ("none", "No auth (public endpoint)"),
                    ("api_key", "API key (custom header)"),
                    ("bearer", "Bearer token (Authorization header)"),
                    ("basic", "HTTP Basic (user + password)"),
                ],
                inject_path_default="{block}.config.auth_mode",
            ),
            FieldSpec(
                name="auth_token",
                label="Token / API key",
                type=FieldType.PASSWORD,
                required=False,
                masked=True,
                help="Used when auth_mode is api_key or bearer.",
                inject_path_default="{block}.config.auth_token",
            ),
            FieldSpec(
                name="auth_header_name",
                label="Header name",
                type=FieldType.TEXT,
                required=False,
                masked=False,
                help="Custom header for api_key auth mode (default: X-API-Key).",
                placeholder="X-API-Key",
            ),
            FieldSpec(
                name="auth_username",
                label="Username (for basic auth)",
                type=FieldType.TEXT,
                required=False,
                masked=False,
            ),
            FieldSpec(
                name="auth_password",
                label="Password (for basic auth)",
                type=FieldType.PASSWORD,
                required=False,
                masked=True,
            ),
        ]

    def validate_fields(
        self,
        fields: dict[str, Any],
        schema_fields: list[dict[str, Any]],
    ) -> None:
        super().validate_fields(fields, schema_fields)
        url = (fields or {}).get("url", "")
        if url:
            p = urlparse(url)
            if p.scheme not in ("http", "https"):
                raise ValidationError(
                    "url", f"unsupported scheme {p.scheme!r}; expected http or https",
                )
            if not p.netloc:
                raise ValidationError("url", "missing host")
        mode = (fields or {}).get("auth_mode") or "none"
        if mode == "basic":
            user = (fields or {}).get("auth_username", "")
            pwd = (fields or {}).get("auth_password", "")
            if not user or not pwd:
                raise ValidationError(
                    "auth_username",
                    "auth_username + auth_password required for basic auth",
                )
        elif mode in ("api_key", "bearer"):
            tok = (fields or {}).get("auth_token", "")
            if not tok:
                raise ValidationError(
                    "auth_token", f"auth_token required for {mode} auth",
                )
