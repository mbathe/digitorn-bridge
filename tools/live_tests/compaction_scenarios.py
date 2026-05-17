"""Live diagnosis: does the auto-compact hook actually fire?

Builds up a long conversation on a small-context fallback (qwen
ollama) until the message history crosses 75% of effective_max,
then snapshots:

  - session_metrics.context.pressure  (UI-facing pressure)
  - session_metrics.context.compactions  (counter)
  - the TurnState the hook would see (estimated_tokens, effective_max)

If pressure crosses 0.75 but compactions stays at 0, we have a
reproducible bug. We dump enough state to attribute it to the right
layer (threshold mismatch, condition no-op, action no-op, cooldown,
keep_recent short-circuit).
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, "C:/Users/ASUS/Documents/digitorn-bridge/packages")
from digitorn.testing import DevClient
from digitorn.testing.models import SessionHandle


_DAEMON = "http://127.0.0.1:8000"
_APP_ID = "sp-probe-app"  # fresh app, no main+fallback mess


def _spam_pad(idx: int) -> str:
    """Build a multi-KB user message so we cross the threshold in
    fewer turns. Realistic content (mostly Latin so char/4 ~ tokens)
    instead of zero-information padding."""
    chunk = (
        "Le contexte d'un agent LLM est compose de plusieurs blocs: "
        "system prompt, schemas des outils, historique des messages, "
        "et les tokens reserves pour la generation. Chaque turn agrandit "
        "l'historique. Quand l'historique depasse une fraction du "
        "context_window (typiquement 75%), la compaction doit fire pour "
        "couper les vieux messages, garder les recents, et resumer le "
        "milieu. Sans compaction, le provider rejette la requete et "
        "l'agent perd le fil."
    )
    # 200 chunks ~ 30KB/msg ~ 7-8K tokens. Crosses 75% of 124K in
    # ~12 turns, which keeps the run under 2 minutes.
    return f"Message {idx}: {chunk * 200}"


def run() -> int:
    creds = json.loads(
        (Path.home() / ".digitorn" / "credentials.json").read_text()
    )
    client = DevClient.with_token(creds["access_token"], daemon_url=_DAEMON, timeout=180)

    sid = f"compact-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=_APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )

    print(f"=== COMPACTION DIAGNOSIS ===")
    print(f"app_id={_APP_ID}  session_id={sid}")
    print(f"each turn appends ~3KB ; track pressure + compactions per turn\n")

    pressure_log: list[tuple[int, float, int, int, int]] = []
    for i in range(1, 25):
        msg = _spam_pad(i)
        post = client.post_message_raw(session, msg)
        if post.get("status_code") != 200:
            print(f"turn {i}: POST {post.get('status_code')}  body={str(post.get('body'))[:200]}")
            break

        # Wait for the turn to land (simple delay; we don't need
        # streaming here, just the post-turn snapshot)
        time.sleep(8)

        try:
            br = client.get_context_breakdown(session)
        except Exception as exc:
            br = {"error": str(exc)}

        data = br.get("data", br) if isinstance(br, dict) else {}
        ctx = data.get("context") or data
        pressure = ctx.get("pressure")
        tokens = ctx.get("total_estimated_tokens", 0)
        effective = ctx.get("effective_max", 0)
        compactions = ctx.get("compactions", 0)
        pressure_log.append((i, pressure or 0.0, tokens, effective, compactions))
        print(
            f"turn {i:2d}: pressure={pressure!r:6} tokens={tokens:>7} "
            f"effective_max={effective:>7} compactions={compactions}"
        )

        if compactions > 0:
            print(f"\n*** COMPACTION FIRED at turn {i} ***")
            return 0

    print("\n--- summary ---")
    crossed_75 = any(p[1] and p[1] >= 0.75 for p in pressure_log)
    final_compactions = pressure_log[-1][4] if pressure_log else 0
    print(f"crossed_pressure_0.75 = {crossed_75}")
    print(f"final_compactions     = {final_compactions}")

    if crossed_75 and final_compactions == 0:
        print("\n*** BUG REPRODUCED: pressure exceeded 75% but no compaction fired ***")
        return 1

    if not crossed_75:
        print("\n(inconclusive: didn't cross threshold in 15 turns; bump payload or turn count)")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(run())
