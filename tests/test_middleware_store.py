"""Tests for middleware store, registry, and scaffold."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from digitorn.core.middleware_store import (
    MiddlewareDescriptor,
    MiddlewareRegistry,
    create_middleware_scaffold,
    get_middleware_registry,
    install_middleware,
    uninstall_middleware,
)
from digitorn.core.paths import builtin_middleware_dir, user_middleware_dir


# ── Registry discovery ───────────────────────────────────────────────


def test_registry_discovers_builtins():
    """Registry finds all 11 builtin middlewares."""
    reg = MiddlewareRegistry()
    count = reg.discover()
    assert count == 15


def test_registry_list_all():
    reg = MiddlewareRegistry()
    reg.discover()
    all_mw = reg.list_all()
    ids = {d.middleware_id for d in all_mw}
    assert "mask_secrets" in ids
    assert "audit" in ids
    assert "budget" in ids
    assert "auto_heal" in ids


def test_registry_list_by_level():
    reg = MiddlewareRegistry()
    reg.discover()
    app_mw = reg.list_by_level("app")
    app_ids = {d.middleware_id for d in app_mw}
    assert "mask_secrets" in app_ids
    assert "content_filter" in app_ids
    # "all" level middlewares should appear in every level filter
    assert "audit" in app_ids
    assert "retry" in app_ids


def test_registry_get_descriptor():
    reg = MiddlewareRegistry()
    reg.discover()
    desc = reg.get_descriptor("mask_secrets")
    assert desc is not None
    assert desc.middleware_id == "mask_secrets"
    assert desc.level == "app"
    assert desc.source == "builtin"


def test_registry_get_nonexistent():
    reg = MiddlewareRegistry()
    reg.discover()
    assert reg.get_descriptor("nonexistent") is None


# ── Registry instantiation ───────────────────────────────────────────


def test_registry_instantiate_app():
    reg = MiddlewareRegistry()
    reg.discover()
    mw = reg.instantiate("mask_secrets", {"patterns": ["custom"]})
    assert mw is not None
    assert hasattr(mw, "before")
    assert hasattr(mw, "after")


def test_registry_instantiate_module():
    reg = MiddlewareRegistry()
    reg.discover()
    mw = reg.instantiate("retry", {"max_attempts": 5})
    assert mw is not None
    assert mw.max_attempts == 5


def test_registry_instantiate_mcp():
    reg = MiddlewareRegistry()
    reg.discover()
    mw = reg.instantiate("budget", {"max_calls_per_hour": 50})
    assert mw is not None
    assert mw.max_calls_per_hour == 50


def test_registry_instantiate_nonexistent():
    reg = MiddlewareRegistry()
    reg.discover()
    assert reg.instantiate("nonexistent") is None


# ── Scaffold generation ──────────────────────────────────────────────


def test_create_scaffold_app(tmp_path):
    dest = create_middleware_scaffold("test_app_mw", level="app", output_dir=tmp_path)
    assert dest.exists()
    assert (dest / "digitorn-middleware.toml").exists()
    assert (dest / "middleware.py").exists()
    # TOML has correct ID
    content = (dest / "digitorn-middleware.toml").read_text()
    assert 'middleware_id = "test_app_mw"' in content
    assert 'level = "app"' in content
    # Python file has class
    py = (dest / "middleware.py").read_text()
    assert "class TestAppMwMiddleware" in py
    assert "async def before" in py
    assert "async def after" in py


def test_create_scaffold_module(tmp_path):
    dest = create_middleware_scaffold("test_mod_mw", level="module", output_dir=tmp_path)
    py = (dest / "middleware.py").read_text()
    assert "class TestModMwMiddleware" in py
    assert "async def __call__" in py


def test_create_scaffold_mcp(tmp_path):
    dest = create_middleware_scaffold("test_mcp_mw", level="mcp", output_dir=tmp_path)
    py = (dest / "middleware.py").read_text()
    assert "class TestMcpMwMiddleware" in py
    assert "async def __call__" in py


# ── Install / uninstall ──────────────────────────────────────────────


def test_install_and_uninstall(tmp_path):
    """Install a scaffold, verify it's discoverable, then uninstall."""
    # Create scaffold
    scaffold = create_middleware_scaffold("test_install", level="app", output_dir=tmp_path)

    # Install
    desc = install_middleware(scaffold)
    assert desc.middleware_id == "test_install"
    assert desc.source == "user"

    # Verify discoverable
    reg = MiddlewareRegistry()
    reg.discover()
    found = reg.get_descriptor("test_install")
    assert found is not None
    assert found.source == "user"

    # Uninstall
    removed = uninstall_middleware("test_install")
    assert removed is True

    # Verify gone
    reg2 = MiddlewareRegistry()
    reg2.discover()
    assert reg2.get_descriptor("test_install") is None


def test_install_invalid_no_toml(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="No digitorn-middleware.toml"):
        install_middleware(empty)


def test_uninstall_nonexistent():
    assert uninstall_middleware("definitely_not_installed_xyz") is False


# ── Singleton registry ───────────────────────────────────────────────


def test_singleton_registry():
    reg1 = get_middleware_registry()
    reg2 = get_middleware_registry()
    assert reg1 is reg2


# ── TOML descriptor ──────────────────────────────────────────────────


def test_descriptor_defaults():
    d = MiddlewareDescriptor(middleware_id="test")
    assert d.version == "1.0.0"
    assert d.level == "app"
    assert d.source == "builtin"
    assert d.enabled is True
    assert d.config_schema == {}
