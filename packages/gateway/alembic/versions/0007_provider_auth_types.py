"""Provider auth types: support OAuth2, basic, multi-field, mTLS, etc.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-06

The initial dashboard-writable schema (0006) modeled providers as
"slug + env_var fallback" - good enough for OpenAI/Anthropic/DeepSeek
where one env var is the whole credential. This migration generalises
to any auth shape:

  * ``auth_type`` (api_key, api_key_header, basic_auth, multi_field,
    oauth2, claude_code, custom) tells the dispatcher how to inject
    the credential into outbound requests.
  * ``auth_schema`` (JSONB) is a list of ``{name, label, kind, secret}``
    entries describing the fields the dashboard form should render and
    the runtime should expect inside the encrypted blob.

The credential row's ``encrypted_value`` blob format does not change:
the cipher already wraps an opaque bytes payload. The only difference
is that we now serialise a JSON dict into the plaintext (token+refresh+
expires for OAuth, user+password for basic, ...) instead of a single
string. Old rows (single-string) keep working - the runtime detects
JSON vs raw on decrypt.

Idempotent on re-run.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    op.execute("""
    ALTER TABLE gateway_providers
        ADD COLUMN IF NOT EXISTS auth_type VARCHAR(32) NOT NULL DEFAULT 'api_key'
    """)
    op.execute("""
    ALTER TABLE gateway_providers
        ADD COLUMN IF NOT EXISTS auth_schema JSONB NOT NULL DEFAULT '[]'::jsonb
    """)
    # Backfill existing rows: providers seeded in 0006 used a plain env_var
    # mapping that maps to auth_type=api_key with a single 'value' field.
    # SQLAlchemy parses bare ``:`` as bind params; build the JSON via
    # json_build_object so no literal ``:`` appears in the statement.
    op.execute("""
    UPDATE gateway_providers
       SET auth_schema = jsonb_build_array(jsonb_build_object(
               'name', 'value',
               'label', 'API key',
               'kind', 'secret',
               'secret', true,
               'required', true
           ))
     WHERE auth_schema = '[]'::jsonb
       AND auth_type = 'api_key'
    """)


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute("ALTER TABLE gateway_providers DROP COLUMN IF EXISTS auth_schema")
    op.execute("ALTER TABLE gateway_providers DROP COLUMN IF EXISTS auth_type")
