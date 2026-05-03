"""Show all events around the gap in seq 16-19 + the last events of the session."""
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

    print("=== ALL kind=message rows for this session ===")
    rows = await c.fetch(
        "SELECT seq, role, type, ts FROM history_log "
        "WHERE session_id = $1 AND kind = 'message' "
        "ORDER BY seq ASC",
        SID,
    )
    for r in rows:
        print(f"  seq={r['seq']:>3}  {r['role']:>10}  {r['type']:>22}  {r['ts'].isoformat()}")

    print()
    print("=== Last 30 events around the END of the session ===")
    rows = await c.fetch(
        "SELECT seq, kind, type, ts FROM history_log "
        "WHERE session_id = $1 "
        "ORDER BY id DESC LIMIT 30",
        SID,
    )
    for r in reversed(rows):
        print(f"  seq={r['seq']:>5}  {r['kind']:>8}  {r['type']:>30}  {r['ts'].isoformat()}")

    print()
    print("=== Events in the GAP (between turns) ===")
    # ts of seq 16 final and seq 19 start - print what's between
    rows = await c.fetch(
        "SELECT seq, kind, type, ts FROM history_log "
        "WHERE session_id = $1 AND ts > '2026-05-03 08:21:20' AND ts < '2026-05-03 08:24:10' "
        "ORDER BY ts ASC",
        SID,
    )
    print(f"  ({len(rows)} events in the gap)")
    for r in rows[:50]:
        print(f"  seq={r['seq']:>5}  {r['kind']:>8}  {r['type']:>30}  {r['ts'].isoformat()}")
    if len(rows) > 50:
        print(f"  ... +{len(rows) - 50} more")

    await c.close()

asyncio.run(main())
