"""Live republish of chess-coach + smart-rss-digest with real icons.

End-to-end exercise:
  1. Generate a 256x256 PNG icon (themed colour + emoji as text overlay)
     for each package.
  2. Write the icon next to package.toml.
  3. Patch package.toml: bump version, swap `icon = "<emoji>"` for
     `icon = "icon.png"`.
  4. Build the .tar.gz archive (same shape the CLI would).
  5. Login to the local Hub as paul@digitorn.io and POST the archive
     to the publish endpoint.
  6. Verify GET /api/v1/packages/digitorn-official/{pkg}/icon returns
     the PNG bytes.

Run with the local Hub already started on http://127.0.0.1:8001.
"""
from __future__ import annotations

import io
import os
import re
import sys
import tarfile
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

HUB = "http://127.0.0.1:8001"
APPS_DIR = Path(__file__).resolve().parents[3] / "apps" / "digitorn-official"
PUBLISHER = "digitorn-official"

PACKAGES = [
    {
        "id": "chess-coach",
        "emoji_was": "♟️",
        "icon_text": "♟",
        "bg": (47, 79, 79),    # dark slate gray
        "fg": (240, 230, 200), # ivory
    },
    {
        "id": "smart-rss-digest",
        "emoji_was": "📰",
        "icon_text": "📰",
        "bg": (200, 60, 60),   # red
        "fg": (250, 250, 250), # white
    },
]


def _expect(cond: bool, label: str) -> None:
    if not cond:
        print(f"  [FAIL] {label}")
        sys.exit(2)
    print(f"  [OK]   {label}")


def _bump_patch(version: str) -> str:
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not semver: {version}")
    major, minor, patch = parts
    return f"{major}.{minor}.{int(patch) + 1}"


def _make_icon(text: str, bg: tuple, fg: tuple) -> bytes:
    """A 256x256 PNG with the given background and a centred glyph.

    Falls back to the package's first letter when the emoji can't be
    rendered (no system font for it)."""
    size = 256
    img = Image.new("RGB", (size, size), bg)
    d = ImageDraw.Draw(img)
    # Try the largest readily-available font; emojis often work on
    # Windows with seguiemj.ttf. If that fails, fall back to the
    # default bitmap font + the first letter of `text`.
    glyph = text
    font = None
    for path, sz in [
        ("C:/Windows/Fonts/seguiemj.ttf", 160),
        ("C:/Windows/Fonts/seguisb.ttf", 160),
        ("C:/Windows/Fonts/arial.ttf", 160),
    ]:
        try:
            font = ImageFont.truetype(path, sz)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
        glyph = (text or "?")[0].upper()

    # Center the glyph
    bbox = d.textbbox((0, 0), glyph, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - w) / 2 - bbox[0]
    y = (size - h) / 2 - bbox[1]
    d.text((x, y), glyph, fill=fg, font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _patch_toml(toml_path: Path, new_version: str) -> bytes:
    """Bump version + swap icon to local file. Returns the new bytes."""
    src = toml_path.read_text(encoding="utf-8")
    src = re.sub(
        r'^version\s*=\s*"[^"]+"',
        f'version = "{new_version}"',
        src,
        count=1,
        flags=re.M,
    )
    src = re.sub(
        r'^icon\s*=\s*"[^"]*"',
        'icon = "icon.png"',
        src,
        count=1,
        flags=re.M,
    )
    return src.encode("utf-8")


def _build_archive(pkg_dir: Path, patched_toml: bytes, icon_bytes: bytes) -> bytes:
    """Pack the package dir into .tar.gz, replacing package.toml + adding
    icon.png. We DON'T mutate the source dir on disk — the patched
    bytes stay in-memory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path in sorted(pkg_dir.rglob("*")):
            if not path.is_file():
                continue
            arcname = path.relative_to(pkg_dir).as_posix()
            if arcname == "package.toml":
                info = tarfile.TarInfo(name=arcname)
                info.size = len(patched_toml)
                tf.addfile(info, io.BytesIO(patched_toml))
            elif arcname == "icon.png":
                # Skip — we'll write our own.
                continue
            else:
                tf.add(path, arcname=arcname)
        # Add the icon last so we can override any stale one.
        info = tarfile.TarInfo(name="icon.png")
        info.size = len(icon_bytes)
        tf.addfile(info, io.BytesIO(icon_bytes))
    return buf.getvalue()


def main() -> int:
    print("[0] login as paul@digitorn.io")
    r = httpx.post(
        f"{HUB}/api/v1/auth/login",
        json={"email": "paul@digitorn.io", "password": "motdepasse123"},
        timeout=30,
    )
    _expect(r.status_code == 200, f"login (got {r.status_code})")
    token = r.json()["access_token"]
    print(f"         token = {token[:24]}...")

    for spec in PACKAGES:
        pid = spec["id"]
        pkg_dir = APPS_DIR / pid
        toml_path = pkg_dir / "package.toml"
        if not toml_path.exists():
            print(f"\n[skip] {pid}: not found at {pkg_dir}")
            continue

        print(f"\n=== {pid} ===")
        print("[1] generate icon.png")
        icon_bytes = _make_icon(spec["icon_text"], spec["bg"], spec["fg"])
        _expect(len(icon_bytes) > 0, f"icon generated ({len(icon_bytes)} B)")
        # Persist on disk so the user has it next to the source.
        (pkg_dir / "icon.png").write_bytes(icon_bytes)

        print("[2] patch package.toml (bump version + icon)")
        current_version = re.search(
            r'^version\s*=\s*"([^"]+)"',
            toml_path.read_text(encoding="utf-8"),
            re.M,
        ).group(1)
        new_version = _bump_patch(current_version)
        patched = _patch_toml(toml_path, new_version)
        # Persist on disk too — the user keeps the change.
        toml_path.write_bytes(patched)
        print(f"         {current_version} -> {new_version}")

        print("[3] build .tar.gz")
        archive = _build_archive(pkg_dir, patched, icon_bytes)
        print(f"         archive: {len(archive)} B")

        print("[4] POST publish")
        r = httpx.post(
            f"{HUB}/api/v1/publishers/{PUBLISHER}/packages/{pid}/versions",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (f"{pid}-{new_version}.tar.gz", archive, "application/gzip")},
            data={"version": new_version},
            timeout=120,
        )
        _expect(
            r.status_code == 201,
            f"publish (got {r.status_code}: {r.text[:200]})",
        )

        print("[5] GET /icon route")
        r = httpx.get(f"{HUB}/api/v1/packages/{PUBLISHER}/{pid}/icon", timeout=30)
        _expect(r.status_code == 200, f"icon GET (got {r.status_code})")
        _expect(r.content == icon_bytes, "served bytes match generated bytes")
        print(
            f"         content-type={r.headers.get('content-type')}, "
            f"cache={r.headers.get('cache-control')}"
        )

        print("[6] DB row sanity")
        # Hit the package detail endpoint and check icon_url
        r = httpx.get(f"{HUB}/api/v1/packages/{PUBLISHER}/{pid}", timeout=30)
        _expect(r.status_code == 200, "detail GET")
        url = r.json().get("icon_url")
        _expect(
            url is not None and "/icon" in url,
            f"icon_url points at /icon route (got {url!r})",
        )

    print("\nALL PACKAGES REPUBLISHED + ICON ROUTE LIVE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
