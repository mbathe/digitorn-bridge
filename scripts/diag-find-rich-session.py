"""Find a session of the user that has tool_call events."""
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
    user = await c.fetchrow("SELECT id FROM users WHERE email = $1 LIMIT 1", EMAIL)
    if user is None:
        print("no user"); return
    rows = await c.fetch(
        "SELECT session_id, COUNT(*) FILTER (WHERE type = 'tool_call') AS tools, "
        "       COUNT(*) FILTER (WHERE type = 'user_message') AS users, "
        "       COUNT(*) AS total, MAX(ts) AS last "
        "FROM history_log WHERE user_id = $1 GROUP BY session_id "
        "HAVING COUNT(*) FILTER (WHERE type = 'tool_call') > 2 "
        "ORDER BY last DESC LIMIT 5",
        user["id"],
    )
    for r in rows:
        print(f"sid={r['session_id']} tools={r['tools']} users={r['users']} total={r['total']} last={r['last']}")
    await c.close()


asyncio.run(main())
