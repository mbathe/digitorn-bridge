"""Tests for the second wave of filesystem namespaces.

Covers features A→J of the "push further" iteration:

- C: ``{{asset_b64.X}}`` - data URI
- D: ``{{include:path}}`` - YAML fragment
- B: Markdown image path rewrite in loaded prompts
- G: Frontmatter parsing + validation
- E: ``capabilities: [...]`` auto-loading from skills/
- F: Locale-suffixed prompt resolution (prompts/X.fr.md)
- A: BundleHotReloader (snapshot + fire path)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages"))


def _bundle(tmp_path: Path) -> Path:
    """Build a minimal bundle with every subdir populated."""
    b = tmp_path / "app"
    b.mkdir()
    (b / "prompts").mkdir()
    (b / "skills").mkdir()
    (b / "assets").mkdir()
    return b


# ── C: asset_b64 ────────────────────────────────────────────────────


def test_asset_b64_returns_data_uri(tmp_path: Path) -> None:
    from digitorn.core.app.variables import resolve_variables

    b = _bundle(tmp_path)
    (b / "assets" / "tiny.png").write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    )
    result = resolve_variables(
        "{{asset_b64.tiny.png}}",
        variables={},
        bundle_dir=b,
        app_id="app",
    )
    assert result.startswith("data:image/png;base64,")
    import base64
    payload = result.split(",", 1)[1]
    assert base64.b64decode(payload).startswith(b"\x89PNG")


def test_asset_b64_size_cap_raises(tmp_path: Path) -> None:
    from digitorn.core.app.variables import resolve_variables
    import os

    b = _bundle(tmp_path)
    # 100 kB file - exceeds the default 64 kB cap
    (b / "assets" / "big.png").write_bytes(b"\x00" * (100 * 1024))

    with pytest.raises(ValueError, match="exceeds the"):
        resolve_variables(
            "{{asset_b64.big.png}}",
            variables={},
            bundle_dir=b,
            app_id="app",
        )


def test_asset_b64_env_override_cap(tmp_path: Path) -> None:
    """``DIGITORN_ASSET_B64_MAX_BYTES`` lets admins bump the cap."""
    from digitorn.core.app.variables import resolve_variables
    import os

    b = _bundle(tmp_path)
    (b / "assets" / "medium.png").write_bytes(b"\x00" * (80 * 1024))
    os.environ["DIGITORN_ASSET_B64_MAX_BYTES"] = str(200 * 1024)
    try:
        result = resolve_variables(
            "{{asset_b64.medium.png}}",
            variables={},
            bundle_dir=b,
            app_id="app",
        )
        assert result.startswith("data:")
    finally:
        os.environ.pop("DIGITORN_ASSET_B64_MAX_BYTES", None)


# ── D: include YAML fragment ───────────────────────────────────────


def test_include_yaml_fragment(tmp_path: Path) -> None:
    from digitorn.core.app.variables import resolve_variables

    b = _bundle(tmp_path)
    (b / "fragments").mkdir()
    (b / "fragments" / "brain.yaml").write_text(
        "provider: anthropic\nmodel: claude-sonnet-4-5\ntemperature: 0.1\n",
        encoding="utf-8",
    )
    result = resolve_variables(
        "{{include:fragments/brain.yaml}}",
        variables={},
        bundle_dir=b,
        app_id="app",
    )
    # The include returns a JSON-serialized dict (JSON is valid YAML)
    import json
    parsed = json.loads(result)
    assert parsed["provider"] == "anthropic"
    assert parsed["model"] == "claude-sonnet-4-5"


def test_include_rejects_missing(tmp_path: Path) -> None:
    from digitorn.core.app.variables import resolve_variables

    b = _bundle(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        resolve_variables(
            "{{include:nope.yaml}}",
            variables={},
            bundle_dir=b,
            app_id="app",
        )


def test_include_rejects_path_traversal(tmp_path: Path) -> None:
    from digitorn.core.app.variables import resolve_variables

    b = _bundle(tmp_path)
    with pytest.raises(ValueError, match="escapes bundle"):
        resolve_variables(
            "{{include:../../etc/passwd}}",
            variables={},
            bundle_dir=b,
            app_id="app",
        )


# ── B: markdown image rewrite ───────────────────────────────────────


def test_markdown_image_rewrite_inside_prompt(tmp_path: Path) -> None:
    from digitorn.core.app.variables import resolve_variables

    b = _bundle(tmp_path)
    (b / "assets" / "diagram.svg").write_text("<svg/>", encoding="utf-8")
    (b / "prompts" / "doc.md").write_text(
        "# Docs\n\n![architecture](../assets/diagram.svg)\n\nDetails here.",
        encoding="utf-8",
    )
    result = resolve_variables(
        "{{prompt.doc}}",
        variables={},
        bundle_dir=b,
        app_id="my-app",
    )
    assert "/api/apps/my-app/assets/assets/diagram.svg" in result
    assert "../assets/diagram.svg" not in result


def test_markdown_external_urls_preserved(tmp_path: Path) -> None:
    from digitorn.core.app.variables import resolve_variables

    b = _bundle(tmp_path)
    (b / "prompts" / "ext.md").write_text(
        "![logo](https://example.com/logo.png)\n![local](assets/missing.png)",
        encoding="utf-8",
    )
    result = resolve_variables(
        "{{prompt.ext}}",
        variables={},
        bundle_dir=b,
        app_id="app",
    )
    # External URL untouched
    assert "https://example.com/logo.png" in result
    # Relative path that doesn't match a file stays as-is
    assert "assets/missing.png" in result


# ── G: frontmatter ──────────────────────────────────────────────────


def test_frontmatter_stripped_from_body(tmp_path: Path) -> None:
    from digitorn.core.app.variables import resolve_variables

    b = _bundle(tmp_path)
    (b / "prompts" / "with_fm.md").write_text(
        "---\nversion: 2\ndescription: Test prompt\n---\n\nHello world",
        encoding="utf-8",
    )
    result = resolve_variables(
        "{{prompt.with_fm}}",
        variables={},
        bundle_dir=b,
        app_id="app",
    )
    assert "---" not in result
    assert "version" not in result
    assert result.strip() == "Hello world"


def test_frontmatter_metadata_collected(tmp_path: Path) -> None:
    from digitorn.core.app.variables import (
        bundle_context,
        collected_prompt_metadata,
        resolve_variables,
    )

    b = _bundle(tmp_path)
    (b / "prompts" / "meta.md").write_text(
        "---\nversion: 3\nmax_tokens_estimate: 500\nmin_model: claude-haiku\n---\n\nBody",
        encoding="utf-8",
    )
    with bundle_context(bundle_dir=b, app_id="app"):
        resolve_variables("{{prompt.meta}}", variables={})
        metadata = collected_prompt_metadata()
    assert "prompt.meta" in metadata
    assert metadata["prompt.meta"]["version"] == 3
    assert metadata["prompt.meta"]["max_tokens_estimate"] == 500


def test_frontmatter_validates_required_variables() -> None:
    from digitorn.core.app.compiler import _validate_prompt_metadata

    errors: list[str] = []
    _validate_prompt_metadata(
        metadata={
            "prompt.foo": {
                "variables_required": ["user_name", "missing_var"],
            },
        },
        declared_variables={"user_name"},
        errors=errors,
    )
    assert len(errors) == 1
    assert "missing_var" in errors[0]


# ── F: locale-suffixed prompts ──────────────────────────────────────


def test_locale_prefers_suffixed_file(tmp_path: Path) -> None:
    from digitorn.core.app.variables import resolve_variables

    b = _bundle(tmp_path)
    (b / "prompts" / "system.md").write_text("Default prompt", encoding="utf-8")
    (b / "prompts" / "system.fr.md").write_text(
        "Prompt en français", encoding="utf-8",
    )

    result_default = resolve_variables(
        "{{prompt.system}}", variables={}, bundle_dir=b, app_id="app",
    )
    assert result_default == "Default prompt"

    # Now with locale=fr
    from digitorn.core.app.variables import bundle_context
    with bundle_context(bundle_dir=b, app_id="app", locale="fr"):
        result_fr = resolve_variables("{{prompt.system}}", variables={})
    assert result_fr == "Prompt en français"


def test_locale_fallback_when_suffix_missing(tmp_path: Path) -> None:
    from digitorn.core.app.variables import bundle_context, resolve_variables

    b = _bundle(tmp_path)
    (b / "prompts" / "system.md").write_text("Only default", encoding="utf-8")

    with bundle_context(bundle_dir=b, app_id="app", locale="es"):
        result = resolve_variables("{{prompt.system}}", variables={})
    assert result == "Only default"


def test_list_available_locales(tmp_path: Path) -> None:
    from digitorn.core.app.variables import list_available_locales

    b = _bundle(tmp_path)
    (b / "prompts" / "system.md").write_text("", encoding="utf-8")
    (b / "prompts" / "system.en.md").write_text("", encoding="utf-8")
    (b / "prompts" / "system.fr.md").write_text("", encoding="utf-8")
    (b / "prompts" / "system.es.md").write_text("", encoding="utf-8")

    locales = list_available_locales(b)
    assert set(locales) == {"en", "fr", "es"}


# ── E: capabilities auto-loading ────────────────────────────────────


def test_capabilities_auto_loaded_into_system_prompt(tmp_path: Path) -> None:
    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.modules.registry import ModuleRegistry

    b = _bundle(tmp_path)
    (b / "prompts" / "main.md").write_text(
        "You are the main agent.", encoding="utf-8",
    )
    (b / "skills" / "commit.md").write_text(
        "Use conventional commit format.", encoding="utf-8",
    )
    (b / "skills" / "review.md").write_text(
        "Focus on readability and correctness.", encoding="utf-8",
    )

    yaml_text = """
