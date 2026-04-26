"""Live end-to-end test of the icon pipeline (option C: Hub-served).

Exercises the full flow against the real OCI bucket + Neon DB +
local Hub server:

  1. parse_and_validate extracts icon bytes from a tar.
  2. _maybe_push_icon stores the bytes in the PRIVATE S3 prefix and
     returns ``(hub_url, ext)``.
  3. The icon route GET ``/api/v1/packages/{pub}/{pkg}/icon`` reads
     them back, anonymously, with the right Content-Type + cache.
  4. Cleanup: deletes the test object from S3.

Pre-req: a local Hub server is running and listening on ``HUB_URL``
(defaults to ``http://127.0.0.1:8001``). Start it with::

  cd packages/hub && py -3.12 -m digitorn_hub
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tarfile

import httpx

# Load .env BEFORE importing hub modules so settings see OCI keys.
_ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.exists(_ENV_PATH):
    for line in open(_ENV_PATH, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)

from digitorn_hub.archive import parse_and_validate  # noqa: E402
from digitorn_hub.routers.packages import _maybe_push_icon  # noqa: E402
from digitorn_hub.storage.s3 import get_storage  # noqa: E402

HUB_URL = os.environ.get("HUB_URL", "http://127.0.0.1:8001")
PUB = "_icon_test_pub"
PKG = "icon-test-pkg"

_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\x5d\xcc\x14\x0c\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_archive() -> bytes:
    pkg_toml = (
        f'[package]\n'
        f'id = "{PKG}"\n'
        f'name = "Icon Test"\n'
        f'version = "0.0.1"\n'
        f'description = "Live test, safe to delete."\n'
        f'category = "developer-tools"\n'
        f'icon = "icon.png"\n\n'
        f'[package.permissions]\n'
        f'risk_level = "low"\n\n'
        f'[package.hub]\n'
        f'tags = ["test"]\n'
    ).encode()
    app_yaml = f'app:\n  app_id: {PKG}\n  name: Icon Test\n'.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in [
            ("package.toml", pkg_toml),
            ("app.yaml", app_yaml),
            ("icon.png", _PNG_1X1),
        ]:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _expect(cond: bool, label: str) -> None:
    if not cond:
        print(f"  [FAIL] {label}")
        sys.exit(2)
    print(f"  [OK]   {label}")


async def main() -> int:
    from digitorn_hub.db import session_scope
    from digitorn_hub.models import Package, Publisher
    from sqlalchemy import select, delete

    print("[0] borrow an existing publisher slug (avoids FK gymnastics)")
    async with session_scope() as s:
        pub_row = (await s.execute(select(Publisher).limit(1))).scalar_one_or_none()
        if pub_row is None:
            print("  [SKIP] no publisher in DB to attach test package")
            return 0
        pub_slug = pub_row.slug
        pub_id = pub_row.id
    print(f"         using publisher slug={pub_slug}")

    print("[1] parse archive + extract icon bytes")
    parsed = parse_and_validate(_make_archive())
    _expect(parsed.icon_url is None, "icon_url None for relative path")
    _expect(parsed.icon_bytes == _PNG_1X1, "bytes round-trip")
    _expect(parsed.icon_content_type == "image/png", "content-type detected")

    print("[2] _maybe_push_icon -> private S3 + (hub_url, ext)")
    storage = get_storage()
    pushed = await _maybe_push_icon(pub_slug, PKG, parsed, storage)
    _expect(pushed is not None, "push succeeded")
    hub_url, ext = pushed
    _expect(ext == "png", "ext is png")
    print(f"         hub_url = {hub_url}")
    s3_key = f"icons/{pub_slug}/{PKG}.{ext}"
    _expect(await storage.object_exists(s3_key), f"S3 object exists at {s3_key}")

    print("[3] GET /icon route via local Hub")
    async with session_scope() as s:
        await s.execute(delete(Package).where(
            Package.publisher_id == pub_id, Package.package_id == PKG
        ))
        s.add(Package(
            publisher_id=pub_id,
            package_id=PKG,
            name="Icon Test",
            description="live test",
            risk_level="low",
            icon_url=hub_url,
            icon_storage_ext=ext,
        ))

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{HUB_URL}/api/v1/packages/{pub_slug}/{PKG}/icon")
    _expect(r.status_code == 200, f"GET /icon returned 200 (got {r.status_code})")
    _expect(r.content == _PNG_1X1, "served bytes match uploaded bytes")
    _expect(
        r.headers.get("content-type", "").startswith("image/png"),
        "Content-Type is image/png",
    )
    _expect(
        "max-age" in r.headers.get("cache-control", ""),
        "Cache-Control set",
    )

    print("[4] cleanup (delete test object + DB row)")
    try:
        await storage.delete_object(s3_key)
        async with session_scope() as s:
            await s.execute(delete(Package).where(
                Package.publisher_id == pub_id, Package.package_id == PKG
            ))
        print("  [OK]   cleaned")
    except Exception as exc:
        print(f"  [warn] cleanup failed (manual delete needed): {exc}")

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
