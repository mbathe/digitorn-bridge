"""Inspect the shape of a few key events in a session, focus on seq+payload."""
from __future__ import annotations
import asyncio, json
from pathlib import Path
import asyncpg, yaml

cfg = yaml.safe_load((Path.home() / ".digitorn" / "config.yaml").read_text(encoding="utf-8"))
url = (cfg.get("database") or {}).get("url") or ""
if url.startswith("postgresql+asyncpg://"):
    url = "postgresql://" + url[len("postgresql+asyncpg://"):]

SID = "5ac418d7-10c8-4daf-b32e-b3a41106a040"  # smaller session


async def main() -> None:
    c = await asyncpg.connect(url)
    rows = await c.fetch(
        "SELECT seq, kind, type, role, payload "
        "FROM history_log WHERE session_id = $1 "
        "AND type IN ('user_message','tool_call','tool_start','message_started','message_done','result','token','error') "
        "ORDER BY seq ASC LIMIT 30",
        SID,
    )
    for r in rows:
        p = r["payload"] or {}
        if isinstance(p, str):
            p = json.loads(p)
        sid_in_p = p.get("session_id", "(missing)") if isinstance(p, dict) else "(not dict)"
        keys = list(p.keys())[:6] if isinstance(p, dict) else []
        print(f"seq={r['seq']:5} type={r['type']:18} sid_in_payload={'YES' if sid_in_p != '(missing)' else 'NO ':3} keys={keys}")
    await c.close()


asyncio.run(main())
