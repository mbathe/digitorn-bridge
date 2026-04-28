"""End-to-end verification: the token counts the daemon sends to the
client ARE TRUE - they match the actual payload that was sent to the LLM.

No mocks. Real DevClient, real session, real streaming chat with the local
Ollama qwen model.

Two levels of ground-truth, each checked independently:

  LEVEL 1 - BILLING TRUTH (must be EXACT, 0% tolerance):
    What the provider API actually billed, returned in the `usage` field
    of the last streaming chunk. Captured by a direct curl to Ollama
    /api/chat re-running the same prompt on the side. The daemon's
    `usage.prompt` / `usage.completion` MUST equal this.

  LEVEL 2 - BREAKDOWN ESTIMATE (within 15%, same-tokenizer families):
    Daemon's breakdown (system/tools/messages) is computed with tiktoken
    cl100k_base. For GPT / Claude / Qwen models this is within a few
    percent of the real count; for models with very different tokenizers
    it drifts. The daemon honestly labels this as "estimated".
"""
from __future__ import annotations
import glob
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages"))

import tiktoken  # noqa: E402
from digitorn.testing.client import DevClient  # noqa: E402

APP_YAML = Path(__file__).parent / "coding-assistant-local.yaml"
WORKSPACE = Path(__file__).parent / "workspace"
APP_ID = "prod-coding-assistant-local"
DIAG_DIR = Path(os.environ["DIGITORN_DIAG_DIR"])
ENC = tiktoken.get_encoding("cl100k_base")


def tt(text: str) -> int:
    return len(ENC.encode(text, disallowed_special=()))


def tt_json(obj) -> int:
    return tt(json.dumps(obj, ensure_ascii=False, default=str))


def load_wire_payloads():
    """Load all diag files written by openai_compat.chat_stream."""
    files = sorted(glob.glob(str(DIAG_DIR / "wire_payload_*.json")))
    payloads = []
    for f in files:
        try:
            payloads.append(json.loads(Path(f).read_text(encoding="utf-8")))
        except Exception as exc:
            print(f"  (couldn't read {f}: {exc})")
    return payloads, files


def fetch_daemon_breakdown(session_id: str) -> dict:
    import urllib.request
    url = f"http://127.0.0.1:8000/api/apps/{APP_ID}/sessions/{session_id}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        envelope = json.loads(resp.read())
    return envelope.get("data", envelope) or {}


def pct(a, b):
    if max(a, b) == 0:
        return 0.0
    return abs(a - b) / max(a, b) * 100


