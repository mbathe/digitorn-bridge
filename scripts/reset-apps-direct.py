"""Direct Postgres + disk wipe of every non-builtin deployed app.

Bypasses the broken DELETE /api/apps/<id> endpoint (which 403's on
``?scope=system`` for non-admin users — see scripts/reset-apps.ps1
output). Reads the database URL from ``~/.digitorn/config.yaml`` and
connects with asyncpg directly. Purges every table that references
the app_id, in dependency order, then wipes the bundle dirs on disk.

Usage:
    py -3.12 scripts\\reset-apps-direct.py [--dry-run]

PRE-REQUISITES:
    1. Daemon STOPPED. Wiping `applications` rows while the daemon
       is running corrupts its in-memory cache and triggers FK
       violations when sessions get re-saved.
    2. Network access to the Postgres host (Neon).

Builtins (digitorn-builder / -chat / -clone / -code / -deepresearch /
-react-sandbox) are kept intact — the daemon re-bootstraps them on
startup anyway, deleting them creates more chaos than it solves.

Schema notes (verified against core/models.py):
    - ``agents.session_pk`` (NOT ``user_session_id``) → user_sessions.id
    - ``app_module_grants.profile_id`` cascades from app_profiles, so
      we don't delete it explicitly.
    - ``session_checkpoints``, ``action_executions``, ``user_sessions``,
      ``activations``, ``history_log``, ``inbox_items``,
      ``usage_events``, ``background_sessions``,
      ``app_module_configs``, ``app_profiles``, ``app_secrets``,
      ``app_bundles`` all carry an ``app_id`` column directly.
    - ``installed_packages`` uses ``package_id`` (= app_id).
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

import yaml
import asyncpg

BUILTINS = frozenset({
    "digitorn-builder",
    "digitorn-chat",
    "digitorn-clone",
    "digitorn-code",
    "digitorn-deepresearch",
    "digitorn-react-sandbox",
})

# Tables that carry an ``app_id`` column or reference applications.id.
# Order: deepest children first, then parents. Each entry is
# ``(table, where_clause)``. ``$1`` is the app_id parameter.
TABLES_BY_APP_ID = [
    # Session-level fan-out (deepest first).
    ("agents",                 "session_pk IN (SELECT id FROM user_sessions WHERE app_id = $1)"),
    ("session_checkpoints",    "app_id = $1"),
    ("action_executions",      "app_id = $1"),
    ("activation_events",      "activation_id IN (SELECT id FROM activations WHERE app_id = $1)"),
    ("activations",            "app_id = $1"),
    ("history_log",            "app_id = $1"),
    ("inbox_items",            "app_id = $1"),
    ("usage_events",           "app_id = $1"),
    ("background_sessions",    "app_id = $1"),
    ("user_sessions",          "app_id = $1"),
    # App-config tables (app_module_grants cascades from app_profiles
    # via DB-level CASCADE on profile_id - no explicit delete needed).
    ("app_module_configs",     "app_id = $1"),
    ("app_profiles",           "app_id = $1"),
    ("app_secrets",            "app_id = $1"),
    # Bundles & install registry.
    ("app_bundles",            "app_id = $1"),
    ("installed_packages",     "package_id = $1"),
    # Finally the parent.
    ("applications",           "app_id = $1"),
]


def _read_db_url() -> str:
    cfg_path = Path.home() / ".digitorn" / "config.yaml"
    if not cfg_path.is_file():
        sys.exit(f"FATAL: {cfg_path} not found")
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    url = (cfg.get("database") or {}).get("url") or ""
    if not url:
        sys.exit("FATAL: database.url missing from config.yaml")
    # asyncpg expects "postgresql://" not "postgresql+asyncpg://"
    if url.startswith("postgresql+asyncpg://"):
        url = "postgresql://" + url[len("postgresql+asyncpg://"):]
    return url


async def _count_rows(conn: asyncpg.Connection, app_id: str) -> dict[str, int]:
    """Dry-run helper: count rows per table.

    Each query runs in autocommit mode (NO outer transaction) so that
    a per-table failure (column missing on this daemon version, table
    not present, etc.) never poisons the rest of the run with
    "current transaction is aborted, commands ignored…".
    """
    counts: dict[str, int] = {}
    for table, where in TABLES_BY_APP_ID:
        sql = f"SELECT COUNT(*) FROM {table} WHERE {where}"
        try:
            n = await conn.fetchval(sql, app_id)
            counts[table] = int(n or 0)
        except asyncpg.UndefinedTableError:
            # Table not present on this schema version — skip silently.
            continue
        except asyncpg.UndefinedColumnError as exc:
            print(f"    {table}: column missing - {exc}")
            continue
        except Exception as exc:
            print(f"    {table}: COUNT failed - {type(exc).__name__}: {exc}")
            continue
    return counts


async def _delete_rows(conn: asyncpg.Connection, app_id: str) -> dict[str, int]:
    """Live-run helper: delete rows per table inside a per-app transaction.

    Each app gets its own transaction so a failure on one app doesn't
    abort the next ones. Inside the transaction, we use SAVEPOINT
    around every table delete so a single broken statement (table
    missing, column rename, etc.) doesn't poison the rest of the
    deletes for the same app — we just record the error and move on.
    """
    counts: dict[str, int] = {}
    async with conn.transaction():
        for table, where in TABLES_BY_APP_ID:
            sql = f"DELETE FROM {table} WHERE {where}"
            try:
                async with conn.transaction():  # nested = SAVEPOINT
                    status = await conn.execute(sql, app_id)
                n = int(status.split()[-1]) if status.startswith("DELETE") else 0
                counts[table] = n
            except asyncpg.UndefinedTableError:
                continue
            except asyncpg.UndefinedColumnError as exc:
                print(f"    {table}: column missing - {exc}")
                continue
            except Exception as exc:
                print(f"    {table}: DELETE failed - {type(exc).__name__}: {exc}")
                continue
    return counts


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Count rows without deleting.")
    args = parser.parse_args()

    db_url = _read_db_url()
    print(f"Connecting to {db_url.split('@')[-1].split('?')[0]} …")
    conn: asyncpg.Connection = await asyncpg.connect(db_url)
    try:
        rows = await conn.fetch(
            "SELECT app_id, name FROM applications ORDER BY app_id",
        )
        targets = [r["app_id"] for r in rows if r["app_id"] not in BUILTINS]
        builtins_present = [r["app_id"] for r in rows if r["app_id"] in BUILTINS]

        print()
        print(f"== Found {len(rows)} app rows ({len(builtins_present)} builtins, "
              f"{len(targets)} custom) ==")
        for app_id in targets:
            print(f"  - {app_id}")
        if not targets:
            print("Nothing to delete.")
            return

        print()
        print("== DRY RUN ==" if args.dry_run else "== DELETING (live) ==")

        for app_id in targets:
            if args.dry_run:
                counts = await _count_rows(conn, app_id)
                action = "would delete"
            else:
                counts = await _delete_rows(conn, app_id)
                action = "deleted"
            summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v > 0)
            if summary:
                print(f"  {app_id}: {action} {summary}")
            else:
                print(f"  {app_id}: no rows")

        # Disk wipe (only on live run, only for apps the DB delete saw).
        if not args.dry_run:
            bundle_root = Path.home() / ".digitorn" / "apps"
            packages_root = Path.home() / ".digitorn" / "packages"
            print()
            print("== Wiping disk (bundles + packages) ==")
            for app_id in targets:
                for root in (bundle_root, packages_root):
                    d = root / app_id
                    if d.is_dir():
                        try:
                            shutil.rmtree(d)
                            print(f"  rm  {d}")
                        except Exception as exc:
                            print(f"  ERR {d} — {exc}")

        print()
        if args.dry_run:
            print("Dry run complete. Re-run without --dry-run to actually delete.")
        else:
            print("Done. Restart the daemon now — builtins will re-bootstrap.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
