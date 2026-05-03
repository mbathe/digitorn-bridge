"""Quick diagnostic: does the user_id have a row in `users`, and does
history_log carry any rows for that user_id ?"""
from __future__ import annotations
import asyncio, sys
from pathlib import Path
import asyncpg, yaml

cfg = yaml.safe_load((Path.home() / ".digitorn" / "config.yaml").read_text(encoding="utf-8"))
url = (cfg.get("database") or {}).get("url") or ""
if url.startswith("postgresql+asyncpg://"):
    url = "postgresql://" + url[len("postgresql+asyncpg://"):]

UID = None  # resolved by email below
EMAIL = "mbathepaul@gmail.com"


async def main() -> None:
    c = await asyncpg.connect(url)

    print(f"=== users WHERE email={EMAIL} ===")
    rows = await c.fetch(
        "SELECT id, email, created_at FROM users WHERE email = $1",
        EMAIL,
    )
    if not rows:
        print(f"  NOT FOUND - {EMAIL}")
        return
    for r in rows:
        print(f"  {dict(r)}")
    UID = rows[0]["id"]

    print()
    print("=== history_log rows for this user_id (last 5) ===")
    rows = await c.fetch(
        "SELECT id, kind, type, session_id, ts "
        "FROM history_log WHERE user_id = $1 ORDER BY id DESC LIMIT 5",
        UID,
    )
    for r in rows:
        print(f"  {dict(r)}")
    if not rows:
        print("  (none — confirms persist is failing)")

    print()
    print("=== history_log rows for copilot-smoke (any user, last 5) ===")
    rows = await c.fetch(
        "SELECT id, kind, type, user_id, ts "
        "FROM history_log WHERE app_id = 'copilot-smoke' ORDER BY id DESC LIMIT 5",
    )
    for r in rows:
        print(f"  {dict(r)}")

    print()
    print("=== users with any row (top 5) ===")
    rows = await c.fetch("SELECT id, email FROM users ORDER BY created_at DESC LIMIT 5")
    for r in rows:
        print(f"  {dict(r)}")

    await c.close()

asyncio.run(main())
