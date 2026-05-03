"""Inspect tool_call payload shape."""
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
    rows = await c.fetch(
        "SELECT seq, type, payload FROM history_log "
        "WHERE session_id = $1 AND type IN ('tool_call','tool_start','tool_call_streaming','thinking_started','thinking_delta','stream_done') "
        "ORDER BY seq ASC LIMIT 6",
        SID,
    )
    for r in rows:
        p = r["payload"] or {}
        if isinstance(p, str):
            p = json.loads(p)
        sid_in_p = "session_id" in (p.keys() if isinstance(p, dict) else [])
        keys = list(p.keys())[:8] if isinstance(p, dict) else []
        print(f"seq={r['seq']:5} type={r['type']:22} has_sid={sid_in_p} keys={keys}")
    await c.close()


asyncio.run(main())
