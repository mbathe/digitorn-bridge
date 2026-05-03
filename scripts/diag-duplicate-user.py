"""Show which user_id is currently used for each subsystem when an
email has multiple rows (auth desync between Postgres + remote auth)."""
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

    print(f"=== users WHERE email={EMAIL} ===")
    users = await c.fetch(
        "SELECT id, email, created_at FROM users WHERE email = $1",
        EMAIL,
    )
    uids = [u["id"] for u in users]
    for u in users:
        print(f"  {dict(u)}")

    for uid in uids:
        print()
        print(f"=== Activity for user_id={uid} ===")
        sessions = await c.fetchval(
            "SELECT COUNT(*) FROM user_sessions WHERE user_id = $1", uid,
        )
        history_msg = await c.fetchval(
            "SELECT COUNT(*) FROM history_log WHERE user_id = $1 AND kind = 'message'",
            uid,
        )
        history_evt = await c.fetchval(
            "SELECT COUNT(*) FROM history_log WHERE user_id = $1 AND kind = 'event'",
            uid,
        )
        creds = await c.fetchval(
            "SELECT COUNT(*) FROM credentials WHERE user_id = $1", uid,
        )
        last_seen = await c.fetchval(
            "SELECT MAX(ts) FROM history_log WHERE user_id = $1", uid,
        )
        print(f"  sessions={sessions} hist_msg={history_msg} hist_evt={history_evt} creds={creds}")
        print(f"  last_seen={last_seen}")

    await c.close()

asyncio.run(main())
