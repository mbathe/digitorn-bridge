"""McpServerHandler - real lifecycle bridge to ``MCPConnectionPool``.

An MCP credential is different from every other credential type
because the credential **is** the configuration of a live process
(or network connection). When the user fills in the fields, we need
to spawn the process; when they delete the credential, we kill it.

The bridge
----------

The handler talks to ``digitorn.modules.mcp.connections.MCPConnectionPool``
which already knows how to:

- spawn a stdio subprocess with a custom env
- merge env vars safely (via ``build_safe_env``)
- connect to SSE / streamable HTTP MCP servers
- keep an in-memory registry of live entries
- reconnect on failure

So this handler is essentially a **translator** between the
``credentials_schema`` YAML shape and ``pool.connect()``'s keyword
arguments. No subprocess code lives here - it's all in the
existing pool.

Transport mapping::

    schema transport    →   pool transport_type
    ────────────────────    ───────────────────
    stdio               →   stdio
    http                →   streamable_http
    ws                  →   (not supported by the pool today → error)

Env var substitution
--------------------

The schema can declare ``env_template``::

    env_template:
      NOTION_API_KEY: "{{field.api_key}}"
      NOTION_WORKSPACE: "{{field.workspace}}"

The handler walks this dict and substitutes each ``{{field.X}}``
reference with the corresponding value from the filled credential.
The resulting env dict is handed to ``pool.connect(env=...)``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from digitorn.core.credentials.handler import CredentialHandler

logger = logging.getLogger(__name__)


# Translates the high-level schema transport name to the pool's
# internal transport_type constants. The pool uses "sse" and
# "streamable_http" for the two HTTP-based transports - we surface
# the newer one as the default "http" mapping because it's what
# most MCP servers recommend in 2025+.
_TRANSPORT_MAP = {
    "stdio": "stdio",
    "http": "streamable_http",
    "sse": "sse",
}


class McpServerHandler(CredentialHandler):
    provider_type = "mcp_server"

    def validate_fields(
        self,
        fields: dict[str, Any],
        schema_fields: list[dict[str, Any]],
    ) -> None:
        super().validate_fields(fields, schema_fields)

    async def on_credential_filled(
        self,
        credential: dict[str, Any],
        schema_provider: dict[str, Any],
        ctx: Any,
    ) -> None:
        """Register + connect the MCP server using the filled credential."""
        pool = getattr(ctx, "mcp_pool", None)
        if pool is None:
            logger.debug(
                "McpServerHandler: no mcp_pool in ctx - skipping spawn "
                "for %s (the process will be started when the MCP module "
                "comes online)",
                credential.get("provider_name"),
            )
            return

        server_id = schema_provider.get("name") or credential.get(
            "provider_name"
        )
        if not server_id:
            logger.warning(
                "McpServerHandler: cannot spawn MCP without server_id"
            )
            return

        # Map transport
        raw_transport = (schema_provider.get("transport") or "stdio").lower()
        transport_type = _TRANSPORT_MAP.get(raw_transport)
        if transport_type is None:
            logger.warning(
                "McpServerHandler: transport %r not supported by the pool "
                "(supported: %s) - skipping %s",
                raw_transport, sorted(_TRANSPORT_MAP), server_id,
            )
            return

        # Build env from env_template + credential fields
        env = _render_env_template(
            schema_provider.get("env_template") or {},
            credential.get("fields") or {},
        )

        # Build connect kwargs based on transport
        connect_kwargs: dict[str, Any] = {"env": env}
        if transport_type == "stdio":
            command = schema_provider.get("command") or []
            if not command:
                logger.warning(
                    "McpServerHandler: stdio server %s has no command, skipping",
                    server_id,
                )
                return
            # The pool's connect() expects command (str) + args (list) for stdio
            connect_kwargs["command"] = command[0]
            connect_kwargs["args"] = command[1:]
        else:
            # HTTP / SSE - need a URL
            url = schema_provider.get("url") or ""
            if not url:
                logger.warning(
                    "McpServerHandler: %s transport %s has no URL, skipping",
                    server_id, transport_type,
                )
                return
            connect_kwargs["url"] = url
            # Allow env_template to supply custom headers for auth
            headers = {
                k[len("HEADER_"):]: v
                for k, v in env.items()
                if k.startswith("HEADER_")
            }
            if headers:
                connect_kwargs["headers"] = headers

        # Disconnect any stale instance first - calling connect on an
        # existing server_id would conflict with the pool's internal map.
        try:
            existing = pool.get_server(server_id)
            if existing is not None:
                await pool.disconnect(server_id)
        except Exception as exc:
            logger.debug(
                "McpServerHandler: pre-disconnect of %s failed: %s",
                server_id, exc,
            )

        # Connect
        try:
            await pool.connect(server_id, transport_type, **connect_kwargs)
            logger.info(
                "McpServerHandler: %s connected (transport=%s)",
                server_id, transport_type,
            )
        except Exception as exc:
            logger.warning(
                "McpServerHandler: connect failed for %s: %s",
                server_id, exc,
            )

    async def on_credential_removed(
        self,
        credential: dict[str, Any],
        schema_provider: dict[str, Any],
        ctx: Any,
    ) -> None:
        """Kill the MCP process (or disconnect HTTP) before the row is deleted."""
        pool = getattr(ctx, "mcp_pool", None)
        if pool is None:
            return
        server_id = schema_provider.get("name") or credential.get("provider_name")
        if not server_id:
            return
        try:
            await pool.disconnect(server_id)
            logger.info("McpServerHandler: %s disconnected", server_id)
        except Exception as exc:
            logger.debug(
                "McpServerHandler: disconnect of %s failed (%s)",
                server_id, exc,
            )


# ────────────────────────────────────────────────────────────────────
# Env template substitution
# ────────────────────────────────────────────────────────────────────


_FIELD_PATTERN = re.compile(r"\{\{field\.([a-zA-Z0-9_]+)\}\}")


def _render_env_template(
    template: dict[str, str],
    fields: dict[str, Any],
) -> dict[str, str]:
    """Substitute ``{{field.X}}`` references with credential field values.

    Example::

        template = {"NOTION_API_KEY": "{{field.api_key}}"}
        fields   = {"api_key": "secret_abc"}
        → {"NOTION_API_KEY": "secret_abc"}

    Fields that don't exist in ``fields`` become empty strings, so
    the subprocess just sees an empty env var rather than a literal
    ``{{field.X}}`` string (which would be a clear bug).
    """
    out: dict[str, str] = {}
    for k, raw_value in (template or {}).items():
        if not isinstance(raw_value, str):
            out[k] = str(raw_value)
            continue
        rendered = _FIELD_PATTERN.sub(
            lambda m: str(fields.get(m.group(1), "")), raw_value,
        )
        out[k] = rendered
    return out
