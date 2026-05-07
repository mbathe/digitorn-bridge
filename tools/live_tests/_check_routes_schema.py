"""Confirm migration 0010 backfilled every gateway_routes row with the
right per-route dispatch identity. Pure read-only -- no mutations."""
from __future__ import annotations

import asyncio
import os
import sys

DB_URL = (
    "postgresql+asyncpg://neondb_owner:***REMOVED***"
    "@ep-wild-forest-al4945yw.c-3.eu-central-1.aws.neon.tech/neondb"
    "?ssl=require"
)


async def main() -> int:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    eng = create_async_engine(DB_URL, pool_pre_ping=True)
    bad = 0
    async with eng.connect() as conn:
        # All routes must now have NOT-NULL identity columns.
        rows = (await conn.execute(text("""
            SELECT
                r.id, r.model_alias, r.priority,
                r.provider_slug, r.real_model_id, r.compat,
                r.base_url, r.dispatch_headers,
                m.provider_slug AS model_provider,
                m.real_model_id AS model_real
            FROM gateway_routes r
            JOIN gateway_models m ON m.alias = r.model_alias
            ORDER BY r.model_alias, r.priority
        """))).all()
        print(f"Total routes: {len(rows)}")
        if not rows:
            print("WARN: zero routes in DB. Migration applied but no data to "
                  "verify. This is fine for a fresh DB.")
            return 0
        for r in rows:
            if r.provider_slug is None or r.real_model_id is None or r.compat is None:
                print(f"  FAIL: route {r.id} ({r.model_alias} P{r.priority}) "
                      f"has null identity field(s) -- backfill missed")
                bad += 1
                continue
            # Sanity: backfilled routes should match the model's identity
            # exactly (since migration time, no operator has touched them).
            if r.provider_slug != r.model_provider:
                print(f"  WARN: route {r.id} ({r.model_alias} P{r.priority}) "
                      f"provider_slug={r.provider_slug} != model.{r.model_provider}"
                      f" -- legitimate cross-provider, or stale data")
            if r.real_model_id != r.model_real:
                print(f"  WARN: route {r.id} ({r.model_alias} P{r.priority}) "
                      f"real_model_id={r.real_model_id} != model.{r.model_real}"
                      f" -- legitimate per-route override, or stale data")
        # Aggregate stats
        agg = (await conn.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE provider_slug IS NULL) AS null_provider,
                COUNT(*) FILTER (WHERE real_model_id IS NULL) AS null_model,
                COUNT(*) FILTER (WHERE compat IS NULL) AS null_compat,
                COUNT(DISTINCT provider_slug) AS distinct_providers,
                COUNT(DISTINCT model_alias) AS distinct_aliases
            FROM gateway_routes
        """))).first()
        print(f"\nNull provider_slug: {agg.null_provider}")
        print(f"Null real_model_id: {agg.null_model}")
        print(f"Null compat:       {agg.null_compat}")
        print(f"Distinct providers in routes: {agg.distinct_providers}")
        print(f"Distinct aliases:             {agg.distinct_aliases}")
        # Confirm the FK constraint and index exist.
        idx = (await conn.execute(text("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'gateway_routes'
        """))).all()
        names = {r.indexname for r in idx}
        for needed in ("ix_gateway_routes_provider_slug",):
            if needed in names:
                print(f"OK: index {needed} present")
            else:
                print(f"FAIL: index {needed} MISSING")
                bad += 1
        # Confirm the new FK exists
        fks = (await conn.execute(text("""
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'gateway_routes'::regclass
              AND contype = 'f'
        """))).all()
        fk_names = {r.conname for r in fks}
        if "gateway_routes_provider_fk" in fk_names:
            print("OK: FK gateway_routes_provider_fk present")
        else:
            print("FAIL: FK gateway_routes_provider_fk MISSING")
            bad += 1
    await eng.dispose()
    if bad:
        print(f"\n{bad} issue(s) detected.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    os.environ.setdefault(
        "DIGITORN_GATEWAY_MASTER_KEY",
        "mlkupM2IoI7GnzNGY8g4PvsWpysnciOgMK1Yqm8qJIA=",
    )
    sys.exit(asyncio.run(main()))
