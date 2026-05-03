"""Check if seqs are unique per session in history_log for a sample session."""
from __future__ import annotations
import asyncio, json
from pathlib import Path
import asyncpg, yaml

cfg = yaml.safe_load((Path.home() / ".digitorn" / "config.yaml").read_text(encoding="utf-8"))
url = (cfg.get("database") or {}).get("url") or ""
if url.startswith("postgresql+asyncpg://"):
    url = "postgresql://" + url[len("postgresql+asyncpg://"):]

SID = "5ac418d7-10c8-4daf-b32e-b3a41106a040"


async def main() -> None:
    c = await asyncpg.connect(url)
    print("All user_message events of this session, ordered by seq:")
    rows = await c.fetch(
        "SELECT seq, kind, type, role, "
        "       LEFT(content::text, 50) AS msg_content, "
        "       LEFT((payload->>'content')::text, 50) AS pl_content "
        "FROM history_log WHERE session_id = $1 AND type = 'user_message' "
        "ORDER BY seq ASC",
        SID,
    )
    for r in rows:
        print(f"  seq={r['seq']:5} kind={r['kind']:8} role={r['role'] or '-':6}  msg={r['msg_content']}  pl={r['pl_content']}")

    print("\nDuplicate seqs (should be empty):")
    dups = await c.fetch(
        "SELECT seq, COUNT(*) FROM history_log WHERE session_id = $1 GROUP BY seq HAVING COUNT(*) > 1 LIMIT 10",
        SID,
    )
    for r in dups:
        print(f"  seq={r['seq']:5} count={r['count']}")
    await c.close()


asyncio.run(main())