app:
  app_id: cap-app
  name: Cap App
  version: "1.0"

modules:
  filesystem:
    config: {}

agents:
  - id: main
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: "{{env.ANTHROPIC_API_KEY}}"
    system_prompt: "{{prompt.main}}"
    capabilities: [commit, review]
"""
    (b / "app.yaml").write_text(yaml_text, encoding="utf-8")

    reg = ModuleRegistry()
    try:
        from digitorn.core.loader import load_modules
        load_modules(reg, enabled=None, disabled=None, load_all=True)
    except Exception:
        pass

    compiler = AppYAMLCompiler(reg)
    compiled = compiler.compile_file(b / "app.yaml")
    sp = compiled.agents[0].system_prompt
    assert "You are the main agent" in sp
    assert "## Available capabilities" in sp
    assert "commit" in sp
    assert "conventional commit format" in sp
    assert "review" in sp
    assert "readability" in sp


def test_capabilities_missing_skill_raises(tmp_path: Path) -> None:
    from digitorn.core.app.compiler import AppCompilationError, AppYAMLCompiler
    from digitorn.modules.registry import ModuleRegistry

    b = _bundle(tmp_path)
    (b / "prompts" / "main.md").write_text("Base", encoding="utf-8")

    yaml_text = """
app:
  app_id: cap-missing
  name: Cap Missing
  version: "1.0"

