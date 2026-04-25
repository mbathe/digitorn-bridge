"""Pipeline runtime tests."""

from __future__ import annotations

import pytest

from digitorn.core.pipeline import (
    PipelineStep,
    StepResult,
    compile_pipeline,
    _resolve_template,
)


class TestCompilePipeline:
    def test_basic(self):
        steps = compile_pipeline([
            {"app": "analyzer", "input": "{{input}}"},
            {"app": "scorer", "input": "{{steps[0].output}}"},
        ])
        assert len(steps) == 2
        assert steps[0].app_id == "analyzer"
        assert steps[1].app_id == "scorer"

    def test_missing_app(self):
        with pytest.raises(ValueError, match="missing 'app'"):
            compile_pipeline([{"input": "test"}])

    def test_defaults(self):
        steps = compile_pipeline([{"app": "test"}])
        assert steps[0].input_template == "{{input}}"
        assert steps[0].timeout == 120.0
        assert steps[0].on_error == "stop"

    def test_custom_config(self):
        steps = compile_pipeline([{
            "app": "slow",
            "input": "custom",
            "timeout": 300,
            "on_error": "continue",
        }])
        assert steps[0].timeout == 300
        assert steps[0].on_error == "continue"


class TestResolveTemplate:
    def test_input(self):
        assert _resolve_template("{{input}}", "hello", []) == "hello"

    def test_step_output(self):
        results = [
            StepResult(app_id="a", success=True, output="result_a", duration=1.0),
        ]
        assert _resolve_template("{{steps[0].output}}", "", results) == "result_a"

    def test_step_app_id(self):
        results = [
            StepResult(app_id="analyzer", success=True, output="x", duration=1.0),
        ]
        assert _resolve_template("{{steps[0].app_id}}", "", results) == "analyzer"

    def test_step_success(self):
        results = [
            StepResult(app_id="a", success=False, output="", duration=1.0, error="fail"),
        ]
        assert _resolve_template("{{steps[0].success}}", "", results) == "false"

    def test_mixed(self):
        results = [
            StepResult(app_id="a", success=True, output="analysis done", duration=1.0),
        ]
        tpl = "Score this: {{steps[0].output}} (original: {{input}})"
        result = _resolve_template(tpl, "src/auth.py", results)
        assert result == "Score this: analysis done (original: src/auth.py)"

    def test_out_of_range(self):
        assert _resolve_template("{{steps[5].output}}", "", []) == "{{steps[5].output}}"

    def test_chained(self):
        results = [
            StepResult(app_id="a", success=True, output="step0", duration=1.0),
            StepResult(app_id="b", success=True, output="step1", duration=1.0),
        ]
        assert _resolve_template("{{steps[1].output}}", "", results) == "step1"

    def test_no_template(self):
        assert _resolve_template("plain text", "input", []) == "plain text"
