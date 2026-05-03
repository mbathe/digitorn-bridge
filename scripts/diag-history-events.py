"""Sample events of the latest session for an email - inspect type+seq."""
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
    sess = await c.fetchrow(
        "SELECT session_id, MAX(ts) AS last FROM history_log "
        "WHERE user_id = $1 GROUP BY session_id ORDER BY last DESC LIMIT 1",
        user["id"],
    )
    sid = sess["session_id"]
    print(f"session={sid}\n")

    rows = await c.fetch(
        "SELECT seq, kind, type, role, "
        "       (payload->>'event_id') as ev, "
        "       LEFT(coalesce(content, payload::text), 60) AS preview "
        "FROM history_log WHERE session_id = $1 "
        "ORDER BY seq ASC LIMIT 60",
        sid,
    )
    print(f"first {len(rows)} rows by seq:")
    for r in rows:
        print(f"  seq={r['seq']:5} kind={r['kind']:7} type={r['type']:25} role={r['role'] or '-':10} -> {r['preview'][:60] if r['preview'] else ''}")
    await c.close()


asyncio.run(main())
