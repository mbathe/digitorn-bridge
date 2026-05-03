"""Backfill installed_packages rows for apps that have an applications
row but no matching registry entry. Result of pre-fix dev deploys that
skipped the registry write.

Usage:
    py -3.12 scripts\\backfill-installed-packages.py [--dry-run]

Per-app strategy:
    1. Find applications rows whose (app_id, scope, owner) tuple has
       no installed_packages match.
    2. Synthesize a minimal local-source row from the applications row
       (yaml_path, yaml_hash already present) - source_type=local.
    3. Built-ins are handled by the daemon's bootstrap on next start
       and skipped here.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import yaml

BUILTINS = frozenset({
    "digitorn-builder",
    "digitorn-chat",
    "digitorn-clone",
    "digitorn-code",
    "digitorn-deepresearch",
    "digitorn-react-sandbox",
})


def _read_db_url() -> str:
    cfg_path = Path.home() / ".digitorn" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    url = (cfg.get("database") or {}).get("url") or ""
    if not url:
        sys.exit("FATAL: database.url missing")
    if url.startswith("postgresql+asyncpg://"):
        url = "postgresql://" + url[len("postgresql+asyncpg://"):]
    return url


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    url = _read_db_url()
    print(f"Connecting to {url.split('@')[-1].split('?')[0]} …")
    c = await asyncpg.connect(url)
    try:
        # Find applications without matching installed_packages.
        rows = await c.fetch(
            """
            SELECT a.app_id, a.scope, a.owner_user_id,
                   a.yaml_path, a.yaml_hash, a.version, a.name,
                   a.description, a.author, a.created_at
            FROM applications a
            LEFT JOIN installed_packages p
              ON p.package_id = a.app_id
             AND p.scope = a.scope
             AND COALESCE(p.owner_user_id, '') = COALESCE(a.owner_user_id, '')
            WHERE p.id IS NULL
            ORDER BY a.app_id
            """,
        )
        targets = [
            r for r in rows if r["app_id"] not in BUILTINS
        ]
        builtins_orphan = [
            r["app_id"] for r in rows if r["app_id"] in BUILTINS
        ]

        print()
        print(f"== {len(rows)} applications without registry row ({len(builtins_orphan)} builtins, {len(targets)} custom) ==")
        for r in targets:
            yp = r["yaml_path"] or "(no yaml_path)"
            print(f"  - {r['app_id']}  scope={r['scope']}  yaml={yp}")

        if not targets:
            print("Nothing to backfill.")
            return

        if args.dry_run:
            print()
            print("Dry run - no inserts performed. Re-run without --dry-run to backfill.")
            return

        print()
        print("== Backfilling ==")
        inserted = 0
        skipped = 0
        for r in targets:
            app_id = r["app_id"]
            scope = r["scope"] or "system"
            owner = r["owner_user_id"] or None
            # Registry constraint: scope='system' must NOT have an owner.
            registry_owner = owner if scope == "user" else None

            yaml_path = r["yaml_path"] or ""
            # Use the existing yaml_hash when present; otherwise hash
            # whatever YAML the daemon stored in applications row, not
            # the disk file (the disk file may have been edited since).
            h = r["yaml_hash"]
            if not h:
                # Synthesize from app_id - we just need SOMETHING unique.
                h = hashlib.sha256(app_id.encode("utf-8")).hexdigest()

            manifest = {
                "name": r["name"] or app_id,
                "version": r["version"] or "0.0.0",
                "description": r["description"] or "",
                "author": r["author"] or "",
            }

            try:
                await c.execute(
                    """
                    INSERT INTO installed_packages
                      (id, package_id, scope, owner_user_id,
                       source_type, source_uri, version, hash,
                       install_dir, manifest, status, installed_by,
                       installed_at, updated_at)
                    VALUES
                      ($1, $2, $3, $4, 'local', $5, $6, $7,
                       '', $8::jsonb, 'installed', '',
                       $9, $9)
                    """,
                    uuid.uuid4().hex,
                    app_id,
                    scope,
                    registry_owner,
                    yaml_path,
                    r["version"] or "0.0.0",
                    h,
                    json.dumps(manifest),
                    r["created_at"] or datetime.now(timezone.utc),
                )
                print(f"  OK   {app_id} (scope={scope})")
                inserted += 1
            except Exception as exc:
                print(f"  FAIL {app_id}: {exc}")
                skipped += 1

        print()
        print(f"Done. inserted={inserted} skipped={skipped}")
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