modules:
  filesystem:
    config: {}

agents:
  - id: main
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: "{{env.ANTHROPIC_API_KEY}}"
    system_prompt: "{{prompt.main}}"
    capabilities: [nonexistent]
"""
    (b / "app.yaml").write_text(yaml_text, encoding="utf-8")

    reg = ModuleRegistry()
    try:
        from digitorn.core.loader import load_modules
        load_modules(reg, enabled=None, disabled=None, load_all=True)
    except Exception:
        pass

    compiler = AppYAMLCompiler(reg)
    with pytest.raises(AppCompilationError) as exc_info:
        compiler.compile_file(b / "app.yaml")
    assert "nonexistent" in str(exc_info.value).lower()


# ── A: BundleHotReloader ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hot_reloader_detects_prompt_change(tmp_path: Path) -> None:
    from digitorn.core.app.hot_reload import BundleHotReloader

    b = _bundle(tmp_path)
    (b / "prompts" / "main.md").write_text("v1", encoding="utf-8")

    fired = asyncio.Event()

    async def on_change():
        fired.set()

    reloader = BundleHotReloader(
        app_id="test", bundle_dir=b, on_change=on_change,
    )
    await reloader.start()
    try:
        # Modify the prompt
        await asyncio.sleep(0.1)
        (b / "prompts" / "main.md").write_text("v2 changed", encoding="utf-8")

        # Wait up to 5s for the reloader to detect + debounce + fire
        try:
            await asyncio.wait_for(fired.wait(), timeout=5)
        except asyncio.TimeoutError:
            pytest.fail("hot reloader didn't fire within 5s")
    finally:
        await reloader.stop()


@pytest.mark.asyncio
async def test_hot_reloader_ignores_unrelated_file(tmp_path: Path) -> None:
    """Touching a file OUTSIDE prompts/skills/assets doesn't fire."""
    from digitorn.core.app.hot_reload import BundleHotReloader

    b = _bundle(tmp_path)
    (b / "app.yaml").write_text("original", encoding="utf-8")

    fired = asyncio.Event()

    async def on_change():
        fired.set()

    reloader = BundleHotReloader(
        app_id="test", bundle_dir=b, on_change=on_change,
    )
    await reloader.start()
    try:
        await asyncio.sleep(0.1)
        (b / "app.yaml").write_text("modified", encoding="utf-8")
        # Give the reloader 2s to (wrongly) detect the change
        await asyncio.sleep(2)
        assert not fired.is_set()
    finally:
        await reloader.stop()
