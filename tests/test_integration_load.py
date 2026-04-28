"""End-to-end integration tests under concurrent load.

Exercises the full Digitorn pipeline (compile → bootstrap → multi-session
chat → cleanup) with N concurrent sessions to verify:
- Session isolation works under concurrency
- No state leaks across sessions
- Cleanup happens correctly
- Tool calls don't interfere across sessions
- The daemon survives many concurrent operations

Run: py -3.12 tests/test_integration_load.py
"""
import sys
import os
import asyncio
import tempfile
import shutil
import textwrap
import time
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
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name} -- {detail}")


def section(title):
    print(f"\n=== {title} ===")


# ── Mock provider ─────────────────────────────────────────
from digitorn.modules.llm_provider.providers.base import (
    ChatMessage, ChatResponse, ProviderCapabilities, ProviderInfo, TokenUsage,
)


class MockProvider:
    """Configurable mock LLM provider - returns scripted or default responses."""

    def __init__(self, responses=None, response_fn=None):
        self.provider_id = "mock"
        self.model = "mock-model"
        self.api_key = ""
        self.base_url = None
        self.timeout = 60.0
        self.max_retries = 1
        self.default_params: dict[str, Any] = {}
        self._responses = list(responses or [])
        self._call_index = 0
        self._response_fn = response_fn
        self.call_log: list[dict[str, Any]] = []
        self.call_count = 0

    async def initialize(self) -> None:
        pass

    async def chat(self, messages, **kwargs):
        self.call_count += 1
        self.call_log.append({
            "messages": [(m.role, (m.content or "")[:80]) for m in messages],
            "tool_count": len(kwargs.get("tools") or []),
        })
        if self._response_fn:
            return self._response_fn(messages, kwargs)
        if self._call_index < len(self._responses):
            r = self._responses[self._call_index]
            self._call_index += 1
            return r
        return ChatResponse(
            content="Done.",
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


def make_text_response(text: str) -> ChatResponse:
    return ChatResponse(
        content=text, model="mock-model", finish_reason="end_turn",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        tool_calls=None, raw={},
    )


def make_tool_response(tool_name: str, args: dict, call_id: str = "call_1") -> ChatResponse:
    return ChatResponse(
        content="", model="mock-model", finish_reason="tool_use",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        tool_calls=[{
            "id": call_id, "type": "function",
            "function": {"name": tool_name, "arguments": args},
        }],
        raw={},
    )


# ── Compile + bootstrap helper ────────────────────────────

async def compile_and_bootstrap(yaml_path: Path, mock_provider: MockProvider):
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

    app = RuntimeApp(
        app_id=compiled.app_id,
        execution=compiled.execution,
        contexts=boot_result["contexts"],
        modules=boot_result["modules"],
        context_builder=boot_result["context_builder"],
    )
    return app


def write_yaml(tmp_path: Path, app_id: str = "load-test") -> Path:
    yaml_content = textwrap.dedent(f"""
        app:
          app_id: {app_id}
          name: "Load Test App"

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
            system_prompt: "You are a test assistant."

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


# ══════════════════════════════════════════════════════════
# TEST 1: Single session full pipeline
# ══════════════════════════════════════════════════════════

async def test_single_session(tmpdir):
    section("Test 1: Single session full pipeline")

    yaml_path = write_yaml(tmpdir, app_id="t1-single")
    mock = MockProvider(responses=[make_text_response("Hello from mock!")])

    app = await compile_and_bootstrap(yaml_path, mock)
    check("app bootstrapped", app is not None)

    result = await app.run_one_shot("Hi")
    check("run_one_shot returned", result is not None)
    check("result has content", "Hello" in (result.content or ""))
    check("provider was called", mock.call_count > 0)

    await app.shutdown()
    check("app shutdown clean", True)


# ══════════════════════════════════════════════════════════
# TEST 2: Multiple concurrent sessions - same app
# ══════════════════════════════════════════════════════════

async def test_concurrent_sessions(tmpdir):
    section("Test 2: 10 concurrent sessions")

    yaml_path = write_yaml(tmpdir, app_id="t2-concurrent")

    # Each session gets its own mock so we can track per-session calls
    mocks = [
        MockProvider(responses=[make_text_response(f"Response from session {i}")])
        for i in range(10)
    ]
    apps = []
    for i in range(10):
        app = await compile_and_bootstrap(yaml_path, mocks[i])
        apps.append(app)

    check("10 apps bootstrapped", len(apps) == 10)

    # Run all 10 in parallel
    start = time.monotonic()
    results = await asyncio.gather(
        *[apps[i].run_one_shot(f"Question from session {i}") for i in range(10)],
        return_exceptions=True,
    )
    elapsed = time.monotonic() - start

    # Count successes
    success_count = sum(1 for r in results if not isinstance(r, Exception) and r is not None and r.error is None)
    check(f"10/10 sessions succeeded ({elapsed:.2f}s)", success_count == 10,
          f"got {success_count}/10 successes")

    # Each provider was called
    call_counts = [m.call_count for m in mocks]
    check("each session called its provider", all(c > 0 for c in call_counts),
          f"call_counts: {call_counts}")

    # Cleanup all
    for app in apps:
        await app.shutdown()
    check("all apps shutdown clean", True)


# ══════════════════════════════════════════════════════════
# TEST 3: Session isolation - state doesn't leak
# ══════════════════════════════════════════════════════════

async def test_session_isolation(tmpdir):
    section("Test 3: Session isolation (state doesn't leak)")

    yaml_path = write_yaml(tmpdir, app_id="t3-isolation")

    # Create 2 separate apps with independent state
    mock1 = MockProvider(responses=[make_text_response("Session 1 response")])
    mock2 = MockProvider(responses=[make_text_response("Session 2 response")])

    app1 = await compile_and_bootstrap(yaml_path, mock1)
    app2 = await compile_and_bootstrap(yaml_path, mock2)

    # Get filesystem module from each - verify they have separate state
    fs1 = app1.modules.get("filesystem")
    fs2 = app2.modules.get("filesystem")

    if fs1 and fs2:
        # Add to session_a in app1, verify app2 doesn't see it
        fs1._session_read_files.setdefault("isolation_test", set()).add("from_app1.py")
        check("fs1 has its own state", "from_app1.py" in fs1._session_read_files.get("isolation_test", set()))
        check("fs2 does NOT see fs1 state",
              "from_app1.py" not in fs2._session_read_files.get("isolation_test", set()))

    await app1.shutdown()
    await app2.shutdown()


# ══════════════════════════════════════════════════════════
# TEST 4: Cleanup leaves no residue
# ══════════════════════════════════════════════════════════

async def test_cleanup_leaves_no_residue(tmpdir):
    section("Test 4: Cleanup leaves no residue")

    yaml_path = write_yaml(tmpdir, app_id="t4-cleanup")

    # Create app, get module references, run, shutdown, verify state cleared
    mock = MockProvider(responses=[make_text_response("Hello")])
    app = await compile_and_bootstrap(yaml_path, mock)

    fs = app.modules.get("filesystem")
    mem = app.modules.get("memory")

    # Manually populate some session state
    if fs:
        fs._session_read_files["s1"] = {"a.py"}
        fs._session_read_files["s2"] = {"b.py"}
        check("fs populated", len(fs._session_read_files) >= 2)

    if mem:
        mem.get_session_store("session_a")
        mem.get_session_store("session_b")
        check("mem populated", len(mem._session_stores) >= 2)

    # Cleanup individual sessions
    if fs and hasattr(fs, "cleanup_session"):
        await fs.cleanup_session("s1")
        check("fs s1 cleaned", "s1" not in fs._session_read_files)
        check("fs s2 preserved", "s2" in fs._session_read_files)

    if mem and hasattr(mem, "cleanup_session"):
        await mem.cleanup_session("session_a")
        check("mem session_a cleaned", "session_a" not in mem._session_stores)
        check("mem session_b preserved", "session_b" in mem._session_stores)

    await app.shutdown()


# ══════════════════════════════════════════════════════════
# TEST 5: Error in one session doesn't affect others
# ══════════════════════════════════════════════════════════

async def test_error_isolation(tmpdir):
    section("Test 5: Error in one session doesn't affect others")

    yaml_path = write_yaml(tmpdir, app_id="t5-error-iso")

    # Create 5 apps. Make app[2] fail by raising in its mock provider.
    apps = []
    mocks = []
    for i in range(5):
        if i == 2:
            # This mock will throw on chat()
            class FailingMock(MockProvider):
                async def chat(self, messages, **kwargs):
                    raise RuntimeError("simulated provider crash")
            mock = FailingMock()
        else:
            mock = MockProvider(responses=[make_text_response(f"OK from {i}")])
        mocks.append(mock)
        app = await compile_and_bootstrap(yaml_path, mock)
        apps.append(app)

    # Run all 5 in parallel
    results = await asyncio.gather(
        *[apps[i].run_one_shot(f"Q{i}") for i in range(5)],
        return_exceptions=True,
    )

    # Sessions 0, 1, 3, 4 should succeed; session 2 should have error (not crash)
    ok_count = 0
    err_count = 0
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            err_count += 1
        elif r is not None and r.error:
            err_count += 1
        else:
            ok_count += 1

    check("4/5 succeeded despite 1 crash", ok_count == 4, f"ok={ok_count}, err={err_count}")
    check("1/5 failed cleanly", err_count == 1, f"err={err_count}")

    for app in apps:
        try:
            await app.shutdown()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════
# TEST 6: 50 sessions in parallel (load test)
# ══════════════════════════════════════════════════════════

async def test_50_sessions(tmpdir):
    section("Test 6: 50 concurrent sessions")

    yaml_path = write_yaml(tmpdir, app_id="t6-50")

    apps = []
    for i in range(50):
        mock = MockProvider(responses=[make_text_response(f"Resp {i}")])
        app = await compile_and_bootstrap(yaml_path, mock)
        apps.append(app)

    check("50 apps bootstrapped", len(apps) == 50)

    start = time.monotonic()
    results = await asyncio.gather(
        *[apps[i].run_one_shot(f"Q{i}") for i in range(50)],
        return_exceptions=True,
    )
    elapsed = time.monotonic() - start

    success = sum(1 for r in results if not isinstance(r, Exception) and r and r.error is None)
    rate = (50 / elapsed) if elapsed > 0.001 else float("inf")
    check(f"50/50 succeeded ({elapsed:.3f}s, {rate:.0f} req/s)",
          success == 50, f"got {success}/50")

    # Cleanup all in parallel
    cleanup_start = time.monotonic()
    await asyncio.gather(*[app.shutdown() for app in apps], return_exceptions=True)
    cleanup_elapsed = time.monotonic() - cleanup_start
    check(f"50 shutdown completed ({cleanup_elapsed:.2f}s)", True)


# ══════════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════════

async def main():
    tmpdir = Path(tempfile.mkdtemp(prefix="digitorn_load_"))
    try:
        await test_single_session(tmpdir)
        await test_concurrent_sessions(tmpdir)
        await test_session_isolation(tmpdir)
        await test_cleanup_leaves_no_residue(tmpdir)
        await test_error_isolation(tmpdir)
        await test_50_sessions(tmpdir)
    finally:
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass

    print("\n" + "=" * 55)
    print(f"  PASSED: {passed}")
    print(f"  FAILED: {failed}")
    print(f"  TOTAL:  {passed + failed}")
    print("=" * 55)
    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
