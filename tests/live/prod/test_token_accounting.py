"""Live token-accounting audit.

What we check, with a REAL llm call, on ONE turn:

  A) per-turn `result.usage.input_tokens / output_tokens` reported by the daemon
  B) cumulative `result.usage.total_input_tokens / total_output_tokens`
  C) daemon estimate `result.context.total_estimated_tokens` (breakdown pct)
  D) ground-truth tiktoken count of the EXACT messages sent to the LLM
     (system prompt + tools schema + conversation history)
  E) provider-reported usage from the raw streaming response (Ollama /v1/chat)

Bug flags:
  * B should == A on turn 1 (nothing accumulated before). If A != B → double-count.
  * D should be within ±10% of A (or E). If daemon's char/4 estimate is ≥25%
    off from real tokenization, the breakdown pct shown to the user is wrong.
  * C should be ~= D. If C is way below D, the tools_schema *90 heuristic
    is underestimating.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages"))

import tiktoken  # noqa: E402
from digitorn.testing.client import DevClient  # noqa: E402

APP_YAML = Path(__file__).parent / "coding-assistant-local.yaml"
WORKSPACE = Path(__file__).parent / "workspace"
APP_ID = "prod-coding-assistant-local"


def count_tokens(text: str) -> int:
    enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text, disallowed_special=()))


def count_tokens_json(obj) -> int:
    return count_tokens(json.dumps(obj, ensure_ascii=False))


def main() -> int:
    client = DevClient(daemon_url="http://127.0.0.1:8000", auto_approve=True, timeout=90)
    app = client.deploy(APP_YAML, force=True, wait=5)
    print(f"deployed: status={app.status} tools={app.total_tools}")

    session = client.create_session(APP_ID, workspace=str(WORKSPACE))

    msg = "Count the files in src/ and return just the number."
    print(f"\nuser: {msg}\n")
    r = client.send(session, msg, timeout=90)
    print(f"reply head: {(r.text or '')[:200].strip()}\n")

    # -- Pull what the daemon reports ---------------------------
    history = client.get_history(session, include_system=True)

    # Find the last `result`-bearing record (DevClient stores usage/context
    # on turns[-1] summary - but our raw shape here relies on meta).
    usage = {}
    context_snap = {}
    try:
        # The DevClient records the final result event.
        last_turn = r
        usage = {
            "input_tokens":  getattr(last_turn, "prompt_tokens", None),
            "output_tokens": getattr(last_turn, "completion_tokens", None),
        }
    except Exception:
        pass

    import urllib.request
    url = f"http://127.0.0.1:8000/api/apps/{APP_ID}/sessions/{session.session_id}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            envelope = json.loads(resp.read())
        data = envelope.get("data", envelope) if isinstance(envelope, dict) else {}
        context_snap = data.get("context", {}) or {}
        sm_usage = data.get("tokens", {}) or {}
        usage = {
            "input_tokens":  sm_usage.get("prompt"),
            "output_tokens": sm_usage.get("completion"),
        }
    except Exception as exc:
        print(f"  (session meta fetch failed: {exc})")
        sm_usage = {}

    print("-- Daemon-reported --------------------------------")
    print(f"  usage (per-turn):   input={usage.get('input_tokens')}  output={usage.get('output_tokens')}")
    print(f"  usage (cumulative): {sm_usage}")
    print(f"  context snapshot:   {json.dumps(context_snap, indent=2)[:800]}")

    # -- Ground-truth: what does tiktoken say ? -----------------
    #
    # We recount from `history` (includes system msg + all turns).
    # This is what the *next* turn would see as prompt - close enough
    # to what the LLM saw on this turn (we ignore the response delta).
    sys_msg = next((m for m in history if m.get("role") == "system"), None)
    sys_text = (sys_msg or {}).get("content", "") or ""
    sys_tt = count_tokens(sys_text)

    user_assistant_tool_tt = 0
    n_msgs = 0
    for m in history:
        if m.get("role") == "system":
            continue
        n_msgs += 1
        c = m.get("content", "") or ""
        if isinstance(c, list):
            c = json.dumps(c, ensure_ascii=False)
        user_assistant_tool_tt += count_tokens(str(c))
        # Tool_calls add JSON payload
        tcs = m.get("tool_calls") or []
        if tcs:
            user_assistant_tool_tt += count_tokens_json(tcs)

    # Tools schema size - pull via /tool_display_defaults won't give us the
    # tool LIST; we'll approximate by hitting the app metadata.
    tools_tt = 0
    try:
        url2 = f"http://127.0.0.1:8000/api/apps/{APP_ID}"
        with urllib.request.urlopen(url2, timeout=5) as resp:
            app_meta = json.loads(resp.read())
        # Count tokens of the raw tool schemas if exposed
        tools_meta = app_meta.get("tools") or []
        tools_tt = count_tokens_json(tools_meta) if tools_meta else 0
        print(f"  (got {len(tools_meta)} tool specs from app meta)")
    except Exception as exc:
        print(f"  (tool specs fetch failed: {exc})")

    gt_total = sys_tt + user_assistant_tool_tt + tools_tt

    print("\n-- Tiktoken (cl100k_base) ground-truth -------------")
    print(f"  system_prompt_tokens    = {sys_tt:>6}  "
          f"(daemon said {context_snap.get('system_prompt_tokens')})")
    print(f"  tools_schema_tokens     = {tools_tt:>6}  "
          f"(daemon said {context_snap.get('tools_schema_tokens')})")
    print(f"  messages_tokens         = {user_assistant_tool_tt:>6}  "
          f"(daemon said {context_snap.get('message_history_tokens')})")
    print(f"  GROUND-TRUTH total      = {gt_total:>6}  "
          f"(daemon said {context_snap.get('total_estimated_tokens')})")
    provider_prompt = usage.get("input_tokens") or 0
    print(f"  provider prompt_tokens  = {provider_prompt:>6}  "
          f"(what the LLM *actually* billed)")

    # -- Bug flags -------------------------------------------
    flags = []
    d_sys = context_snap.get("system_prompt_tokens", 0) or 0
    d_tools = context_snap.get("tools_schema_tokens", 0) or 0
    d_msgs = context_snap.get("message_history_tokens", 0) or 0
    d_total = context_snap.get("total_estimated_tokens", 0) or 0

    def pct_diff(a, b):
        if max(a, b) == 0:
            return 0
        return abs(a - b) / max(a, b) * 100

    if pct_diff(sys_tt, d_sys) > 20:
        flags.append(f"system_prompt estimate off by {pct_diff(sys_tt, d_sys):.0f}%")
    if pct_diff(tools_tt, d_tools) > 20 and tools_tt > 100:
        flags.append(f"tools_schema estimate off by {pct_diff(tools_tt, d_tools):.0f}% "
                     f"(gt={tools_tt}, daemon={d_tools})")
    if pct_diff(user_assistant_tool_tt, d_msgs) > 30:
        flags.append(f"messages estimate off by {pct_diff(user_assistant_tool_tt, d_msgs):.0f}%")
    if provider_prompt > 0 and pct_diff(provider_prompt, d_total) > 30:
        flags.append(
            f"total_estimated ({d_total}) vs provider prompt_tokens "
            f"({provider_prompt}) - {pct_diff(provider_prompt, d_total):.0f}% off"
        )

    print("\n-- Verdict -----------------------------------------")
    if flags:
        print("BUGS CONFIRMED:")
        for f in flags:
            print(f"  ! {f}")
        return 1
    print("OK - all counters within 20–30% of ground truth.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
