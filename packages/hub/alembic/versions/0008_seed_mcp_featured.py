"""seed mcp_featured_entries from the bundled JSON catalog

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-15

Data migration. Reads the catalog seed shipped inside the image at
``/app/src/digitorn_hub/data/mcp_catalog_seed.json`` (also available
in the source tree at the same relative path) and upserts every entry
into ``hub.mcp_featured_entries``.

Idempotent: ``ON CONFLICT (server_id) DO UPDATE`` re-applies the seed
columns on every run, so the table stays in sync with whatever shape
the JSON file describes — but admin-edited fields like
``featured_priority``, ``hidden``, ``verified_by`` are **preserved**
across runs (they're not in the JSON).

Empty / missing JSON is a soft skip — lets local devs run ``alembic
upgrade head`` without the seed file (e.g. fresh checkouts before the
export script has run).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence, Union

from alembic import op
from sqlalchemy import MetaData, Table
from sqlalchemy.dialects.postgresql import insert as pg_insert

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "hub"

# Path inside the production image (Dockerfile copies ``src/`` to ``/app/src``).
# Also resolves correctly from source checkouts (relative to the migrations dir).
_IMAGE_SEED = Path("/app/src/digitorn_hub/data/mcp_catalog_seed.json")
_LOCAL_SEED = (
    Path(__file__).resolve().parent.parent.parent
    / "src" / "digitorn_hub" / "data" / "mcp_catalog_seed.json"
)


# Columns admins are expected to tweak post-seed — preserved across runs
# instead of being clobbered by ON CONFLICT DO UPDATE.
_PROTECTED_COLS = frozenset({
    "featured_priority", "hidden", "verified_at", "verified_by",
    "last_tested_at", "last_tested_ok", "last_test_error",
})


def _seed_path() -> Path | None:
    for candidate in (_IMAGE_SEED, _LOCAL_SEED):
        if candidate.exists():
            return candidate
    return None


def upgrade() -> None:
    seed = _seed_path()
    if seed is None:
        # Soft skip — no JSON file available, nothing to seed.
        return

    payload = json.loads(seed.read_text(encoding="utf-8"))
    entries: list[dict] = payload.get("entries", [])
    if not entries:
        return

    bind = op.get_bind()
    meta = MetaData()
    meta.reflect(bind=bind, schema=SCHEMA, only=("mcp_featured_entries",))
    tbl: Table = meta.tables[f"{SCHEMA}.mcp_featured_entries"]

    now = datetime.now(timezone.utc)
    for entry in entries:
        # Ensure verification stamps exist even on fresh inserts.
        values = dict(entry)
        values.setdefault("verified_at", now)
        values.setdefault("verified_by", "seed:0008")

        stmt = pg_insert(tbl).values(**values)
        # Update everything from the JSON, but never clobber the admin
        # protected columns once they've been set in the DB.
        update_cols = {
            k: stmt.excluded[k]
            for k in values
            if k not in _PROTECTED_COLS and k != "server_id"
        }
        if update_cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=["server_id"],
                set_=update_cols,
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=["server_id"])
        bind.execute(stmt)


def downgrade() -> None:
    # Don't auto-delete admin-curated rows on a rollback. Operators can
    # truncate manually if they really want to undo.
    pass
