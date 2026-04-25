"""24-hour soak test — daemon stability under realistic continuous load.

Simulates realistic activity for an extended period and tracks:
- Memory growth (RSS)
- File descriptor count
- Latency per operation
- Cleanup effectiveness
- Error rate over time

Designed to run for hours/days. Configurable duration via DURATION env var.
Default: 5 minutes for quick verification. Set DURATION=86400 for 24h.

Run: py -3.12 tests/test_soak_24h.py
     DURATION=300 py -3.12 tests/test_soak_24h.py    # 5 min
     DURATION=86400 py -3.12 tests/test_soak_24h.py  # 24h

Metrics saved to: docs/soak_metrics.csv
"""
import sys
import os
import asyncio
import tempfile
import shutil
import textwrap
import time
import gc
import csv
import random
from pathlib import Path
from typing import Any
from datetime import datetime
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import logging
logging.disable(logging.WARNING)


# ── Configuration ─────────────────────────────────────────
DURATION_SECONDS = int(os.environ.get("DURATION", "300"))  # 5 min default
SAMPLE_INTERVAL = float(os.environ.get("SAMPLE_INTERVAL", "10"))  # every 10s
OPS_PER_BATCH = int(os.environ.get("OPS_PER_BATCH", "20"))  # ops per cycle


# ── Memory + FD tracking ──────────────────────────────────

def get_rss_mb() -> float:
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except ImportError:
        return 0.0


def get_fd_count() -> int:
    try:
        import psutil
        proc = psutil.Process()
        try:
            return proc.num_fds()
        except AttributeError:
            return len(proc.open_files()) + len(proc.connections())
    except Exception:
        return 0


def get_thread_count() -> int:
    try:
        import threading
        return threading.active_count()
    except Exception:
        return 0


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


# ── App YAML ─────────────────────────────────────────────

