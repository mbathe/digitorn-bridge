from __future__ import annotations

import io
import tarfile

import pytest

from digitorn_hub.archive import ArchiveError, parse_and_validate

_VALID_PACKAGE_TOML = b"""\
[package]
id = "demo-app"
name = "Demo App"
version = "1.2.3"
description = "A demo application."
category = "developer-tools"
icon = "icon.png"

[package.permissions]
risk_level = "low"

[package.hub]
tags = ["demo", "test", "Example"]
"""

_VALID_APP_YAML = b"app:\n  app_id: demo-app\n  name: Demo App\n"


def _make_tgz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_parses_minimal_valid_archive():
    data = _make_tgz(
        {"package.toml": _VALID_PACKAGE_TOML, "app.yaml": _VALID_APP_YAML}
    )
    parsed = parse_and_validate(data)
    assert parsed.package_id == "demo-app"
    assert parsed.version == "1.2.3"
    assert parsed.name == "Demo App"
    assert parsed.risk_level == "low"
    assert parsed.tags == ["demo", "example", "test"]  # lowercased + sorted + deduped
    # `icon = "icon.png"` is a relative filename, not an absolute URL.
    # Since the fixture archive doesn't carry the icon file, both
    # icon_url and icon_bytes must be None — the publish handler will
    # fall back to the seeded initial avatar in the UI.
    assert parsed.icon_url is None
    assert parsed.icon_bytes is None
    assert parsed.icon_content_type is None
    assert parsed.size == len(data)
    assert len(parsed.sha256) == 64


def test_accepts_top_level_directory():
    data = _make_tgz(
        {
            "demo-app/package.toml": _VALID_PACKAGE_TOML,
            "demo-app/app.yaml": _VALID_APP_YAML,
        }
    )
    parsed = parse_and_validate(data)
    assert parsed.package_id == "demo-app"


def test_rejects_missing_package_toml():
    data = _make_tgz({"app.yaml": _VALID_APP_YAML})
    with pytest.raises(ArchiveError, match="package.toml"):
        parse_and_validate(data)


def test_rejects_missing_app_yaml():
    data = _make_tgz({"package.toml": _VALID_PACKAGE_TOML})
    with pytest.raises(ArchiveError, match="app.yaml"):
        parse_and_validate(data)


def test_rejects_path_traversal():
    data = _make_tgz(
        {
            "package.toml": _VALID_PACKAGE_TOML,
            "app.yaml": _VALID_APP_YAML,
            "../../etc/passwd": b"x",
        }
    )
    with pytest.raises(ArchiveError, match="unsafe path"):
        parse_and_validate(data)


def test_rejects_invalid_semver():
    bad = _VALID_PACKAGE_TOML.replace(b'version = "1.2.3"', b'version = "abc"')
    data = _make_tgz({"package.toml": bad, "app.yaml": _VALID_APP_YAML})
    with pytest.raises(ArchiveError, match="semver"):
        parse_and_validate(data)


def test_rejects_invalid_id_format():
    bad = _VALID_PACKAGE_TOML.replace(b'id = "demo-app"', b'id = "Bad_ID!"')
    data = _make_tgz({"package.toml": bad, "app.yaml": _VALID_APP_YAML})
    with pytest.raises(ArchiveError, match="id"):
        parse_and_validate(data)


def test_rejects_unknown_risk_level():
    bad = _VALID_PACKAGE_TOML.replace(b'risk_level = "low"', b'risk_level = "extreme"')
    data = _make_tgz({"package.toml": bad, "app.yaml": _VALID_APP_YAML})
    with pytest.raises(ArchiveError, match="risk_level"):
        parse_and_validate(data)


# ─── Icon extraction ────────────────────────────────────────────────


# Smallest possible "PNG" — first 8 bytes are the PNG magic, the rest
# is filler. We don't decode the image, we just check round-trip
# byte equality + content-type detection.
_PNG_1X1 = b"\x89PNG\r\n\x1a\n" + b"fakepng-bytes"


def test_absolute_icon_url_passthrough():
    pkg_with_url = _VALID_PACKAGE_TOML.replace(
        b'icon = "icon.png"', b'icon = "https://cdn.example.com/foo.png"'
    )
    data = _make_tgz({"package.toml": pkg_with_url, "app.yaml": _VALID_APP_YAML})
    parsed = parse_and_validate(data)
    assert parsed.icon_url == "https://cdn.example.com/foo.png"
    assert parsed.icon_bytes is None
    assert parsed.icon_content_type is None


def test_relative_icon_extracted_to_bytes():
    data = _make_tgz({
        "package.toml": _VALID_PACKAGE_TOML,
        "app.yaml": _VALID_APP_YAML,
        "icon.png": _PNG_1X1,
    })
    parsed = parse_and_validate(data)
    # icon_url must NOT carry the relative filename — that's the bug
    # we're fixing.
    assert parsed.icon_url is None
    assert parsed.icon_bytes == _PNG_1X1
    assert parsed.icon_content_type == "image/png"


def test_relative_icon_extracted_under_top_dir():
    data = _make_tgz({
        "demo-app/package.toml": _VALID_PACKAGE_TOML,
        "demo-app/app.yaml": _VALID_APP_YAML,
        "demo-app/icon.png": _PNG_1X1,
    })
    parsed = parse_and_validate(data)
    assert parsed.icon_url is None
    assert parsed.icon_bytes == _PNG_1X1
    assert parsed.icon_content_type == "image/png"


def test_unknown_icon_extension_silently_dropped():
    bad = _VALID_PACKAGE_TOML.replace(b'icon = "icon.png"', b'icon = "icon.exe"')
    data = _make_tgz({
        "package.toml": bad,
        "app.yaml": _VALID_APP_YAML,
        "icon.exe": _PNG_1X1,  # exe is not in _ICON_CONTENT_TYPES
    })
    parsed = parse_and_validate(data)
    assert parsed.icon_url is None
    assert parsed.icon_bytes is None


def test_oversized_icon_silently_dropped():
    huge = b"x" * (300 * 1024)  # 300 KiB > _MAX_ICON_BYTES (256)
    data = _make_tgz({
        "package.toml": _VALID_PACKAGE_TOML,
        "app.yaml": _VALID_APP_YAML,
        "icon.png": huge,
    })
    parsed = parse_and_validate(data)
    assert parsed.icon_url is None
    assert parsed.icon_bytes is None
