"""Performance profiling: identify hot paths in the daemon under realistic load.

Profiles a representative workload (bootstrap + 500 concurrent runs)
and reports the top 25 hot paths by cumulative time and by self time.

Run: py -3.12 tests/test_profile_perf.py
"""
import sys
import os
import asyncio
import tempfile
import shutil
import textwrap
import cProfile
import pstats
import io
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import logging
logging.disable(logging.WARNING)


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

    async def initialize(self) -> None:
        pass

    async def chat(self, messages, **kwargs):
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


def write_yaml(tmp_path: Path) -> Path:
    yaml_content = textwrap.dedent("""
        app:
          app_id: profile-test
          name: "Profile Test"

        modules:
          filesystem: {}
          memory: {}

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: mock
              model: mock-model
              backend: openai_compat
            system_prompt: "Test."

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


async def profiled_workload(tmpdir: Path, n_runs: int):
    """The workload to profile."""
    yaml_path = write_yaml(tmpdir)
    mock = MockProvider()

    app = await bootstrap_app(yaml_path, mock)

    # Run N concurrent runs
    results = await asyncio.gather(
        *[app.run_one_shot(f"Q{i}") for i in range(n_runs)],
        return_exceptions=True,
    )

    success = sum(
        1 for r in results
        if not isinstance(r, Exception) and r and getattr(r, "error", None) is None
    )

    await app.shutdown()
    return success


def main():
    tmpdir = Path(tempfile.mkdtemp(prefix="digitorn_profile_"))
    n_runs = 500
    print(f"Profiling: bootstrap + {n_runs} concurrent runs", flush=True)
    print("=" * 70, flush=True)

    profiler = cProfile.Profile()

    start = time.monotonic()
    profiler.enable()
    try:
        success = asyncio.run(profiled_workload(tmpdir, n_runs))
    finally:
        profiler.disable()
        elapsed = time.monotonic() - start
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass

    print(f"\nWorkload completed: {success}/{n_runs} success in {elapsed:.2f}s")
    print(f"Throughput: {n_runs / elapsed:.0f} req/s")
    print()

    # ── Top 25 by cumulative time ─────────────────────────
    print("=" * 70)
    print("TOP 25 HOT PATHS - by cumulative time (where time is spent)")
    print("=" * 70)
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream).sort_stats("cumulative")
    # Filter to digitorn code only (skip stdlib + third party)
    stats.print_stats("digitorn", 25)
    print(stream.getvalue())

    # ── Top 25 by self time ───────────────────────────────
    print("=" * 70)
    print("TOP 25 HOT PATHS - by self time (functions doing actual work)")
    print("=" * 70)
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream).sort_stats("tottime")
    stats.print_stats("digitorn", 25)
    print(stream.getvalue())

    # ── Top 15 callers of expensive functions ────────────
    print("=" * 70)
    print("TOP 15 OVERALL - what dominates the workload")
    print("=" * 70)
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream).sort_stats("cumulative")
    stats.print_stats(15)
    print(stream.getvalue())

    # ── Save full profile for offline analysis ───────────
    output_path = Path("docs/perf_profile.txt")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"Workload: bootstrap + {n_runs} concurrent runs\n")
        f.write(f"Total: {elapsed:.2f}s ({n_runs / elapsed:.0f} req/s)\n\n")
        f.write("=" * 70 + "\n")
        f.write("TOP 50 BY CUMULATIVE TIME (digitorn only)\n")
        f.write("=" * 70 + "\n")
        s = io.StringIO()
        pstats.Stats(profiler, stream=s).sort_stats("cumulative").print_stats("digitorn", 50)
        f.write(s.getvalue())
        f.write("\n" + "=" * 70 + "\n")
        f.write("TOP 50 BY SELF TIME (digitorn only)\n")
        f.write("=" * 70 + "\n")
        s = io.StringIO()
        pstats.Stats(profiler, stream=s).sort_stats("tottime").print_stats("digitorn", 50)
        f.write(s.getvalue())
    print(f"\nFull profile saved to: {output_path}")


if __name__ == "__main__":
    main()
