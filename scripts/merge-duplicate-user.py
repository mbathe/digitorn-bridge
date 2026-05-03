"""Merge a duplicate-email user into a single canonical id.

Background
----------
auth.digitorn.ai issued two `users` rows for ``mbathepaul@gmail.com``
(created 2026-04-27 and 2026-04-28). All real activity (724 messages,
48 sessions, 3 credentials) lives under the older id. The newer id
has 33 events and 1 credential - looks like a stray re-registration.

Result of this split: depending on which id the JWT carries today,
the user sees ALL their history (older id) or NOTHING (newer id),
because every session/credential lookup filters by user_id.

Fix
---
Pick ONE canonical id (the one with the most data) and re-point every
related row to it. Then delete the dup ``users`` row.

Tables to repoint (subset; covers the user's user-visible state):
  - user_sessions.user_id
  - history_log.user_id
  - history_log.actor_user_id
  - credentials.user_id
  - inbox_items.user_id
  - usage_events.user_id
  - background_sessions.user_id
  - installed_packages.owner_user_id (only when scope='user')
  - applications.owner_user_id (only when scope='user')
  - api_keys.user_id  (auth artifacts that may also be split)
  - refresh_tokens.user_id

Usage:
    py -3.12 scripts\\merge-duplicate-user.py --email mbathepaul@gmail.com [--dry-run]
"""
from __future__ import annotations
import argparse
import asyncio
from pathlib import Path
import asyncpg, yaml

cfg = yaml.safe_load((Path.home() / ".digitorn" / "config.yaml").read_text(encoding="utf-8"))
url = (cfg.get("database") or {}).get("url") or ""
if url.startswith("postgresql+asyncpg://"):
    url = "postgresql://" + url[len("postgresql+asyncpg://"):]


# (table, user-id-column-name, optional-extra-where)
REPOINT_TABLES: list[tuple[str, str, str | None]] = [
    ("user_sessions",       "user_id", None),
    ("history_log",         "user_id", None),
    ("history_log",         "actor_user_id", None),
    ("credentials",         "user_id", None),
    ("inbox_items",         "user_id", None),
    ("inbox_devices",       "user_id", None),
    ("usage_events",        "user_id", None),
    ("background_sessions", "user_id", None),
    ("installed_packages",  "owner_user_id", "scope = 'user'"),
    ("applications",        "owner_user_id", "scope = 'user'"),
    ("app_bundles",         "owner_user_id", "scope = 'user'"),
    ("api_keys",            "user_id", None),
    ("refresh_tokens",      "user_id", None),
    ("user_roles",          "user_id", None),
]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    c = await asyncpg.connect(url)
    try:
        users = await c.fetch(
            "SELECT id, email, created_at FROM users WHERE email = $1 ORDER BY created_at ASC",
            args.email,
        )
        if len(users) <= 1:
            print(f"users={len(users)} - nothing to merge.")
            return

        # Pick the id with the most history_log rows as canonical.
        scored: list[tuple[str, int]] = []
        for u in users:
            n = await c.fetchval(
                "SELECT COUNT(*) FROM history_log WHERE user_id = $1", u["id"],
            )
            scored.append((u["id"], int(n or 0)))
        scored.sort(key=lambda x: x[1], reverse=True)
        canonical_id, canonical_n = scored[0]
        dups = [uid for (uid, _n) in scored[1:]]

        print(f"== Email {args.email!r} → {len(users)} rows ==")
        for uid, n in scored:
            tag = "(canonical)" if uid == canonical_id else "(dup)"
            print(f"  {tag} {uid}  history_rows={n}")

        for dup_id in dups:
            print()
            print(f"== Repointing {dup_id} → {canonical_id} ==")
            for table, col, extra in REPOINT_TABLES:
                where = f"{col} = $1"
                if extra:
                    where += f" AND {extra}"
                count_sql = f"SELECT COUNT(*) FROM {table} WHERE {where}"
                upd_sql = f"UPDATE {table} SET {col} = $2 WHERE {where}"
                try:
                    n = await c.fetchval(count_sql, dup_id) or 0
                except asyncpg.UndefinedTableError:
                    continue
                except asyncpg.UndefinedColumnError:
                    continue
                except Exception as exc:
                    print(f"    {table}.{col}: COUNT failed - {exc}")
                    continue
                if n == 0:
                    continue
                if args.dry_run:
                    print(f"    {table}.{col} would update {n} rows")
                else:
                    try:
                        async with c.transaction():
                            await c.execute(upd_sql, dup_id, canonical_id)
                        print(f"    {table}.{col} updated {n} rows")
                    except Exception as exc:
                        print(f"    {table}.{col} FAILED - {exc}")

            if not args.dry_run:
                try:
                    async with c.transaction():
                        await c.execute("DELETE FROM users WHERE id = $1", dup_id)
                    print(f"  Deleted users row {dup_id}")
                except Exception as exc:
                    print(f"  Delete users row FAILED - {exc}")
        print()
        print("Done." if not args.dry_run else "Dry run - re-run without --dry-run.")
    finally:
        await c.close()

asyncio.run(main())
