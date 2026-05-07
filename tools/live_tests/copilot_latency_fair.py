"""Fair latency benchmark: both paths use httpx with connection pooling.

The previous run was unfair to the "direct" path because urllib.request
opens a fresh TCP+TLS connection per call, while our gateway has a
warm pool. This run uses httpx.AsyncClient on BOTH sides so the only
difference is the gateway processing.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

GATEWAY = "http://127.0.0.1:8202"
COPILOT = "https://api.githubcopilot.com"
N = 10

CREDS = json.loads(
    (Path.home() / ".digitorn" / "credentials.json").read_text(encoding="utf-8")
)
USER_JWT = CREDS["access_token"]


async def get_copilot_session_token() -> str:
    import os
    os.environ["DIGITORN_GATEWAY_MASTER_KEY"] = "mlkupM2IoI7GnzNGY8g4PvsWpysnciOgMK1Yqm8qJIA="
    sys.path.insert(0, "c:/Users/ASUS/Documents/digitorn-bridge/packages/gateway/src")
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy import select
    from digitorn_gateway.models_db import GatewayCredential
    from digitorn_gateway.cipher import decrypt_dict

    DB = "postgresql+asyncpg://neondb_owner:***REMOVED***@ep-wild-forest-al4945yw.c-3.eu-central-1.aws.neon.tech/neondb?ssl=require"
    eng = create_async_engine(DB, pool_pre_ping=True)
    async with AsyncSession(eng) as db:
        r = (await db.execute(
            select(GatewayCredential).where(GatewayCredential.provider_slug == "github_copilot")
        )).scalars().first()
        secret = decrypt_dict(r.encrypted_value)
    await eng.dispose()
    return secret.get("api_key", "")


async def main():
    print("=" * 70)
    print("  Fair latency benchmark — both paths use httpx with pooling")
    print("=" * 70)
    api_key = await get_copilot_session_token()

    # One pooled httpx.AsyncClient PER PATH, kept open the whole bench.
    async with (
        httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=300),
        ) as gw_client,
        httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=300),
        ) as direct_client,
    ):
        gw_body = {
            "model": "github_copilot/gpt-4o-mini",
            "messages": [{"role": "user", "content": "Reply with: OK"}],
            "max_tokens": 6, "temperature": 1.0,
        }
        direct_body = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "Reply with: OK"}],
            "max_tokens": 6, "temperature": 1.0,
        }

        async def call_gw():
            t0 = time.perf_counter()
            r = await gw_client.post(
                f"{GATEWAY}/v1/chat/completions",
                json=gw_body,
                headers={"Authorization": f"Bearer {USER_JWT}", "Content-Type": "application/json"},
            )
            r.raise_for_status()
            return (time.perf_counter() - t0) * 1000

        async def call_direct():
            t0 = time.perf_counter()
            r = await direct_client.post(
                f"{COPILOT}/chat/completions",
                json=direct_body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Editor-Version": "vscode/1.96.0",
                    "Editor-Plugin-Version": "copilot-chat/0.27.0",
                    "Copilot-Integration-Id": "vscode-chat",
                    "User-Agent": "GithubCopilot/1.270.0",
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
            return (time.perf_counter() - t0) * 1000

        print("\nWarmup (3 calls per path, not measured)...")
        for _ in range(3):
            try:
                await call_gw()
                await call_direct()
            except Exception as exc:
                print(f"  warmup err: {type(exc).__name__}: {exc}")

        print(f"\nMeasuring: {N} alternating calls per path...\n")
        print(f"  {'#':<3} {'gateway':<14} {'direct':<14} {'overhead':<14}")
        gw_times = []
        dir_times = []
        for i in range(1, N + 1):
            gw_ms = await call_gw()
            dir_ms = await call_direct()
            gw_times.append(gw_ms)
            dir_times.append(dir_ms)
            ov = gw_ms - dir_ms
            marker = "(gateway slower)" if ov > 0 else "(gateway faster)"
            print(f"  {i:<3} {gw_ms:>7.0f}ms       {dir_ms:>7.0f}ms       {ov:>+7.0f}ms  {marker}")

    def stats(t):
        s = sorted(t)
        return {
            "min": s[0],
            "p50": statistics.median(s),
            "mean": statistics.fmean(s),
            "p95": s[int(len(s) * 0.95)] if len(s) > 1 else s[0],
            "max": s[-1],
            "stdev": statistics.stdev(s) if len(s) > 1 else 0,
        }

    g = stats(gw_times); d = stats(dir_times)
    print()
    print("─" * 70)
    print(f"  {'Path':<24}  {'min':>7}  {'p50':>7}  {'mean':>7}  {'p95':>7}  {'max':>7}  {'σ':>5}")
    print("─" * 70)
    print(f"  {'Through gateway':<24}  {g['min']:7.0f}  {g['p50']:7.0f}  {g['mean']:7.0f}  {g['p95']:7.0f}  {g['max']:7.0f}  {g['stdev']:5.0f}")
    print(f"  {'Direct to Copilot':<24}  {d['min']:7.0f}  {d['p50']:7.0f}  {d['mean']:7.0f}  {d['p95']:7.0f}  {d['max']:7.0f}  {d['stdev']:5.0f}")
    print()
    p50_overhead = g['p50'] - d['p50']
    p95_overhead = g['p95'] - d['p95']
    mean_overhead = g['mean'] - d['mean']
    print(f"  Overhead at p50:   {p50_overhead:+7.0f}ms  ({p50_overhead/d['p50']*100:+.1f}%)")
    print(f"  Overhead at mean:  {mean_overhead:+7.0f}ms")
    print(f"  Overhead at p95:   {p95_overhead:+7.0f}ms")
    print()
    if abs(p50_overhead) < 30:
        verdict = "EXCELLENT — gateway overhead is negligible at this scale"
    elif p50_overhead < 80:
        verdict = "GOOD — gateway adds <80ms p50, fine for prod"
    elif p50_overhead < 200:
        verdict = "OK — gateway adds 80-200ms p50, room for optimisation"
    else:
        verdict = "POOR — gateway adds >200ms p50"
    print(f"  Verdict: {verdict}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
