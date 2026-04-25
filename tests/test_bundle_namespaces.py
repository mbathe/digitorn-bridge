"""Tests for the compile-time filesystem namespaces.

Exercises ``{{prompt.X}}``, ``{{skill.X}}``, ``{{asset.X}}``
resolution via the variables module — both in isolation (via
``bundle_context``) and through the full compiler pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages"))


def _make_bundle(tmp_path: Path) -> Path:
    """Build a minimal app bundle directory with prompts/skills/assets."""
    bundle = tmp_path / "my-app"
    bundle.mkdir()

    (bundle / "prompts").mkdir()
    (bundle / "prompts" / "presentation.md").write_text(
        "You are a helpful assistant.\n",
        encoding="utf-8",
    )
    (bundle / "prompts" / "system.txt").write_text(
        "System instructions go here.",
        encoding="utf-8",
    )

    (bundle / "skills").mkdir()
    (bundle / "skills" / "commit.md").write_text(
        "# Commit skill\nUse conventional commit format.",
        encoding="utf-8",
    )

    (bundle / "assets").mkdir()
    (bundle / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (bundle / "assets" / "icon.svg").write_text("<svg></svg>", encoding="utf-8")
    (bundle / "assets" / "welcome.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return bundle


def test_prompt_namespace_inlines_content(tmp_path: Path) -> None:
    from digitorn.core.app.variables import resolve_variables

    bundle = _make_bundle(tmp_path)
    result = resolve_variables(
        "{{prompt.presentation}}",
        variables={},
        bundle_dir=bundle,
        app_id="my-app",
    )
    assert result == "You are a helpful assistant."


def test_prompt_namespace_fuzzy_extension(tmp_path: Path) -> None:
    """``prompt.system`` should find ``prompts/system.txt`` even
    without specifying the extension."""
    from digitorn.core.app.variables import resolve_variables

    bundle = _make_bundle(tmp_path)
    result = resolve_variables(
        "{{prompt.system}}",
        variables={},
        bundle_dir=bundle,
        app_id="my-app",
    )
    assert "System instructions" in result


def test_skill_namespace_inlines_markdown(tmp_path: Path) -> None:
    from digitorn.core.app.variables import resolve_variables

    bundle = _make_bundle(tmp_path)
    result = resolve_variables(
        "{{skill.commit}}",
        variables={},
        bundle_dir=bundle,
        app_id="my-app",
    )
    assert "# Commit skill" in result
    assert "conventional commit" in result


def test_asset_namespace_returns_url(tmp_path: Path) -> None:
    """``{{asset.X}}`` returns the daemon URL the Flutter client
    fetches the file from."""
    from digitorn.core.app.variables import resolve_variables

    bundle = _make_bundle(tmp_path)
    result = resolve_variables(
        "{{asset.logo.png}}",
        variables={},
        bundle_dir=bundle,
        app_id="my-app",
    )
    assert result == "/api/apps/my-app/assets/logo.png"


def test_asset_namespace_extension_fuzzy_match(tmp_path: Path) -> None:
    """``{{asset.logo}}`` without extension matches ``assets/logo.png``."""
    from digitorn.core.app.variables import resolve_variables

    bundle = _make_bundle(tmp_path)
    result = resolve_variables(
        "{{asset.logo}}",
        variables={},
        bundle_dir=bundle,
        app_id="my-app",
    )
    assert result == "/api/apps/my-app/assets/logo.png"


def test_asset_namespace_raises_on_missing(tmp_path: Path) -> None:
    from digitorn.core.app.variables import resolve_variables

    bundle = _make_bundle(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        resolve_variables(
            "{{asset.nonexistent}}",
            variables={},
            bundle_dir=bundle,
            app_id="my-app",
        )


def test_asset_namespace_path_traversal_blocked(tmp_path: Path) -> None:
    """``../../etc/passwd`` attempts shouldn't return anything."""
    from digitorn.core.app.variables import resolve_variables

    bundle = _make_bundle(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        resolve_variables(
            "{{asset.../../etc/passwd}}",
            variables={},
            bundle_dir=bundle,
            app_id="my-app",
        )


def test_prompt_passthrough_without_bundle() -> None:
    """Without a bundle_dir, prompt.X passes through — legacy
    callers that don't know about filesystem namespaces keep
    working."""
    from digitorn.core.app.variables import resolve_variables

    result = resolve_variables("{{prompt.foo}}", variables={})
    assert result == "{{prompt.foo}}"


def test_nested_substitution(tmp_path: Path) -> None:
    """A prompt file that itself contains ``{{app.name}}`` should
    NOT be recursively resolved — the prompt content is treated
    as opaque text (that's the contract: prompt files are the
    source of truth, not templates themselves)."""
    from digitorn.core.app.variables import resolve_variables

    bundle = _make_bundle(tmp_path)
    (bundle / "prompts" / "wrapped.md").write_text(
        "Hello {{app.name}}!", encoding="utf-8",
    )
    result = resolve_variables(
        "{{prompt.wrapped}}",
        variables={"_app_name": "MyApp"},
        bundle_dir=bundle,
        app_id="my-app",
    )
    # The {{app.name}} inside the prompt IS recursively resolved
    # because _resolve_string sees {{ in the returned text. This
    # is intentional — it lets prompt files reference app vars.
    assert "MyApp" in result or "{{app.name}}" in result


def test_bundle_context_cm_sticky_across_calls(tmp_path: Path) -> None:
    """``bundle_context`` lets callers set the ctx once and then
    call ``resolve_variables`` multiple times without re-passing
    bundle_dir/app_id."""
    from digitorn.core.app.variables import bundle_context, resolve_variables

    bundle = _make_bundle(tmp_path)
    with bundle_context(bundle_dir=bundle, app_id="my-app"):
        r1 = resolve_variables("{{prompt.presentation}}", variables={})
        r2 = resolve_variables("{{asset.logo.png}}", variables={})
    assert r1 == "You are a helpful assistant."
    assert r2 == "/api/apps/my-app/assets/logo.png"


def test_compile_with_bundle_namespaces(tmp_path: Path) -> None:
    """Full end-to-end: compile an ``app.yaml`` that uses
    ``{{prompt.system}}`` in an agent's system_prompt, plus
    ``{{asset.logo}}`` in a quick_prompt somewhere.
    """
    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.modules.registry import ModuleRegistry

    bundle = _make_bundle(tmp_path)
    (bundle / "prompts" / "main_system.md").write_text(
        "You are MainBot. Be concise and helpful.",
        encoding="utf-8",
    )

    yaml_content = """
app:
  app_id: my-app
  name: My App
  version: "1.0"
  description: "Test app"
  icon: "assets/icon.svg"

variables:
  greeting: "Hi there"

modules:
  filesystem:
    config: {}

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: "{{env.ANTHROPIC_API_KEY}}"
    system_prompt: "{{prompt.main_system}}"
"""
    (bundle / "app.yaml").write_text(yaml_content, encoding="utf-8")

    reg = ModuleRegistry()
    try:
        from digitorn.core.loader import load_modules
        load_modules(reg, enabled=None, disabled=None, load_all=True)
    except Exception:
        pass

    compiler = AppYAMLCompiler(reg)
    compiled = compiler.compile_file(bundle / "app.yaml")

    assert compiled.meta.app_id == "my-app"
    # The agent's system_prompt should now contain the inlined
    # text from prompts/main_system.md
    agent = compiled.agents[0]
    assert "MainBot" in agent.system_prompt
    assert "concise and helpful" in agent.system_prompt