def write_yaml(tmp_path: Path) -> Path:
    yaml_content = textwrap.dedent("""
        app:
          app_id: soak-test
          name: "Soak Test"

        modules:
          filesystem: {}
          memory: {}
          shell: {}

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: mock
              model: mock-model
              backend: openai_compat
            system_prompt: "Soak test."

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


# ── Bootstrap ─────────────────────────────────────────────

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


# ── Workload generator ────────────────────────────────────

class WorkloadGenerator:
    """Generates realistic mixed operations on the deployed app."""

    def __init__(self, app, tmpdir: Path):
        self.app = app
        self.tmpdir = tmpdir
        self.fs = app.modules.get("filesystem")
        self.mem = app.modules.get("memory")
        self.shell = app.modules.get("shell")
        self.counter = 0

        # Pre-create some workspace files
        self.work_dir = tmpdir / "workspace"
        self.work_dir.mkdir(exist_ok=True)
        if self.fs:
            self.fs._workspace_root = str(self.work_dir)

        # Seed files
        for i in range(5):
            (self.work_dir / f"seed_{i}.py").write_text(f"x = {i}\ny = {i*2}\n")

    async def random_op(self) -> tuple[str, bool, float]:
        """Run a random operation. Returns (op_name, success, duration_ms)."""
        op = random.choice([
            "fs_read",
            "fs_write",
            "fs_edit",
            "fs_grep",
            "fs_glob",
            "mem_remember",
            "mem_task_create",
            "agent_run_one_shot",
        ])

        start = time.monotonic()
        success = False
        try:
            if op == "fs_read" and self.fs:
                from digitorn.modules.filesystem.params import ReadParams
                idx = self.counter % 5
                fp = str(self.work_dir / f"seed_{idx}.py")
                result = await self.fs.read(ReadParams(file_path=fp))
                success = result.success
            elif op == "fs_write" and self.fs:
                from digitorn.modules.filesystem.params import WriteParams
                fp = str(self.work_dir / f"tmp_{self.counter % 50}.py")
                result = await self.fs.write(WriteParams(file_path=fp, content=f"# {self.counter}\n"))
                success = result.success
            elif op == "fs_edit" and self.fs:
                from digitorn.modules.filesystem.params import EditParams, ReadParams
                idx = self.counter % 5
                fp = str(self.work_dir / f"seed_{idx}.py")
                # Read first to register in _read_files
                await self.fs.read(ReadParams(file_path=fp))
                result = await self.fs.edit(EditParams(
                    file_path=fp,
                    old_string=f"x = {idx}",
                    new_string=f"x = {idx}",  # no-op edit
                ))
                success = result.success
            elif op == "fs_grep" and self.fs:
                from digitorn.modules.filesystem.params import GrepParams
                result = await self.fs.grep(GrepParams(pattern="x = ", path=str(self.work_dir)))
                success = result.success
            elif op == "fs_glob" and self.fs:
                from digitorn.modules.filesystem.params import GlobParams
                result = await self.fs.glob(GlobParams(pattern="*.py", path=str(self.work_dir)))
                success = result.success
            elif op == "mem_remember" and self.mem:
                from digitorn.modules.memory.module import RememberParams
                result = await self.mem.remember(RememberParams(content=f"fact {self.counter}"))
                success = result.success
            elif op == "mem_task_create" and self.mem:
                from digitorn.modules.memory.module import TaskCreateParams
                result = await self.mem.task_create(TaskCreateParams(subject=f"Task {self.counter}"))
                success = result.success
            elif op == "agent_run_one_shot":
                result = await self.app.run_one_shot(f"Q{self.counter}")
                success = result is not None and getattr(result, "error", None) is None
        except Exception:
            success = False

        duration_ms = (time.monotonic() - start) * 1000
        self.counter += 1
        return (op, success, duration_ms)

    async def run_batch(self, n_ops: int) -> dict:
        """Run a batch of n_ops operations in parallel."""
        results = await asyncio.gather(
            *[self.random_op() for _ in range(n_ops)],
            return_exceptions=True,
        )
        ops_count = {}
        success_count = 0
        total_duration_ms = 0.0
        max_duration_ms = 0.0
        crashes = 0
        for r in results:
            if isinstance(r, Exception):
                crashes += 1
                continue
            op_name, success, dur = r
            ops_count[op_name] = ops_count.get(op_name, 0) + 1
            if success:
                success_count += 1
            total_duration_ms += dur
            max_duration_ms = max(max_duration_ms, dur)
        return {
            "total": len(results),
            "success": success_count,
            "crashes": crashes,
            "avg_ms": total_duration_ms / max(1, len(results)),
            "max_ms": max_duration_ms,
            "ops_by_type": ops_count,
        }


# ── Main soak loop ────────────────────────────────────────

async def main():
    tmpdir = Path(tempfile.mkdtemp(prefix="digitorn_soak_"))
    metrics_path = Path("docs/soak_metrics.csv")
    metrics_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Soak test — duration: {DURATION_SECONDS}s, sample interval: {SAMPLE_INTERVAL}s")
    print(f"Metrics will be saved to: {metrics_path}")
    print()

    # Bootstrap
    print("Bootstrapping app...", flush=True)
    yaml_path = write_yaml(tmpdir)
    mock = MockProvider()
    app = await bootstrap_app(yaml_path, mock)
    print("App bootstrapped.", flush=True)

    workload = WorkloadGenerator(app, tmpdir)

    # Initial baseline
    gc.collect()
    rss_baseline = get_rss_mb()
    fd_baseline = get_fd_count()
    thread_baseline = get_thread_count()

    print(f"Baseline: RSS={rss_baseline:.1f}MB, FDs={fd_baseline}, threads={thread_baseline}")
    print()

    # Open CSV for streaming metrics
    csv_file = open(metrics_path, "w", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    writer.writerow([
        "elapsed_s", "timestamp", "total_ops", "success_rate",
        "rss_mb", "rss_growth", "fd_count", "fd_growth", "threads",
        "avg_latency_ms", "max_latency_ms", "crashes_total",
    ])
    csv_file.flush()

    start_time = time.monotonic()
    next_sample = start_time + SAMPLE_INTERVAL
    total_ops = 0
    total_success = 0
    total_crashes = 0

    print(f"{'Time':>6} | {'Ops':>7} | {'Success%':>8} | {'RSS MB':>8} | {'+MB':>6} | {'FDs':>5} | {'Avg ms':>7} | {'Max ms':>7} | {'Crash':>5}")
    print("-" * 88)

    try:
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= DURATION_SECONDS:
                break

            # Run a batch of operations
            batch = await workload.run_batch(OPS_PER_BATCH)
            total_ops += batch["total"]
            total_success += batch["success"]
            total_crashes += batch["crashes"]

            # Sample metrics every SAMPLE_INTERVAL seconds
            now = time.monotonic()
            if now >= next_sample:
                gc.collect()
                rss = get_rss_mb()
                fds = get_fd_count()
                threads = get_thread_count()
                success_rate = (100.0 * total_success / max(1, total_ops))

                print(
                    f"{elapsed:6.0f} | {total_ops:7d} | {success_rate:7.1f}% | "
                    f"{rss:7.1f} | {rss - rss_baseline:+5.1f} | {fds:5d} | "
                    f"{batch['avg_ms']:6.1f} | {batch['max_ms']:6.1f} | {total_crashes:5d}",
                    flush=True,
                )

                writer.writerow([
                    f"{elapsed:.0f}",
                    datetime.now().isoformat(timespec="seconds"),
                    total_ops,
                    f"{success_rate:.2f}",
                    f"{rss:.1f}",
                    f"{rss - rss_baseline:+.1f}",
                    fds,
                    fds - fd_baseline,
                    threads,
                    f"{batch['avg_ms']:.1f}",
                    f"{batch['max_ms']:.1f}",
                    total_crashes,
                ])
                csv_file.flush()

                next_sample = now + SAMPLE_INTERVAL

            # Tiny pause to not max out the CPU
            await asyncio.sleep(0.01)

    except KeyboardInterrupt:
        print("\nInterrupted by user", flush=True)
    finally:
        # Final shutdown
        print("\nShutting down...", flush=True)
        try:
            await app.shutdown()
        except Exception as e:
            print(f"Shutdown error: {e}", flush=True)

        gc.collect()
        rss_final = get_rss_mb()
        fd_final = get_fd_count()
        thread_final = get_thread_count()
        elapsed_final = time.monotonic() - start_time

        csv_file.close()
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass

        # Final report
        print("\n" + "=" * 70)
        print("SOAK TEST FINAL REPORT")
        print("=" * 70)
        print(f"Duration:              {elapsed_final:.0f}s ({elapsed_final/60:.1f} min)")
        print(f"Total operations:      {total_ops}")
        print(f"Throughput:            {total_ops / elapsed_final:.0f} ops/s")
        print(f"Success rate:          {100.0 * total_success / max(1, total_ops):.2f}%")
        print(f"Total crashes:         {total_crashes}")
        print(f"Provider call_count:   {mock.call_count}")
        print()
        print(f"RSS: {rss_baseline:.1f}MB → {rss_final:.1f}MB  ({rss_final - rss_baseline:+.1f}MB)")
        print(f"FDs: {fd_baseline} → {fd_final}  ({fd_final - fd_baseline:+d})")
        print(f"Threads: {thread_baseline} → {thread_final}  ({thread_final - thread_baseline:+d})")
        print()
        print(f"Metrics saved to: {metrics_path}")

        # Verdict
        rss_growth = rss_final - rss_baseline
        fd_growth = fd_final - fd_baseline
        crash_rate = (total_crashes / max(1, total_ops)) * 100

        print()
        print("=" * 70)
        print("VERDICT")
        print("=" * 70)

        ok = True
        # Memory growth bound: 2MB per 1000 ops (very generous)
        max_growth = max(50.0, total_ops / 1000.0 * 2.0)
        if rss_growth > max_growth:
            print(f"  FAIL: RSS grew {rss_growth:.1f}MB > {max_growth:.1f}MB threshold (memory leak?)")
            ok = False
        else:
            print(f"  OK: RSS growth {rss_growth:+.1f}MB within {max_growth:.0f}MB threshold")

        if fd_growth > 50:
            print(f"  FAIL: FD count grew by {fd_growth} (file descriptor leak?)")
            ok = False
        else:
            print(f"  OK: FD growth {fd_growth:+d} within 50 threshold")

        if crash_rate > 0.1:
            print(f"  FAIL: Crash rate {crash_rate:.2f}% > 0.1% threshold")
            ok = False
        else:
            print(f"  OK: Crash rate {crash_rate:.4f}% within 0.1% threshold")

        if ok:
            print("\n  STABILITY: ✅ PASS")
        else:
            print("\n  STABILITY: ❌ FAIL — see warnings above")

        return ok


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
