"""Deep diag for a specific session: check message vs event rows,
ordering, and persist failures."""
from __future__ import annotations
import asyncio
from pathlib import Path
import asyncpg, yaml

cfg = yaml.safe_load((Path.home() / ".digitorn" / "config.yaml").read_text(encoding="utf-8"))
url = (cfg.get("database") or {}).get("url") or ""
if url.startswith("postgresql+asyncpg://"):
    url = "postgresql://" + url[len("postgresql+asyncpg://"):]

EMAIL = "mbathepaul@gmail.com"
SID = "5ac418d7-10c8-4daf-b32e-b3a41106a040"


async def main() -> None:
    c = await asyncpg.connect(url)

    print(f"=== users with email={EMAIL} ===")
    rows = await c.fetch("SELECT id, email, created_at FROM users WHERE email = $1", EMAIL)
    for r in rows:
        print(f"  {dict(r)}")

    print()
    print(f"=== Counts in history_log for sid={SID} ===")
    rows = await c.fetch(
        "SELECT kind, COUNT(*) AS n FROM history_log WHERE session_id = $1 GROUP BY kind",
        SID,
    )
    for r in rows:
        print(f"  {dict(r)}")

    print()
    print("=== kind=message rows for this session (max 20) ===")
    rows = await c.fetch(
        "SELECT seq, role, type, LEFT(content, 60) AS preview, ts "
        "FROM history_log WHERE session_id = $1 AND kind = 'message' "
        "ORDER BY seq ASC LIMIT 20",
        SID,
    )
    for r in rows:
        print(f"  {dict(r)}")
    if not rows:
        print("  (none) - persist_turn_bg never wrote kind=message rows!")

    print()
    print("=== user_sessions row for this session ===")
    rows = await c.fetch(
        "SELECT id, app_id, session_id, user_id, created_at FROM user_sessions "
        "WHERE session_id = $1",
        SID,
    )
    for r in rows:
        print(f"  {dict(r)}")
    if not rows:
        print("  (none) - session row missing in DB. _persist_turn skips when ensure_session fails.")

    print()
    print("=== session_checkpoints rows for this session ===")
    rows = await c.fetch(
        "SELECT turn, status, prompt_tokens, completion_tokens, last_error, last_active_at "
        "FROM session_checkpoints WHERE session_id = $1 "
        "ORDER BY turn DESC LIMIT 5",
        SID,
    )
    for r in rows:
        print(f"  {dict(r)}")
    if not rows:
        print("  (none) - no checkpoints written. _persist_turn never reached step 3.")

    await c.close()

asyncio.run(main())
