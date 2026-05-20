"""HubSource - fetch packages from a remote Digitorn Hub."""
from __future__ import annotations

import io
import logging
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from digitorn.core.packages.manifest import PackageManifest
from digitorn.core.packages.source import (
    AvailablePackage,
    FetchError,
    PackageSource,
)

logger = logging.getLogger(__name__)

_HUB_URI_RE = re.compile(
    r"^hub://(?P<pub>[a-z0-9][a-z0-9_-]{1,62}[a-z0-9])/"
    r"(?P<pkg>[a-z0-9][a-z0-9_-]{1,62}[a-z0-9])"
    r"(?:@(?P<version>[A-Za-z0-9.\-+]+))?$"
)
_MAX_BYTES = 100 * 1024 * 1024

@dataclass(frozen=True)
class _ParsedHubUri:
    publisher: str
    package_id: str
    version: str | None

def parse_hub_uri(uri: str) -> _ParsedHubUri:
    m = _HUB_URI_RE.match(uri.strip())
    if not m:
        raise FetchError(
            f"invalid hub URI {uri!r}; "
            "expected hub://<publisher>/<package_id>[@<version>]"
        )
    return _ParsedHubUri(
        publisher=m.group("pub"),
        package_id=m.group("pkg"),
        version=m.group("version"),
    )

class HubSource(PackageSource):
    source_type = "hub"

    def __init__(
        self,
        hub_url: str | None = None,
        *,
        auth_token: str | None = None,
        verify_ssl: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._hub_url = (hub_url or "").rstrip("/")
        self._auth_token = auth_token
        self._verify_ssl = verify_ssl
        self._timeout = timeout_seconds

    def _require_url(self) -> str:
        if not self._hub_url:
            raise FetchError(
                "hub URL is not configured. Set DIGITORN_HUB__URL or hub.url "
                "in ~/.digitorn/config.yaml."
            )
        return self._hub_url

    def _client(self) -> httpx.AsyncClient:
        headers = {"User-Agent": "digitorn-daemon"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return httpx.AsyncClient(
            base_url=self._require_url(),
            timeout=self._timeout,
            verify=self._verify_ssl,
            headers=headers,
            follow_redirects=False,
        )

    async def list_available(self) -> list[AvailablePackage]:
        async with self._client() as c:
            r = await c.get("/api/v1/packages", params={"page_size": 50})
            r.raise_for_status()
            payload = r.json()
        out: list[AvailablePackage] = []
        for hit in payload.get("hits", []):
            pub = hit.get("publisher_slug", "")
            pkg_id = hit.get("package_id", "")
            ver = hit.get("latest_version", "")
            uri = f"hub://{pub}/{pkg_id}" + (f"@{ver}" if ver else "")
            out.append(
                AvailablePackage(
                    package_id=pkg_id,
                    version=ver or "0.0.0",
                    source_type=self.source_type,
                    source_uri=uri,
                    package_dir=Path(),
                    manifest={"hub": hit},
                )
            )
        return out

    async def _resolve_version(
        self, client: httpx.AsyncClient, parsed: _ParsedHubUri
    ) -> str:
        if parsed.version:
            return parsed.version
        r = await client.get(f"/api/v1/packages/{parsed.publisher}/{parsed.package_id}")
        if r.status_code == 404:
            raise FetchError(
                f"package not found on hub: {parsed.publisher}/{parsed.package_id}"
            )
        r.raise_for_status()
        body = r.json()
        latest = body.get("latest_version")
        if not latest:
            raise FetchError(
                f"package {parsed.publisher}/{parsed.package_id} has no published version"
            )
        return latest

    async def _download_archive(
        self, client: httpx.AsyncClient, parsed: _ParsedHubUri, version: str
    ) -> bytes:
        download_url = (
            f"/api/v1/packages/{parsed.publisher}/{parsed.package_id}"
            f"/versions/{version}/download"
        )
        r = await client.get(download_url)
        if r.status_code == 410:
            raise FetchError(f"version {version} has been yanked from the hub")
        if r.status_code == 404:
            raise FetchError(f"version {version} not found on hub")
        if r.status_code in (301, 302, 303, 307, 308):
            redirect = r.headers.get("location")
            if not redirect:
                raise FetchError("hub redirect with no Location header")
            async with httpx.AsyncClient(
                timeout=self._timeout, verify=self._verify_ssl
            ) as raw:
                rr = await raw.get(redirect)
                rr.raise_for_status()
                data = rr.content
        else:
            r.raise_for_status()
            data = r.content
        if len(data) > _MAX_BYTES:
            raise FetchError(
                f"hub archive exceeds {_MAX_BYTES} bytes ({len(data)} bytes)"
            )
        return data

    @staticmethod
    def _extract(data: bytes, dest: Path) -> Path:
        if dest.exists() or dest.is_symlink():
            if dest.is_symlink():
                dest.unlink()
            else:
                shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)

        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            members = tf.getmembers()
            for m in members:
                name = m.name.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    raise FetchError(f"unsafe path in hub archive: {m.name!r}")
                if m.issym() or m.islnk():
                    raise FetchError(f"links not allowed in hub archive: {m.name!r}")
            tf.extractall(dest, filter="data")

        # If the archive packs everything under one top-level dir, flatten it
        entries = list(dest.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            inner = entries[0]
            if (inner / "package.toml").is_file():
                tmp = dest.parent / (dest.name + ".unwrap")
                shutil.move(str(inner), str(tmp))
                shutil.rmtree(dest)
                shutil.move(str(tmp), str(dest))

        if not (dest / "package.toml").is_file():
            raise FetchError("extracted archive has no package.toml at root")
        return dest

    async def fetch(self, source_uri: str, dest: Path) -> Path:
        parsed = parse_hub_uri(source_uri)
        async with self._client() as c:
            version = await self._resolve_version(c, parsed)
            data = await self._download_archive(c, parsed, version)

        path = self._extract(data, dest)
        try:
            PackageManifest.from_path(path / "package.toml")
        except Exception as exc:
            raise FetchError(
                f"hub archive at {parsed.publisher}/{parsed.package_id}@{version}: "
                f"package.toml fails validation: {exc}"
            ) from exc
        logger.info(
            "hub.fetch ok publisher=%s package=%s version=%s bytes=%d",
            parsed.publisher,
            parsed.package_id,
            version,
            len(data),
        )
        return path

    async def check_update(
        self, installed_uri: str, current_hash: str
    ) -> str | None:
        try:
            parsed = parse_hub_uri(installed_uri)
        except FetchError:
            return None
        async with self._client() as c:
            try:
                latest = await self._resolve_version(
                    c, _ParsedHubUri(parsed.publisher, parsed.package_id, None)
                )
            except (FetchError, httpx.HTTPError):
                return None
        return latest
