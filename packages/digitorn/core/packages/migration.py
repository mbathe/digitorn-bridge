"""Auto-classify pre-existing apps as `source_type='local'`."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update

logger = logging.getLogger(__name__)

async def classify_existing_apps(session_factory: Any) -> dict[str, int]:
    """Stamp every legacy Application row with source_type='local'."""
    from digitorn.core.models import Application

    summary = {"already_classified": 0, "newly_classified": 0, "errors": 0}

    try:
        async with session_factory() as db:
            # Pick up rows that bypassed the SQLAlchemy `"local"` default.
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
