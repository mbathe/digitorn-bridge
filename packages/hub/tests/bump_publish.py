"""Tiny helper: bump chess-coach to 1.0.2 + republish."""
from __future__ import annotations

import io
import re
import sys
import tarfile
from pathlib import Path

import httpx

HUB = "https://hub.digitorn.ai"
APPS = Path(__file__).resolve().parents[3] / "apps" / "digitorn-official"
PUBLISHER = "digitorn-official"
PKG = "chess-coach"
NEW_VERSION = "1.0.2"


def main() -> int:
    pkg_dir = APPS / PKG
    toml_path = pkg_dir / "package.toml"

    src = toml_path.read_text(encoding="utf-8")
    src = re.sub(r'^version\s*=\s*"[^"]+"', f'version = "{NEW_VERSION}"', src, count=1, flags=re.M)
    toml_path.write_text(src, encoding="utf-8")
    print(f"  patched -> v{NEW_VERSION}")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path in sorted(pkg_dir.rglob("*")):
            if not path.is_file():
                continue
            tf.add(path, arcname=path.relative_to(pkg_dir).as_posix())
    archive = buf.getvalue()
    print(f"  archive: {len(archive)} B")

    r = httpx.post(
        f"{HUB}/api/v1/auth/login",
        json={"email": "paul@digitorn.io", "password": "motdepasse123"},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]

    r = httpx.post(
        f"{HUB}/api/v1/publishers/{PUBLISHER}/packages/{PKG}/versions",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (f"{PKG}-{NEW_VERSION}.tar.gz", archive, "application/gzip")},
        data={"version": NEW_VERSION},
        timeout=120,
    )
    print(f"  publish status={r.status_code}")
    if r.status_code != 201:
        print(f"  body: {r.text[:400]}")
        return 2
    print(f"  OK, v{NEW_VERSION} published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
