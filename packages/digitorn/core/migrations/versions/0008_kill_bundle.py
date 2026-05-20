"""Kill bundle: drop app_bundles table + current_bundle_id FK + install_dir column.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-15

The bundle concept is removed. `~/.digitorn/apps/<scoped>/` is the
sole on-disk source-of-truth. The deterministic path makes
`installed_packages.install_dir` redundant (computed at read time)
and the entire `app_bundles` table dead.

Operations (idempotent, Postgres + SQLite):

1. DROP FK `applications.current_bundle_id` -> `app_bundles.id`
   (created with `use_alter=True` so the constraint name is stable).
2. DROP COLUMN `applications.current_bundle_id`.
3. DROP COLUMN `installed_packages.install_dir`.
4. DROP TABLE `app_bundles`.

No data is migrated. Bundles on disk under
`~/.digitorn/apps/<app_id>/bundle-<hash>/` are NOT removed by this
migration -- the bootstrap self-heal will reinstall builtins on next
boot and old bundle dirs become orphan files that can be wiped
manually.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect, text


revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, name: str) -> bool:
    insp = inspect(bind)
    return name in insp.get_table_names()


def _has_column(bind, table: str, column: str) -> bool:
    insp = inspect(bind)
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1: drop `app_bundles` first; SQLite won't drop a column whose
    # FK target still exists.
    if _has_table(bind, "app_bundles"):
        op.drop_table("app_bundles")

    # 2: drop the FK constraint + column applications.current_bundle_id.
    if _has_column(bind, "applications", "current_bundle_id"):
        if dialect == "postgresql":
            # FK constraint name is not deterministic; resolve from
            # information_schema before dropping.
            bind.execute(text("""
                DO $$
                DECLARE
                    fk_name text;
                BEGIN
                    SELECT tc.constraint_name INTO fk_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                    WHERE tc.table_name = 'applications'
                      AND tc.constraint_type = 'FOREIGN KEY'
                      AND kcu.column_name = 'current_bundle_id'
                    LIMIT 1;
                    IF fk_name IS NOT NULL THEN
                        EXECUTE format(
                            'ALTER TABLE applications DROP CONSTRAINT %I',
                            fk_name
                        );
                    END IF;
                END $$;
            """))
            bind.execute(text(
                "DROP INDEX IF EXISTS ix_applications_current_bundle_id"
            ))
            op.drop_column("applications", "current_bundle_id")
        else:
            # SQLite: batch_alter rebuilds the table (FK target is now
            # already dropped above so the rebuild's schema check passes).
            with op.batch_alter_table("applications") as batch:
                batch.drop_column("current_bundle_id")

    # 3: drop install_dir from installed_packages.
    if _has_column(bind, "installed_packages", "install_dir"):
        if dialect == "postgresql":
            op.drop_column("installed_packages", "install_dir")
        else:
            with op.batch_alter_table("installed_packages") as batch:
                batch.drop_column("install_dir")


def downgrade() -> None:
    # Not reversible cleanly: re-creating app_bundles + current_bundle_id
    # without backfill would just leave the daemon in a half-broken state.
    raise NotImplementedError(
        "0002_kill_bundle is one-way. Restore from a pre-migration backup."
    )
