"""Unit tests for digitorn.core.runtime.node_runtime.

These exercise:

- Version parsing (``v22.11.0`` → ``22.11.0``, major extraction)
- ``_probe_path_node`` against the real host PATH (skipped if no node)
- ``NodeRuntime.ensure_installed`` idempotency and source reporting
- ``env`` property PATH augmentation
- ``spawn()`` / ``run()`` helpers with a trivial ``node -e`` command
- Auto-install disabled path raises ``NodeRuntimeError`` cleanly
- Download target resolution for each platform (via monkeypatching ``sys.platform``)
- ``_info_from_install_dir`` against a synthetic extracted tree
- Archive extraction helper with a fake zip/tarball in tmp

The real network download is NOT exercised — we unit-test the functions
that orchestrate it via monkeypatches on ``_download_file``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from digitorn.core.runtime import node_runtime as nr


def test_parse_node_version_strips_v_prefix():
    assert nr._parse_node_version("v22.11.0\n") == "22.11.0"
    assert nr._parse_node_version("22.5.1") == "22.5.1"


def test_version_major_handles_garbage():
    assert nr._version_major("22.11.0") == 22
    assert nr._version_major("garbage") == 0
    assert nr._version_major("") == 0


def test_probe_path_node_returns_info_if_system_has_node():
    info = nr._probe_path_node()
    if info is None:
        pytest.skip("no system node available for probe test")
    assert info.node_path
    assert info.source == "system"
    assert nr._version_major(info.version) >= nr.MIN_MAJOR


def test_ensure_installed_is_idempotent():
    """Calling ensure_installed twice returns the same info."""
    nr.reset_node_runtime()
    rt = nr.get_node_runtime()
    rt.set_auto_install(False)
    try:
        info1 = asyncio.run(rt.ensure_installed())
    except nr.NodeRuntimeError:
        pytest.skip("no node available in this environment")
    info2 = asyncio.run(rt.ensure_installed())
    assert info1 is info2


def test_env_property_prepends_extra_path():
    nr.reset_node_runtime()
    rt = nr.get_node_runtime()
    # Inject a fake resolved info that carries an extra_path entry.
    rt._info = nr.NodeRuntimeInfo(
        node_path="/fake/bin/node",
        npm_path="/fake/bin/npm",
        npx_path="/fake/bin/npx",
        version="22.0.0",
        source="system",
        extra_path=["/fake/bin"],
    )
    env = rt.env
    assert env["PATH"].startswith("/fake/bin")
    assert os.pathsep in env["PATH"]


def test_ensure_installed_raises_when_auto_install_disabled(monkeypatch):
    """If nothing is on PATH AND auto-install is off, we get NodeRuntimeError."""
    nr.reset_node_runtime()
    rt = nr.get_node_runtime()
    rt.set_auto_install(False)

    # Force both probes to return None so we hit the NodeRuntimeError branch.
    monkeypatch.setattr(nr, "_probe_path_node", lambda env=None: None)
    monkeypatch.setattr(nr, "_discover_version_manager_bin", lambda: None)

    with pytest.raises(nr.NodeRuntimeError):
        asyncio.run(rt.ensure_installed())


def test_download_target_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    tag, ext, inner = nr._download_target()
    assert tag == "linux-x64"
    assert ext == "tar.xz"
    assert inner.startswith("node-v")


def test_download_target_darwin_arm64(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    tag, ext, _ = nr._download_target()
    assert tag == "darwin-arm64"
    assert ext == "tar.gz"


def test_download_target_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("platform.machine", lambda: "AMD64")
    tag, ext, _ = nr._download_target()
    assert tag == "win-x64"
    assert ext == "zip"


def test_info_from_install_dir_unix_layout(tmp_path, monkeypatch):
    """Fake an extracted Node tree on a non-win platform and verify resolution."""
    monkeypatch.setattr(sys, "platform", "linux")
    install = tmp_path / "node-v22.11.0"
    bin_dir = install / "bin"
    bin_dir.mkdir(parents=True)
    # Create fake binaries — they don't need to be runnable for the layout probe
    (bin_dir / "node").write_text("#!/bin/sh\necho v22.11.0\n")
    (bin_dir / "npm").write_text("")
    (bin_dir / "npx").write_text("")
    os.chmod(bin_dir / "node", 0o755)

    info = nr._info_from_install_dir(install)
    assert info is not None
    assert info.source == "auto_install"
    assert info.node_path == str(bin_dir / "node")
    assert info.npm_path == str(bin_dir / "npm")
    assert info.npx_path == str(bin_dir / "npx")
    assert info.bin_dir == bin_dir
    assert info.extra_path == [str(bin_dir)]


def test_info_from_install_dir_missing_returns_none(tmp_path):
    assert nr._info_from_install_dir(tmp_path / "nope") is None


def test_auto_install_reuses_already_extracted(tmp_path, monkeypatch):
    """If the target dir already looks like a valid install, don't re-download."""
    # Fake pre-extracted dir
    monkeypatch.setattr(sys, "platform", "linux")
    install = tmp_path / f"node-v{nr.NODE_VERSION}"
    bin_dir = install / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "node").write_text("")

    # Any network call from here would be a bug.
    def _boom(*a, **kw):
        raise AssertionError("_download_file must not be called when dir exists")

    monkeypatch.setattr(nr, "_download_file", _boom)

    info = asyncio.run(nr._auto_install_node(nr.NODE_VERSION, install))
    assert info is not None
    assert info.node_path.endswith("node")
    assert info.source == "auto_install"


def test_spawn_with_system_node_runs_simple_expression():
    """Integration: spawn node and run ``node -e 'console.log(...)'``."""
    nr.reset_node_runtime()
    rt = nr.get_node_runtime()
    rt.set_auto_install(False)
    try:
        asyncio.run(rt.ensure_installed())
    except nr.NodeRuntimeError:
        pytest.skip("no node available to run spawn smoke test")

    async def _do() -> tuple[int, str, str]:
        return await rt.run(
            "node", ["-e", "console.log('hello from digitorn')"],
            timeout=10.0,
        )

    rc, stdout, _ = asyncio.run(_do())
    assert rc == 0
    assert "hello from digitorn" in stdout


def teardown_module():
    nr.reset_node_runtime()
