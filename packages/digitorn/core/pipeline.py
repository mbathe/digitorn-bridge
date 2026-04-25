"""Pipeline runtime — execute a sequence of app calls.

A pipeline chains multiple one_shot apps. Each step receives the output
of the previous step via template variables.

Template variables available in step inputs:
    {{input}}           — original pipeline input
    {{steps[N].output}} — output of step N (0-indexed)
    {{steps[N].app_id}} — app_id of step N
    {{steps[N].success}} — whether step N succeeded

Example YAML:
    pipeline:
      - app: code-analyzer
        input: "{{input}}"
      - app: vulnerability-scorer
        input: "{{steps[0].output}}"
      - app: report-generator
        input: "{{steps[1].output}}"
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass
class PipelineStep:
    app_id: str
    input_template: str
    timeout: float = 120.0
    on_error: str = "stop"


@dataclass
class StepResult:
    app_id: str
    success: bool
    output: str
    duration: float
    error: str = ""


@dataclass
class PipelineResult:
    success: bool
    steps: list[StepResult]
    final_output: str
    total_duration: float
    error: str = ""


def compile_pipeline(raw_steps: list[dict[str, Any]]) -> list[PipelineStep]:
    steps = []
    for i, raw in enumerate(raw_steps):
        app_id = raw.get("app")
        if not app_id:
            raise ValueError(f"Pipeline step {i}: missing 'app' field")
        input_tpl = raw.get("input", "{{input}}")
        timeout = raw.get("timeout", 120.0)
        on_error = raw.get("on_error", "stop")
        steps.append(PipelineStep(
            app_id=app_id,
            input_template=input_tpl,
            timeout=timeout,
            on_error=on_error,
        ))
    return steps


def _resolve_template(template: str, user_input: str, results: list[StepResult]) -> str:
    resolved = template.replace("{{input}}", user_input)

    def _replace_step_ref(m: re.Match) -> str:
        idx = int(m.group(1))
        attr = m.group(2)
        if 0 <= idx < len(results):
            r = results[idx]
            if attr == "output":
                return r.output
            elif attr == "app_id":
                return r.app_id
            elif attr == "success":
                return str(r.success).lower()
            elif attr == "error":
                return r.error
        return m.group(0)

    resolved = re.sub(r"\{\{steps\[(\d+)\]\.(\w+)\}\}", _replace_step_ref, resolved)
    return resolved


async def execute_pipeline(
    steps: list[PipelineStep],
    user_input: str,
    *,
    daemon_url: str = "http://127.0.0.1:8000",
    on_step_start: Any = None,
    on_step_end: Any = None,
) -> PipelineResult:
    import httpx
    from urllib.parse import quote, urlparse

    parsed = urlparse(daemon_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported daemon_url scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise ValueError("daemon_url must have a hostname")
    base = daemon_url.rstrip("/")

    results: list[StepResult] = []
    t0 = time.monotonic()

    async with httpx.AsyncClient() as client:
      for i, step in enumerate(steps):
        step_input = _resolve_template(step.input_template, user_input, results)

        if on_step_start:
            try:
                await on_step_start(i, step.app_id, step_input)
            except Exception:
                logger.warning("pipeline_callback_error step=%d", i, exc_info=True)

        step_t0 = time.monotonic()

        try:
                safe_id = quote(step.app_id, safe="")
                resp = await client.post(
                    f"{base}/api/apps/{safe_id}/run",
                    json={"input": step_input},
                    timeout=step.timeout,
                )
                data = resp.json()

                if data.get("success"):
                    output = data.get("data", {}).get("content", "")
                    sr = StepResult(
                        app_id=step.app_id,
                        success=True,
                        output=output,
                        duration=time.monotonic() - step_t0,
                    )
                else:
                    sr = StepResult(
                        app_id=step.app_id,
                        success=False,
                        output="",
                        duration=time.monotonic() - step_t0,
                        error=data.get("error", "Unknown error"),
                    )
        except httpx.TimeoutException:
            sr = StepResult(
                app_id=step.app_id,
                success=False,
                output="",
                duration=time.monotonic() - step_t0,
                error=f"Timeout after {step.timeout}s",
            )
        except Exception as exc:
            sr = StepResult(
                app_id=step.app_id,
                success=False,
                output="",
                duration=time.monotonic() - step_t0,
                error=str(exc),
            )

        results.append(sr)

        if on_step_end:
            try:
                await on_step_end(i, sr)
            except Exception:
                pass  # Callback failure must not abort pipeline

        logger.info(
            "pipeline_step",
            step=i,
            app_id=step.app_id,
            success=sr.success,
            duration=f"{sr.duration:.1f}s",
        )

        if not sr.success and step.on_error == "stop":
            return PipelineResult(
                success=False,
                steps=results,
                final_output="",
                total_duration=time.monotonic() - t0,
                error=f"Step {i} ({step.app_id}) failed: {sr.error}",
            )

    final = results[-1].output if results else ""
    return PipelineResult(
        success=True,
        steps=results,
        final_output=final,
        total_duration=time.monotonic() - t0,
    )
