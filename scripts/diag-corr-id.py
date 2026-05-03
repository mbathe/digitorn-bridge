"""Check correlation_id presence on token / tool_call events."""
from __future__ import annotations
import asyncio
from pathlib import Path
import asyncpg, yaml

cfg = yaml.safe_load((Path.home() / ".digitorn" / "config.yaml").read_text(encoding="utf-8"))
url = (cfg.get("database") or {}).get("url") or ""
if url.startswith("postgresql+asyncpg://"):
    url = "postgresql://" + url[len("postgresql+asyncpg://"):]

SID = "5ac418d7-10c8-4daf-b32e-b3a41106a040"


async def main() -> None:
    c = await asyncpg.connect(url)
    rows = await c.fetch(
        "SELECT seq, type, "
        "       coalesce(correlation_id, '') AS row_corr, "
        "       coalesce(payload->>'correlation_id', '') AS pl_corr, "
        "       coalesce(payload->>'op_id', '') AS pl_op "
        "FROM history_log WHERE session_id = $1 AND kind = 'event' "
        "AND type IN ('user_message','message_started','token','tool_call','result','message_done') "
        "ORDER BY seq ASC LIMIT 30",
        SID,
    )
    print("seq  | type             | row.correlation_id | pl.correlation_id | pl.op_id")
    print("-" * 100)
    for r in rows:
        print(f"{r['seq']:5} | {r['type']:18} | {r['row_corr']:18} | {r['pl_corr']:18} | {r['pl_op']}")
    await c.close()

asyncio.run(main())
