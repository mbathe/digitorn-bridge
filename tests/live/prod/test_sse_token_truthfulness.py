"""Triple-verification, 3 different prompts in a row.

For each prompt:
  1. Send via DevClient (real HTTP, real daemon, real Ollama).
  2. Read the session detail endpoint (GET /api/apps/{app}/sessions/{sid})
     - this is what the client polls/subscribes to and displays.
  3. Collect the wire-usage chunks written BY THE PROVIDER during this
     turn (dumped to disk mid-stream by the diag hook).
  4. Assert: the cumulative delta on the client-facing API equals the
     sum of wire-usage for this turn, EXACTLY.
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


def collect_wire_usage_after(t0: float) -> tuple[int, int]:
    p_total = 0
    c_total = 0
    for f in sorted(glob.glob(str(DIAG_DIR / "wire_usage_*.json"))):
        try:
            ts = int(Path(f).stem.split("_")[-1]) / 1000.0
            if ts < t0:
                continue
            u = json.loads(Path(f).read_text(encoding="utf-8"))
            p_total += int(u.get("prompt_tokens", 0))
            c_total += int(u.get("completion_tokens", 0))
        except Exception:
            pass
    return p_total, c_total


def fetch_session_tokens(session_id: str) -> tuple[int, int]:
    url = f"http://127.0.0.1:8000/api/apps/{APP_ID}/sessions/{session_id}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        envelope = json.loads(resp.read())
    data = envelope.get("data", envelope) or {}
    tokens = data.get("tokens", {}) or {}
    return int(tokens.get("prompt", 0)), int(tokens.get("completion", 0))


def main() -> int:
    for f in glob.glob(str(DIAG_DIR / "wire_*.json")):
        try:
            os.remove(f)
        except OSError:
            pass

    client = DevClient(daemon_url="http://127.0.0.1:8000", auto_approve=True, timeout=180)
    app = client.deploy(APP_YAML, force=True, wait=5)
    print(f"deployed: status={app.status} tools={app.total_tools}")

    session = client.create_session(APP_ID, workspace=str(WORKSPACE))
    print(f"session: {session.session_id}\n")

    prompts = [
        "List the files in src/ and summarize.",
        "Read src/calculator.py and tell me how many functions it has.",
        "What programming language is this project written in? Answer in one word.",
    ]

    # Baseline - a fresh session has no metrics row yet. Start at 0.
    prev_p, prev_c = 0, 0

    runs = []
    for i, p in enumerate(prompts, 1):
        print(f"--- Run {i} ---")
        print(f"prompt: {p}")
        t0 = time.time()
        r = client.send(session, p, timeout=180)

        # Pull the cumulative tokens the client would see after this turn
        time.sleep(0.2)  # let the API snapshot settle after turn end
        cum_p, cum_c = fetch_session_tokens(session.session_id)
        turn_p_client = cum_p - prev_p
        turn_c_client = cum_c - prev_c
        prev_p, prev_c = cum_p, cum_c

        # Real provider billing for this turn
        wire_p, wire_c = collect_wire_usage_after(t0)

        runs.append({
            "prompt":        p,
            "reply_head":    (r.text or "").strip()[:80],
            "turn_in_api":   turn_p_client,
            "turn_in_wire":  wire_p,
            "turn_out_api":  turn_c_client,
            "turn_out_wire": wire_c,
        })
        print(f"  reply        : {(r.text or '').strip()[:80]}")
        print(f"  client API reads: prompt={turn_p_client}  completion={turn_c_client}")
        print(f"  provider billed: prompt={wire_p}  completion={wire_c}\n")

    print("=" * 84)
    print(f"{'run':>3}  {'client-api':>25}  {'provider-billed':>25}  {'match?':>10}")
    print("-" * 84)
    all_exact = True
    for i, r in enumerate(runs, 1):
        api = f"{r['turn_in_api']}→{r['turn_out_api']}"
        wire = f"{r['turn_in_wire']}→{r['turn_out_wire']}"
        in_ok  = r['turn_in_api']  == r['turn_in_wire']  and r['turn_in_wire']  > 0
        out_ok = r['turn_out_api'] == r['turn_out_wire'] and r['turn_out_wire'] > 0
        mark = "EXACT" if (in_ok and out_ok) else "DRIFT"
        print(f"  {i}   {api:>25}   {wire:>25}   {mark:>10}")
        if not (in_ok and out_ok):
            all_exact = False
    print("=" * 84)

    if all_exact:
        print("\nPASS - across 3 independent prompts, every token count the client "
              "reads from /api/apps/.../sessions/... is byte-equal to the provider's "
              "billing. No drift, no hallucination.")
        return 0
    print("\nFAIL - at least one run diverged.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
