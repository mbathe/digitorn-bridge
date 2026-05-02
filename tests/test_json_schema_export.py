"""Phase 6 - JSON Schema export tests.

Three guarantees for the schema published at ``docs/schema/v1.json``:

  1. The script runs without errors and produces a non-empty document.
  2. The generated schema is valid JSON Schema (Draft 2020-12).
  3. ``--check`` fails fast when the published file drifts from the
     Pydantic source of truth.

The generated schema is what an editor (VSCode + Red Hat YAML extension,
JetBrains, neovim+coc) loads when the user adds:

    # yaml-language-server: $schema=https://digitorn.ai/schema/v1.json

at the top of their ``app.yaml``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "generate_json_schema.py"
DEFAULT_OUTPUT = ROOT / "docs" / "schema" / "v1.json"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


class TestSchemaGeneration:

    def test_script_runs_and_writes_file(self, tmp_path: Path):
        out = tmp_path / "schema.json"
        result = _run("--output", str(out))
        assert result.returncode == 0, result.stderr
        assert out.is_file()
        assert out.stat().st_size > 1000  # full schema, not stub

    def test_generated_schema_is_valid_json(self, tmp_path: Path):
        out = tmp_path / "schema.json"
        _run("--output", str(out))
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["$schema"].startswith("https://json-schema.org/")
        assert data["title"] == "Digitorn app.yaml"
        # The 7 canonical top-level blocks (v2 schema).
        properties = data.get("properties", {})
        for required_root in ("app", "runtime", "agents", "tools", "security", "ui", "dev"):
            assert required_root in properties, f"missing root field: {required_root}"

    def test_schema_includes_flow_block(self, tmp_path: Path):
        """The Phase 2 flow: block must be discoverable in the schema."""
        out = tmp_path / "schema.json"
        _run("--output", str(out))
        data = json.loads(out.read_text(encoding="utf-8"))
        defs = data.get("$defs", {})
        # FlowConfig is referenced as a $def by the AppDefinition root.
        assert "FlowConfig" in defs, (
            f"FlowConfig missing from $defs. Available: {sorted(defs.keys())[:20]}"
        )

    def test_schema_includes_typed_models(self, tmp_path: Path):
        """The Phase 2 typed dict replacements must appear in $defs."""
        out = tmp_path / "schema.json"
        _run("--output", str(out))
        data = json.loads(out.read_text(encoding="utf-8"))
        defs = data.get("$defs", {})
        for model in ("AgentPoolConfig", "QuickPrompt", "SkillEntry", "SlashCommand"):
            assert model in defs, f"{model} missing from $defs"


class TestDriftDetection:

    def test_check_passes_when_in_sync(self, tmp_path: Path):
        """Generate, then check against the same path - must succeed."""
        out = tmp_path / "schema.json"
        _run("--output", str(out))
        result = _run("--output", str(out), "--check")
        assert result.returncode == 0, result.stderr

    def test_check_fails_when_file_missing(self, tmp_path: Path):
        out = tmp_path / "missing.json"
        result = _run("--output", str(out), "--check")
        assert result.returncode == 1
        assert "not generated yet" in result.stderr.lower()

    def test_check_fails_when_drifted(self, tmp_path: Path):
        out = tmp_path / "schema.json"
        _run("--output", str(out))
        # Tamper with the file.
        text = out.read_text(encoding="utf-8")
        out.write_text(text.replace('"app"', '"tampered"', 1), encoding="utf-8")
        result = _run("--output", str(out), "--check")
        assert result.returncode == 1
        assert "drift" in result.stderr.lower()


class TestPublishedFileInSync:
    """The committed ``docs/schema/v1.json`` must match the current
    Pydantic source. Otherwise editors load a stale schema and miss
    every field added since the last run."""

    def test_published_schema_in_sync(self):
        if not DEFAULT_OUTPUT.is_file():
            pytest.skip(
                "docs/schema/v1.json not present yet - run "
                "`py -3.12 tools/generate_json_schema.py` to bootstrap."
            )
        result = _run("--check")
        assert result.returncode == 0, result.stderr
