"""Show provider/external_id for each users row of a given email."""
from __future__ import annotations
import asyncio
from pathlib import Path
import asyncpg, yaml

cfg = yaml.safe_load((Path.home() / ".digitorn" / "config.yaml").read_text(encoding="utf-8"))
url = (cfg.get("database") or {}).get("url") or ""
if url.startswith("postgresql+asyncpg://"):
    url = "postgresql://" + url[len("postgresql+asyncpg://"):]

EMAIL = "mbathepaul@gmail.com"


async def main() -> None:
    c = await asyncpg.connect(url)
    rows = await c.fetch(
        "SELECT id, email, app_id, provider, external_id, "
        "       display_name, last_seen_at, created_at "
        "FROM users WHERE email = $1 ORDER BY created_at ASC",
        EMAIL,
    )
    for r in rows:
        print(dict(r))
    await c.close()

asyncio.run(main())
