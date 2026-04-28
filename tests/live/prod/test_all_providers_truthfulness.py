"""Multi-provider truthfulness audit.

For each provider config (openai_compat via Ollama, Anthropic via Claude
Code OAuth), run 2 prompts on a real DevClient session and verify the
token counts surfaced to the client match the provider's billed numbers.

Ground-truth = `wire_usage_*.json` files dumped by each provider's diag
hook at the exact moment the usage chunk was emitted.
Daemon-reported = what `GET /api/apps/{app}/sessions/{sid}` returns to
the client after each turn.
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

WORKSPACE = Path(__file__).parent / "workspace"
DIAG_DIR = Path(os.environ["DIGITORN_DIAG_DIR"])


def clear_diag():
    for f in glob.glob(str(DIAG_DIR / "wire_*.json")):
        try:
            os.remove(f)
        except OSError:
            pass


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


def fetch_session_tokens(app_id: str, session_id: str) -> tuple[int, int]:
    url = f"http://127.0.0.1:8000/api/apps/{app_id}/sessions/{session_id}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        envelope = json.loads(resp.read())
    data = envelope.get("data", envelope) or {}
    tokens = data.get("tokens", {}) or {}
    return int(tokens.get("prompt", 0)), int(tokens.get("completion", 0))


def run_suite(app_yaml: Path, app_id: str, prompts: list[str], label: str) -> int:
    print(f"\n{'=' * 80}\nPROVIDER: {label}  ({app_yaml.name})\n{'=' * 80}")
    clear_diag()

    client = DevClient(daemon_url="http://127.0.0.1:8000", auto_approve=True, timeout=180)
    try:
        app = client.deploy(app_yaml, force=True, wait=5)
    except Exception as exc:
        print(f"SKIP - deploy failed: {exc}")
        return -1
    print(f"deployed: status={app.status} tools={app.total_tools}")

    session = client.create_session(app_id, workspace=str(WORKSPACE))
    print(f"session: {session.session_id}")

    prev_p, prev_c = 0, 0
    runs = []
    for i, p in enumerate(prompts, 1):
        print(f"\n--- {label} run {i}: {p[:60]}")
        t0 = time.time()
        try:
            r = client.send(session, p, timeout=180)
        except Exception as exc:
            print(f"  send failed: {exc}")
            return 1
        time.sleep(0.3)
        cum_p, cum_c = fetch_session_tokens(app_id, session.session_id)
        turn_p_client = cum_p - prev_p
        turn_c_client = cum_c - prev_c
        prev_p, prev_c = cum_p, cum_c
        wire_p, wire_c = collect_wire_usage_after(t0)
        runs.append({
            "turn_p_client": turn_p_client,
            "turn_c_client": turn_c_client,
            "wire_p": wire_p,
            "wire_c": wire_c,
            "reply": (r.text or "").strip()[:80],
        })
        print(f"  reply:          {(r.text or '').strip()[:80]}")
        print(f"  client API:     prompt={turn_p_client}  completion={turn_c_client}")
        print(f"  provider wire:  prompt={wire_p}  completion={wire_c}")

    print(f"\n--- {label} verdict ---")
    print(f"{'run':>3}  {'client-api':>18}  {'provider-billed':>18}  {'match':>8}")
    fails = 0
    for i, r in enumerate(runs, 1):
        api = f"{r['turn_p_client']}→{r['turn_c_client']}"
        wire = f"{r['wire_p']}→{r['wire_c']}"
        ok = (r['turn_p_client'] == r['wire_p'] and r['turn_c_client'] == r['wire_c']
              and r['wire_p'] > 0 and r['wire_c'] > 0)
        mark = "EXACT" if ok else "DRIFT"
        print(f"  {i}   {api:>18}   {wire:>18}   {mark:>8}")
        if not ok:
            fails += 1
    return 0 if fails == 0 else 1


def main() -> int:
    base = Path(__file__).parent
    suites = [
        (base / "coding-assistant-local.yaml",  "prod-coding-assistant-local",
         ["List files in src/ in one sentence.", "What language is this project?"],
         "OpenAI-compat / Ollama qwen25-7b"),
        (base / "coding-assistant-claude.yaml", "prod-coding-assistant-claude",
         ["List files in src/ in one sentence.", "What language is this project?"],
         "Anthropic / claude-haiku-4-5"),
    ]

    results = []
    for yaml, app_id, prompts, label in suites:
        rc = run_suite(yaml, app_id, prompts, label)
        results.append((label, rc))

    print(f"\n{'=' * 80}\nSUMMARY")
    all_ok = True
    for label, rc in results:
        status = {-1: "SKIPPED", 0: "PASS", 1: "FAIL"}.get(rc, "?")
        print(f"  [{status:>7}] {label}")
        if rc == 1:
            all_ok = False
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
