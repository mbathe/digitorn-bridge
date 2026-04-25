"""Auto-classify pre-existing apps as ``source_type='local'``.

When the daemon boots with the new schema for the first time,
existing rows in the ``applications`` table predate the AppPackages
system. They have no ``source_type`` attribution.

This migration runs once at boot, finds every Application row that
hasn't been classified yet, and stamps ``source_type='local'``.
``package_id`` stays NULL because these apps were deployed via the
legacy ``POST /api/apps/deploy`` route, not via a package.

The migration is **idempotent and safe**:

- It only touches rows that are unset
- It never deletes data
- A failure on one row doesn't block the others
- It logs a summary so the admin can see what got migrated
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update

logger = logging.getLogger(__name__)


async def classify_existing_apps(session_factory: Any) -> dict[str, int]:
    """Stamp every legacy Application row with source_type='local'.

    Returns a summary dict::

        {"already_classified": int, "newly_classified": int, "errors": int}

    Called once from the daemon lifespan after the database is
    initialised but before any app is reloaded. Safe to call
    multiple times — only rows where ``source_type`` is empty get
    touched.
    """
    from digitorn.core.models import Application

    summary = {"already_classified": 0, "newly_classified": 0, "errors": 0}

    try:
        async with session_factory() as db:
            # SQLAlchemy default value is "local" so newly-created
            # rows already have it. We're looking for rows where the
            # column is empty/NULL (in case the migration hits a row
            # that bypassed the model defaults somehow).
            rows = await db.execute(
                select(Application).where(
                    (Application.source_type.is_(None))
                    | (Application.source_type == "")
                )
            )
            for row in rows.scalars():
                try:
                    row.source_type = "local"
                    summary["newly_classified"] += 1
                except Exception as exc:
                    logger.warning(
                        "classify_existing_apps: failed for %s: %s",
                        row.app_id, exc,
                    )
                    summary["errors"] += 1

            # Count the rows already correct (for the summary)
            already = await db.execute(
                select(Application).where(Application.source_type == "local")
            )
            summary["already_classified"] = (
                len(already.scalars().all()) - summary["newly_classified"]
            )

            await db.commit()

        if summary["newly_classified"] > 0:
            logger.info(
                "AppPackages migration: classified %d legacy app(s) as source_type='local'",
                summary["newly_classified"],
            )
    except Exception as exc:
        logger.error(
            "AppPackages migration failed (continuing without it): %s", exc,
        )

    return summary
