"""DatabaseFieldsHandler - structured DB connection (host + port + ...).

An alternative to `connection_string` for users who prefer separate
fields over a URL. The injector composes the connection string from
the fields at runtime, OR exposes them individually depending on
what the consumer module expects.

Useful when:
  - The user pastes credentials from a form their DBA shared.
  - Special characters in the password would need percent-encoding
    in a URL (the user pastes raw, the injector encodes on the fly).
  - The consumer module wants the parts separately (Postgres
    psycopg2 typically takes them as separate kwargs).

Fields: host, port, username, password, database, ssl_mode, options.
"""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.credentials.handler import CredentialHandler

logger = logging.getLogger(__name__)


class DatabaseFieldsHandler(CredentialHandler):
    provider_type = "database_fields"
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
                name="driver",
                label="Driver",
                type=FieldType.SELECT,
                required=True,
                masked=False,
                default="postgresql",
                choices=[
                    ("postgresql", "PostgreSQL"),
                    ("mysql", "MySQL / MariaDB"),
                    ("mongodb", "MongoDB"),
                    ("redis", "Redis"),
                    ("sqlserver", "SQL Server"),
                    ("clickhouse", "ClickHouse"),
                    ("snowflake", "Snowflake"),
                ],
                inject_path_default="{block}.config.driver",
            ),
            FieldSpec(
                name="host",
                label="Host",
                type=FieldType.TEXT,
                required=True,
                masked=False,
                placeholder="db.internal or 10.0.0.5",
                inject_path_default="{block}.config.host",
            ),
            FieldSpec(
                name="port",
                label="Port",
                type=FieldType.NUMBER,
                required=True,
                masked=False,
                inject_path_default="{block}.config.port",
            ),
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
            FieldSpec(
                name="database",
                label="Database name",
                type=FieldType.TEXT,
                required=False,
                masked=False,
                help="Default DB to connect to (some drivers require, others don't).",
                inject_path_default="{block}.config.database",
            ),
            FieldSpec(
                name="ssl_mode",
                label="SSL mode",
                type=FieldType.SELECT,
                required=False,
                masked=False,
                default="prefer",
                choices=[
                    ("disable", "Disable - no SSL"),
                    ("allow", "Allow - try SSL, fall back to plain"),
                    ("prefer", "Prefer - try SSL first (default)"),
                    ("require", "Require - SSL or fail"),
                    ("verify-ca", "Verify CA"),
                    ("verify-full", "Verify full (CA + hostname)"),
                ],
                inject_path_default="{block}.config.ssl_mode",
            ),
            FieldSpec(
                name="options",
                label="Extra options (JSON)",
                type=FieldType.JSON,
                required=False,
                masked=False,
                help="Driver-specific options merged into the connection.",
                placeholder='{"connect_timeout": 5}',
            ),
        ]

    async def test_live_connection(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Build a connection URL from the fields and try to open it.

        Delegates to the same ``_probe_db`` helper used by the
        ``connection_string`` handler so both code paths give identical
        feedback (Connected (postgres) / Timeout / etc.).
        """
        from urllib.parse import quote
        from digitorn.core.credentials.handlers.connection_string import (
            _probe_db,
        )
        f = fields or {}
        driver = (f.get("driver") or "postgresql").lower()
        host = (f.get("host") or "").strip()
        if not host:
            return False, "host is required"
        port = f.get("port")
        user = f.get("username") or ""
        pwd = f.get("password") or ""
        db = f.get("database") or ""
        scheme_map = {
            "postgresql": "postgres",
            "mysql": "mysql",
            "mariadb": "mysql",
            "mongodb": "mongodb",
            "redis": "redis",
            "sqlserver": "mssql",
            "clickhouse": "clickhouse",
            "snowflake": "snowflake",
        }
        scheme = scheme_map.get(driver, driver)
        userinfo = ""
        if user:
            userinfo = quote(str(user), safe="")
            if pwd:
                userinfo += ":" + quote(str(pwd), safe="")
            userinfo += "@"
        port_str = f":{port}" if port else ""
        path = f"/{db}" if db else ""
        url = f"{scheme}://{userinfo}{host}{port_str}{path}"
        return await _probe_db(scheme, url)
