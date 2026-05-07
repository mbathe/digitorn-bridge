"""Latency benchmark: our gateway vs direct upstream call to Copilot.

Method:
  * 1 cold + 1 warm call on each path to neutralise TLS/JIT warmup.
  * Then 10 calls per path, ALTERNATING (gateway, direct, gateway, ...)
    so a network blip on one side hits the other equally.
  * Same model, same prompt, same max_tokens, same api_key.
  * Sequential (not concurrent) so we measure end-to-end latency, not
    throughput.

Output: per-call latencies, p50/p95, gateway overhead in ms + percent.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

GATEWAY = "http://127.0.0.1:8202"
COPILOT = "https://api.githubcopilot.com"
N = 10

CREDS = json.loads(
    (Path.home() / ".digitorn" / "credentials.json").read_text(encoding="utf-8")
)
USER_JWT = CREDS["access_token"]


def _read_master_key() -> str:
    """The same key the running gateway uses (from spawn_gateway.py)."""
    return "mlkupM2IoI7GnzNGY8g4PvsWpysnciOgMK1Yqm8qJIA="


async def get_copilot_session_token() -> str:
    """Decrypt the Copilot credential's api_key (the short-lived
    session token Copilot mints from the GitHub OAuth token)."""
    import os
    os.environ["DIGITORN_GATEWAY_MASTER_KEY"] = _read_master_key()
    sys.path.insert(0, "c:/Users/ASUS/Documents/digitorn-bridge/packages/gateway/src")
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy import select
    from digitorn_gateway.models_db import GatewayCredential
    from digitorn_gateway.cipher import decrypt_dict

    DB = "postgresql+asyncpg://neondb_owner:***REMOVED***@ep-wild-forest-al4945yw.c-3.eu-central-1.aws.neon.tech/neondb?ssl=require"
    eng = create_async_engine(DB, pool_pre_ping=True)
    async with AsyncSession(eng) as db:
        r = (await db.execute(
            select(GatewayCredential).where(
                GatewayCredential.provider_slug == "github_copilot",
            )
        )).scalars().first()
        secret = decrypt_dict(r.encrypted_value)
    await eng.dispose()
    return secret.get("api_key", "")


def call_gateway() -> tuple[float, int, str]:
    """Hit our gateway with the user JWT. The gateway adds the Copilot
    headers + dispatches via LiteLLM + (optionally) the warm pool."""
    body = {
        "model": "github_copilot/gpt-4o-mini",
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 6,
        "temperature": 1.0,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{GATEWAY}/v1/chat/completions",
        method="POST", data=data,
        headers={"Authorization": f"Bearer {USER_JWT}", "Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    r = urllib.request.urlopen(req, timeout=30)
    raw = r.read()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    body_resp = json.loads(raw)
    text = (body_resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return elapsed_ms, r.status, text


def call_direct(api_key: str) -> tuple[float, int, str]:
    """Hit api.githubcopilot.com directly with the SAME credentials +
    headers our gateway uses internally. NO gateway in the path."""
    body = {
        "model": "gpt-4o-mini",  # same model, no synthesis prefix
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 6,
        "temperature": 1.0,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{COPILOT}/chat/completions",
        method="POST", data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Editor-Version": "vscode/1.96.0",
            "Editor-Plugin-Version": "copilot-chat/0.27.0",
            "Copilot-Integration-Id": "vscode-chat",
            "User-Agent": "GithubCopilot/1.270.0",
            "Content-Type": "application/json",
        },
    )
    t0 = time.perf_counter()
    r = urllib.request.urlopen(req, timeout=30)
    raw = r.read()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    body_resp = json.loads(raw)
    text = (body_resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return elapsed_ms, r.status, text


def stats(times: list[float]) -> dict:
    if not times:
        return {}
    s = sorted(times)
    return {
        "min": s[0],
        "p50": statistics.median(s),
        "mean": statistics.fmean(s),
        "p95": s[int(len(s) * 0.95)] if len(s) > 1 else s[0],
        "max": s[-1],
        "stdev": statistics.stdev(s) if len(s) > 1 else 0.0,
    }


def print_stats(label: str, times: list[float]) -> None:
    st = stats(times)
    print(f"  {label:24s}  n={len(times):2d}  min={st['min']:7.0f}ms  "
          f"p50={st['p50']:7.0f}ms  mean={st['mean']:7.0f}ms  "
          f"p95={st['p95']:7.0f}ms  max={st['max']:7.0f}ms  σ={st['stdev']:5.0f}ms")


def main() -> int:
    print("=" * 70)
    print("  Latency benchmark — our gateway vs direct Copilot upstream")
    print("=" * 70)

    print("\nFetching the live Copilot session token from the gateway's vault...")
    api_key = asyncio.run(get_copilot_session_token())
    if not api_key:
        print("  FAIL: no Copilot session token found")
        return 1
    print(f"  got api_key (len={len(api_key)})")

    # Warmup: 1 cold + 1 warm on each side so neither pays a TLS/JIT
    # handshake during the measured runs.
    print("\nWarming up both paths (2 calls each, not measured)...")
    for _ in range(2):
        try:
            call_gateway()
            call_direct(api_key)
        except Exception as exc:
            print(f"  warmup err: {type(exc).__name__}: {exc}")

    print(f"\nMeasuring: {N} calls per path, ALTERNATING order...")
    print("(this neutralises temporary network variance hitting one side only)")
    print()
    gw_times: list[float] = []
    dir_times: list[float] = []
    print(f"  {'#':<3} {'gateway':<14} {'direct':<14} {'overhead':<14}")
    for i in range(1, N + 1):
        gw_ms, gw_status, gw_text = call_gateway()
        dir_ms, dir_status, dir_text = call_direct(api_key)
        gw_times.append(gw_ms)
        dir_times.append(dir_ms)
        overhead = gw_ms - dir_ms
        marker = " (gateway slower)" if overhead > 0 else " (gateway faster)"
        print(f"  {i:<3} {gw_ms:>7.0f}ms       {dir_ms:>7.0f}ms       "
              f"{overhead:>+7.0f}ms{marker}")

    print()
    print("─" * 70)
    print("  Statistics:")
    print("─" * 70)
    print_stats("Through gateway", gw_times)
    print_stats("Direct to Copilot", dir_times)

    gw_p50 = statistics.median(gw_times)
    dir_p50 = statistics.median(dir_times)
    overhead_p50 = gw_p50 - dir_p50
    pct = (overhead_p50 / dir_p50) * 100

    gw_mean = statistics.fmean(gw_times)
    dir_mean = statistics.fmean(dir_times)
    overhead_mean = gw_mean - dir_mean

    print()
    print(f"  Gateway overhead at p50:  {overhead_p50:+.0f}ms  ({pct:+.1f}%)")
    print(f"  Gateway overhead at mean: {overhead_mean:+.0f}ms")
    print()
    if overhead_p50 < 50:
        verdict = "EXCELLENT — gateway adds <50ms median, near-imperceptible"
    elif overhead_p50 < 150:
        verdict = "GOOD — gateway adds <150ms median, acceptable for production"
    elif overhead_p50 < 300:
        verdict = "OK — gateway adds 150-300ms, room for optimisation"
    else:
        verdict = "POOR — gateway adds >300ms, investigate"
    print(f"  Verdict: {verdict}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
