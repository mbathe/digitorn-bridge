"""Check what github_copilot credentials exist for the CLI user."""
from __future__ import annotations
import asyncio
from pathlib import Path
import asyncpg, yaml

cfg = yaml.safe_load((Path.home() / ".digitorn" / "config.yaml").read_text(encoding="utf-8"))
url = (cfg.get("database") or {}).get("url") or ""
if url.startswith("postgresql+asyncpg://"):
    url = "postgresql://" + url[len("postgresql+asyncpg://"):]

UID = "d117b18fa131482b802acbb6aa82318a"


async def main() -> None:
    c = await asyncpg.connect(url)
    print("=== credentials for live-test ===")
    rows = await c.fetch(
        "SELECT id, name, label, scope, user_id, provider_name, "
        "provider_type, status, created_at "
        "FROM credentials WHERE user_id = $1 OR scope = 'system_wide' "
        "ORDER BY created_at DESC",
        UID,
    )
    for r in rows:
        print(f"  {dict(r)}")
    if not rows:
        print("  (none)")
    await c.close()

asyncio.run(main())
