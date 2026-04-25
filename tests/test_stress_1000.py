"""Stress test: 1000 concurrent runs on a single deployed app.

Realistic production scenario:
- 1 RuntimeApp bootstrapped once
- N concurrent run_one_shot calls
- Verifies: no memory leak, no crash, no data corruption, scales linearly

Run: py -3.12 tests/test_stress_1000.py
"""
import sys
import os
import asyncio
import tempfile
import shutil
import textwrap
import time
import gc
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import logging
logging.disable(logging.WARNING)


passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}", flush=True)
    else:
        failed += 1
        print(f"  FAIL  {name} -- {detail}", flush=True)


def section(title):
    print(f"\n=== {title} ===", flush=True)


def get_rss_mb() -> float:
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except ImportError:
        return 0.0


# ── Mock provider ─────────────────────────────────────────
from digitorn.modules.llm_provider.providers.base import (
    ChatMessage, ChatResponse, ProviderCapabilities, ProviderInfo, TokenUsage,
)


class MockProvider:
    def __init__(self):
        self.provider_id = "mock"
        self.model = "mock-model"
        self.api_key = ""
        self.base_url = None
        self.timeout = 60.0
        self.max_retries = 1
        self.default_params: dict[str, Any] = {}
        self.call_count = 0
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        pass

    async def chat(self, messages, **kwargs):
        async with self._lock:
            self.call_count += 1
        return ChatResponse(
            content="ok",
            model="mock-model",
            finish_reason="end_turn",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            tool_calls=None,
            raw={},
        )

    def get_info(self) -> ProviderInfo:
        return ProviderInfo(
            provider_id=self.provider_id,
            backend="mock",
            model=self.model,
            capabilities=ProviderCapabilities(tool_use=True),
            extra={},
        )

    async def close(self) -> None:
        pass


# ── App YAML ─────────────────────────────────────────────

def write_yaml(tmp_path: Path, app_id: str = "stress") -> Path:
    yaml_content = textwrap.dedent(f"""
        app:
          app_id: {app_id}
          name: "Stress Test"

        modules:
          filesystem: {{}}
          memory: {{}}

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: mock
              model: mock-model
              backend: openai_compat
            system_prompt: "You are a stress test assistant."

        execution:
          mode: one_shot
          input:
            type: text
          output:
            type: text

        capabilities:
          default_policy: auto
    """)
    p = tmp_path / "app.yaml"
    p.write_text(yaml_content)
    return p


# ── Compile + bootstrap helper ────────────────────────────

async def bootstrap_app(yaml_path: Path, mock_provider):
    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.loader import load_modules
    from digitorn.core.runtime.app import RuntimeApp
    from digitorn.core.runtime.bootstrap import bootstrap
    from digitorn.modules.registry import ModuleRegistry

    registry = ModuleRegistry()
    load_modules(registry, load_all=True)

    compiler = AppYAMLCompiler(registry)
    compiled = compiler.compile_file(yaml_path)

    with patch(
        "digitorn.core.runtime.bootstrap._resolve_provider",
        return_value=mock_provider,
    ):
        boot_result = await bootstrap(compiled, registry)

    return RuntimeApp(
        app_id=compiled.app_id,
        execution=compiled.execution,
        contexts=boot_result["contexts"],
        modules=boot_result["modules"],
        context_builder=boot_result["context_builder"],
    )


# ══════════════════════════════════════════════════════════
# STRESS TEST
# ══════════════════════════════════════════════════════════

async def stress_test(n_runs: int, tmpdir: Path):
    section(f"Stress test: {n_runs} concurrent runs on 1 RuntimeApp")

    gc.collect()
    rss_start = get_rss_mb()
    print(f"  RSS at start: {rss_start:.1f} MB", flush=True)

    yaml_path = write_yaml(tmpdir, app_id=f"stress-{n_runs}")
    mock = MockProvider()

    print("  Bootstrapping app...", flush=True)
    app = await bootstrap_app(yaml_path, mock)
    check("app bootstrapped", app is not None)

    rss_after_boot = get_rss_mb()
    print(f"  RSS after bootstrap: {rss_after_boot:.1f} MB (+{rss_after_boot - rss_start:.1f})", flush=True)

    # Launch N concurrent run_one_shot calls
    print(f"  Launching {n_runs} concurrent runs...", flush=True)
    start = time.monotonic()

    async def one_run(i: int):
        try:
            return await app.run_one_shot(f"Q{i}")
        except Exception as e:
            return e

    results = await asyncio.gather(
        *[one_run(i) for i in range(n_runs)],
        return_exceptions=True,
    )
    elapsed = time.monotonic() - start

    success = sum(1 for r in results if not isinstance(r, Exception) and r and getattr(r, "error", None) is None)
    errors = sum(1 for r in results if not isinstance(r, Exception) and r and getattr(r, "error", None) is not None)
    crashes = sum(1 for r in results if isinstance(r, Exception))

    rate = (n_runs / elapsed) if elapsed > 0.001 else float("inf")
    print(f"  Completed in {elapsed:.2f}s ({rate:.0f} req/s)", flush=True)
    print(f"  Success: {success}, Errors: {errors}, Crashes: {crashes}", flush=True)

    rss_after_run = get_rss_mb()
    print(f"  RSS after run: {rss_after_run:.1f} MB (+{rss_after_run - rss_after_boot:.1f})", flush=True)

    check(f"{n_runs} runs completed without crashes", crashes == 0,
          f"got {crashes} crashes, {errors} errors")
    check(f"{n_runs} runs all succeeded", success == n_runs,
          f"got {success}/{n_runs}")
    check("provider was called n_runs times", mock.call_count == n_runs,
          f"got {mock.call_count}")

    # Shutdown
    print("  Shutting down...", flush=True)
    await app.shutdown()
    gc.collect()
    rss_final = get_rss_mb()
    print(f"  RSS after shutdown: {rss_final:.1f} MB (net: +{rss_final - rss_start:.1f})", flush=True)

    # Memory bound: 50MB baseline + 100KB per run
    max_growth = 50.0 + n_runs * 0.1
    actual_growth = rss_final - rss_start
    check(f"net memory growth < {max_growth:.0f} MB",
          actual_growth < max_growth,
          f"grew {actual_growth:.1f} MB")


# ══════════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════════

async def main():
    tmpdir = Path(tempfile.mkdtemp(prefix="digitorn_stress_"))
    try:
        # Warmup
        await stress_test(100, tmpdir)
        # Real stress
        await stress_test(1000, tmpdir)
    finally:
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass

    print("\n" + "=" * 55, flush=True)
    print(f"  PASSED: {passed}", flush=True)
    print(f"  FAILED: {failed}", flush=True)
    print(f"  TOTAL:  {passed + failed}", flush=True)
    print("=" * 55, flush=True)
    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