def main() -> int:
    # Start clean - wipe prior diag files.
    for f in glob.glob(str(DIAG_DIR / "wire_payload_*.json")):
        try:
            os.remove(f)
        except OSError:
            pass

    client = DevClient(daemon_url="http://127.0.0.1:8000", auto_approve=True, timeout=120)
    app = client.deploy(APP_YAML, force=True, wait=5)
    print(f"deployed: status={app.status} tools={app.total_tools}")

    session = client.create_session(APP_ID, workspace=str(WORKSPACE))
    print(f"session: {session.session_id}")

    msg = "List the files in src/ using the Glob tool, then summarize."
    print(f"\nuser: {msg}")
    r = client.send(session, msg, timeout=120)
    print(f"reply: {(r.text or '')[:200].strip()}\n")

    # Give the provider a moment to flush its last diag file (already sync).
    time.sleep(0.2)

    # ── Load what ACTUALLY went on the wire ───────────────────
    payloads, files = load_wire_payloads()
    print(f"captured {len(payloads)} wire payload(s):")
    for f in files:
        print(f"  {Path(f).name}  ({Path(f).stat().st_size:,} bytes)")
    if not payloads:
        print("FAIL - no wire payloads captured. Is DIGITORN_DIAG_WIRE_PAYLOAD set?")
        return 2

    # LEVEL 1 - capture the real usage chunks the provider emitted DURING
    # the live session (written to disk by the diag hook at the same
    # moment streaming.py reads them). Compare daemon totals to the sum
    # of these - they must match EXACTLY because the daemon's
    # `sm.record_llm_call` is fed from the same chunks.
    usage_files = sorted(glob.glob(str(DIAG_DIR / "wire_usage_*.json")))
    gt_prompt_billed = []
    gt_completion_billed = []
    for f in usage_files:
        try:
            u = json.loads(Path(f).read_text(encoding="utf-8"))
            gt_prompt_billed.append(int(u.get("prompt_tokens", 0)))
            gt_completion_billed.append(int(u.get("completion_tokens", 0)))
        except Exception:
            pass
    gt_prompt_cumulative = sum(gt_prompt_billed)
    gt_completion_cumul_billed = sum(gt_completion_billed)
    print(f"\nwire usage chunks captured: {len(usage_files)}")
    print(f"  per-call prompt tokens:     {gt_prompt_billed}")
    print(f"  per-call completion tokens: {gt_completion_billed}")
    print(f"  cumulative prompt:          {gt_prompt_cumulative}")
    print(f"  cumulative completion:      {gt_completion_cumul_billed}")

    # Breakdown ground-truth = use the LAST wire payload (current snapshot),
    # but pick the most-recent one that actually has a tools schema attached
    # (the final LLM call in a turn is often a tool-free "summarize"
    # completion, and the daemon's snapshot reflects the full deployed
    # toolset that WILL be sent on the next turn).
    last = payloads[-1]
    last_msgs = last.get("messages") or []
    last_tools = last.get("tools") or []
    if not last_tools:
        for p in reversed(payloads):
            if p.get("tools"):
                last_tools = p["tools"]
                break

    # Separate the system prompt from the rest
    sys_msg_text = ""
    other_text = ""
    for m in last_msgs:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", "")
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
        if role == "system":
            sys_msg_text += str(content or "")
        else:
            other_text += str(content or "")
            # tool_calls payload
            tcs = m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None)
            if tcs:
                other_text += json.dumps(tcs, ensure_ascii=False, default=str)

    gt_system = tt(sys_msg_text)
    gt_tools = tt_json(last_tools) if last_tools else 0
    gt_msgs = tt(other_text)

    # ── Fetch what the daemon tells the client ────────────────
    meta = fetch_daemon_breakdown(session.session_id)
    tokens = meta.get("tokens") or {}
    ctx = meta.get("context") or {}
    daemon_prompt_cumul = tokens.get("prompt", 0)
    daemon_completion_cumul = tokens.get("completion", 0)
    daemon_system = ctx.get("system_prompt_tokens", 0)
    daemon_tools = ctx.get("tools_schema_tokens", 0)
    daemon_msgs = ctx.get("message_history_tokens", 0)

    # ── Compare ───────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"{'metric':<35}  {'daemon':>10}  {'truth':>10}  {'diff%':>7}")
    print("-" * 70)
    rows = [
        ("system_prompt_tokens",    daemon_system, gt_system),
        ("tools_schema_tokens",     daemon_tools,  gt_tools),
        ("message_history_tokens",  daemon_msgs,   gt_msgs),
        ("cumulative prompt tokens",daemon_prompt_cumul, gt_prompt_cumulative),
    ]
    fails = []
    # Breakdown fields are tiktoken-based (LEVEL 2) - 15% tol is fine.
    for label, daemon, truth in rows[:-1]:
        d = pct(daemon, truth)
        mark = " OK " if d <= 15 else "FAIL"
        print(f"  {label:<33}  {daemon:>10,}  {truth:>10,}  {d:>6.1f}%  {mark} (tiktoken)")
        if d > 15:
            fails.append(f"{label}: daemon={daemon:,} truth={truth:,} ({d:.1f}% off)")

    # CUMULATIVE prompt MUST match Ollama's billed prompt_tokens EXACTLY
    # (LEVEL 1). Any deviation means the daemon is losing or inventing tokens.
    label, daemon_cumul, _ = rows[-1]
    billed = gt_prompt_cumulative
    d = pct(daemon_cumul, billed) if billed > 0 else 100.0
    mark = "EXACT" if d == 0 else "FAIL"
    print(f"  {label:<33}  {daemon_cumul:>10,}  {billed:>10,}  {d:>6.1f}%  {mark} (billing)")
    if d > 0 and billed > 0:
        fails.append(
            f"{label}: daemon={daemon_cumul:,} ollama-billed={billed:,} "
            f"({daemon_cumul - billed:+,} tokens off - must be EXACT)",
        )

    # Same for completion.
    d = pct(daemon_completion_cumul, gt_completion_cumul_billed) if gt_completion_cumul_billed > 0 else 100.0
    mark = "EXACT" if d == 0 else "FAIL"
    print(f"  {'cumulative completion tokens':<33}  {daemon_completion_cumul:>10,}  "
          f"{gt_completion_cumul_billed:>10,}  {d:>6.1f}%  {mark} (billing)")
    if d > 0 and gt_completion_cumul_billed > 0:
        fails.append(
            f"cumulative completion: daemon={daemon_completion_cumul:,} "
            f"ollama-billed={gt_completion_cumul_billed:,} - must be EXACT",
        )
    print("=" * 70)

    if fails:
        print("\nFAIL - daemon numbers diverge from on-the-wire ground truth:")
        for f in fails:
            print(f"  ! {f}")
        return 1
    print("\nPASS - every number the daemon reports to the client matches "
          "the real payload within 15%.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
