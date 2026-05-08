"""Read-only diagnostic: top tables by total disk + row count, plus
overall project size. Used when Neon hits the 512MB cluster cap and
writes start failing with DiskFullError."""
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
    async with eng.connect() as conn:
        # Total DB size
        row = (await conn.execute(text(
            "SELECT pg_size_pretty(pg_database_size(current_database())) AS total, "
            "pg_database_size(current_database()) AS bytes"
        ))).first()
        total_bytes = row.bytes
        print(f"Database total size: {row.total} ({total_bytes:,} bytes)")
        print(f"Cluster cap (Neon free): 536,870,912 bytes (512 MB)")
        print(f"Used: {total_bytes / 536_870_912 * 100:.1f}%")
        print()

        # Top 20 tables by total relation size (data + indexes + toast)
        print(f"  {'TABLE':<55} {'TOTAL':>10} {'TABLE':>10} {'IDX':>10} {'TOAST':>10} {'ROWS':>15}")
        print("  " + "-" * 115)
        rows = (await conn.execute(text("""
            SELECT
                n.nspname || '.' || c.relname AS qualified,
                pg_total_relation_size(c.oid) AS total_b,
                pg_relation_size(c.oid)       AS heap_b,
                pg_indexes_size(c.oid)        AS idx_b,
                COALESCE(pg_total_relation_size(c.reltoastrelid), 0) AS toast_b,
                c.reltuples::bigint AS approx_rows
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r'
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY total_b DESC
            LIMIT 20
        """))).all()
        for r in rows:
            print(f"  {r.qualified:<55} {_h(r.total_b):>10} {_h(r.heap_b):>10} "
                  f"{_h(r.idx_b):>10} {_h(r.toast_b):>10} {r.approx_rows:>15,}")

        # Free space in tables (vacuum opportunity)
        print()
        print("Top 10 tables with high dead-tuple ratio (vacuum could help):")
        try:
            rows = (await conn.execute(text("""
                SELECT
                    schemaname || '.' || relname AS qualified,
                    n_live_tup, n_dead_tup,
                    CASE WHEN n_live_tup > 0
                         THEN ROUND(100.0 * n_dead_tup / GREATEST(n_live_tup, 1), 1)
                         ELSE 0 END AS dead_pct,
                    last_autovacuum
                FROM pg_stat_user_tables
                WHERE n_dead_tup > 1000
                ORDER BY n_dead_tup DESC
                LIMIT 10
            """))).all()
            if not rows:
                print("  (none)")
            for r in rows:
                print(f"  {r.qualified:<55} live={r.n_live_tup:>10,} "
                      f"dead={r.n_dead_tup:>10,}  {r.dead_pct}%  "
                      f"last_vac={r.last_autovacuum}")
        except Exception as exc:
            print(f"  (cannot read pg_stat_user_tables: {exc})")

        # Count of history_log rows per app + per kind (the suspect)
        print()
        try:
            r = (await conn.execute(text(
                "SELECT COUNT(*) AS n FROM history_log"
            ))).first()
            if r:
                print(f"history_log total rows: {r.n:,}")
            r = (await conn.execute(text("""
                SELECT type, COUNT(*) AS n
                FROM history_log
                GROUP BY type
                ORDER BY n DESC
                LIMIT 10
            """))).all()
            for x in r:
                print(f"  {x.type:<40} {x.n:>10,}")
        except Exception as exc:
            print(f"  (cannot read history_log: {exc})")

    await eng.dispose()
    return 0


def _h(b: int) -> str:
    if b is None:
        return "-"
    for unit in ("B", "K", "M", "G"):
        if b < 1024:
            return f"{b:.0f}{unit}"
        b /= 1024
    return f"{b:.0f}T"


if __name__ == "__main__":
    os.environ.setdefault(
        "DIGITORN_GATEWAY_MASTER_KEY",
        "mlkupM2IoI7GnzNGY8g4PvsWpysnciOgMK1Yqm8qJIA=",
    )
    sys.exit(asyncio.run(main()))
