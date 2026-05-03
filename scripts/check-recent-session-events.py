"""List all events of the most recent session for an email."""
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
        print("user not found")
        return
    uid = user["id"]
    print(f"user_id={uid}")
    # find most recent session with activity
    sess = await c.fetchrow(
        "SELECT session_id, app_id, MAX(ts) AS last "
        "FROM history_log WHERE user_id = $1 "
        "GROUP BY session_id, app_id ORDER BY last DESC LIMIT 3",
        uid,
    )
    if sess is None:
        print("no sessions")
        return
    sid = sess["session_id"]
    app = sess["app_id"]
    print(f"latest session={sid} app={app} last_event={sess['last']}")
    rows = await c.fetch(
        "SELECT seq, type, ts FROM history_log "
        "WHERE session_id = $1 "
        "ORDER BY seq DESC LIMIT 40",
        sid,
    )
    print("\nLast 40 events of latest session (newest first):")
    for r in rows:
        print(f"  seq={r['seq']:5} ts={r['ts']} type={r['type']}")
    await c.close()


asyncio.run(main())
