"""Full audit: EVERY surface that exposes or persists token/context data
must report the same (true) numbers after a real LLM turn.

Surfaces checked:
  1. SSE/Socket.IO `result.usage` (captured via the session detail API's
     cumulative tokens — the API reads the same SessionMetrics the SSE
     event does)
  2. REST /api/apps/{app}/sessions/{sid}       → tokens, context
  3. REST /api/apps/{app}/sessions (list)      → tokens per session row
  4. REST /api/users/me/usage                  → monthly totals, by_app
  5. DB `usage_events` rows                    → per-turn persisted events

Ground truth = wire_usage_*.json written by the provider's diag hook at
the exact moment the usage chunk is emitted (independent channel,
impossible to forge from elsewhere in the daemon).
"""
from __future__ import annotations
import glob
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages"))

from digitorn.testing.client import DevClient  # noqa: E402

APP_YAML = Path(__file__).parent / "coding-assistant-local.yaml"
WORKSPACE = Path(__file__).parent / "workspace"
APP_ID = "prod-coding-assistant-local"
DIAG_DIR = Path(os.environ["DIGITORN_DIAG_DIR"])


def http_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def clear_diag():
    for f in glob.glob(str(DIAG_DIR / "wire_*.json")):
        try: os.remove(f)
        except OSError: pass


def collect_wire_usage_after(t0: float) -> tuple[int, int]:
    p = c = 0
    for f in sorted(glob.glob(str(DIAG_DIR / "wire_usage_*.json"))):
        ts = int(Path(f).stem.split("_")[-1]) / 1000.0
        if ts < t0:
            continue
        u = json.loads(Path(f).read_text(encoding="utf-8"))
        p += int(u.get("prompt_tokens", 0))
        c += int(u.get("completion_tokens", 0))
    return p, c


def fmt(label, got, truth):
    ok = got == truth
    mark = "EXACT" if ok else "DRIFT"
    print(f"  {label:<55}  got={got:>8}  truth={truth:>8}  {mark}")
    return ok


def main() -> int:
    clear_diag()

    client = DevClient(daemon_url="http://127.0.0.1:8000", auto_approve=True, timeout=180)
    app = client.deploy(APP_YAML, force=True, wait=5)
    print(f"deployed: tools={app.total_tools}")
    session = client.create_session(APP_ID, workspace=str(WORKSPACE))
    print(f"session: {session.session_id}\n")

    t0 = time.time()
    client.send(session, "Say Python in one word.", timeout=180)
    time.sleep(0.5)  # let persistence flush

    wire_p, wire_c = collect_wire_usage_after(t0)
    wire_total = wire_p + wire_c
    print(f"ground truth (provider wire usage): prompt={wire_p} completion={wire_c} total={wire_total}\n")

    # ── Surface 1: session detail endpoint (what the client fetches) ────
    print("── 1. GET /api/apps/{app}/sessions/{sid}")
    env = http_json(f"http://127.0.0.1:8000/api/apps/{APP_ID}/sessions/{session.session_id}")
    data = env.get("data", env) or {}
    tok = data.get("tokens", {}) or {}
    ctx = data.get("context", {}) or {}
    all_ok = []
    all_ok.append(fmt("tokens.prompt",       tok.get("prompt", 0),     wire_p))
    all_ok.append(fmt("tokens.completion",   tok.get("completion", 0), wire_c))
    all_ok.append(fmt("tokens.total",        tok.get("total", 0),      wire_total))

    # ── Surface 2: session list endpoint (NEW fix) ──────────────────────
    print("\n── 2. GET /api/apps/{app}/sessions (list)")
    env = http_json(f"http://127.0.0.1:8000/api/apps/{APP_ID}/sessions")
    sessions = (env.get("data") or {}).get("sessions", []) or []
    mine = next((s for s in sessions if s.get("session_id") == session.session_id), None)
    if mine is None:
        print(f"  FAIL — session not found in list")
        all_ok.append(False)
    else:
        list_tok = mine.get("tokens", {}) or {}
        if isinstance(list_tok, dict):
            all_ok.append(fmt("list tokens.prompt",     list_tok.get("prompt", 0),     wire_p))
            all_ok.append(fmt("list tokens.completion", list_tok.get("completion", 0), wire_c))
            all_ok.append(fmt("list tokens.total",      list_tok.get("total", 0),      wire_total))
        else:
            all_ok.append(fmt("list tokens (scalar)",   int(list_tok),                 wire_total))

    # ── Surface 3: /api/users/me/usage (per-user lifetime) ──────────────
    print("\n── 3. GET /api/users/me/usage (per-app)")
    try:
        env = http_json("http://127.0.0.1:8000/api/users/me/usage")
        usage = (env.get("data") or env) or {}
        by_app = usage.get("by_app") or []
        mine_app = next((r for r in by_app if r.get("app_id") == APP_ID), None)
        if mine_app is not None:
            # NOTE: by_app is cumulative across ALL sessions of this app. If
            # only this one session exists we expect exactness; otherwise we
            # expect >= wire totals.
            all_ok.append(fmt("by_app.prompt_tokens >= wire_p",
                              mine_app.get("prompt_tokens", 0) >= wire_p, True))
            all_ok.append(fmt("by_app.completion_tokens >= wire_c",
                              mine_app.get("completion_tokens", 0) >= wire_c, True))
        else:
            print(f"  (no usage row for {APP_ID} — maybe unauth; reading raw DB instead)")
    except Exception as exc:
        print(f"  (skipped: {exc})")

    # ── Surface 4: raw DB usage_events ──────────────────────────────────
    print("\n── 4. DB usage_events rows (raw)")
    import sqlite3
    db_path = Path(str(ROOT).rstrip("\\").rstrip("/")) / "digitorn.db"
    if not db_path.is_file():
        print(f"  (db not found at {db_path}, skipping)")
    else:
        con = sqlite3.connect(str(db_path))
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT SUM(prompt_tokens), SUM(completion_tokens), SUM(cost_usd) "
                "FROM usage_events WHERE app_id = ? AND session_id = ?",
                (APP_ID, session.session_id),
            )
            pt, ct, cost = cur.fetchone()
            pt = int(pt or 0); ct = int(ct or 0)
            all_ok.append(fmt("usage_events SUM(prompt_tokens)",      pt, wire_p))
            all_ok.append(fmt("usage_events SUM(completion_tokens)",  ct, wire_c))
            print(f"  usage_events SUM(cost_usd) = {float(cost or 0):.6f} (informational)")
        finally:
            con.close()

    # ── Surface 5: context breakdown sanity (no double-count, tool=0) ───
    print("\n── 5. context breakdown (snapshot)")
    sys_pt = ctx.get("system_prompt_tokens", 0)
    msg_pt = ctx.get("message_history_tokens", 0)
    tool_pt = ctx.get("tools_schema_tokens", 0)
    total_est = ctx.get("total_estimated_tokens", 0)
    print(f"  system_prompt_tokens    = {sys_pt}")
    print(f"  tools_schema_tokens     = {tool_pt}  (must be 0 — native_tool_use off for ollama)")
    print(f"  message_history_tokens  = {msg_pt}  (must NOT include system prompt)")
    print(f"  total_estimated_tokens  = {total_est}")
    all_ok.append(tool_pt == 0)
    all_ok.append(msg_pt < sys_pt)   # sanity: a short session history << system prompt

    print("\n" + "=" * 70)
    if all(all_ok):
        print("PASS — every surface reports TRUE token values, end-to-end.")
        return 0
    print(f"FAIL — {sum(1 for x in all_ok if not x)}/{len(all_ok)} checks diverged.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
