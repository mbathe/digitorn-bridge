from __future__ import annotations

import hashlib
import io
import re
import tarfile
import tomllib
from dataclasses import dataclass
from typing import Any

from . import versioning

_PACKAGE_TOML = "package.toml"
_APP_YAML = "app.yaml"
_PACKAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$")
_RISK_LEVELS = {"low", "medium", "high"}

# Cap on the icon bytes we'll extract from an archive — protects the
# Hub from a misbehaving publisher uploading a 50 MiB "icon.png".
_MAX_ICON_BYTES = 256 * 1024  # 256 KiB

_ICON_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class ArchiveError(ValueError):
    """Raised when an uploaded archive is malformed or fails validation."""


@dataclass(frozen=True)
class ParsedArchive:
    sha256: str
    size: int
    package_id: str
    name: str
    version: str
    description: str
    category: str | None
    risk_level: str
    # When package.toml's `icon` is an absolute URL (http/https), it
    # lands here as-is. When it's a relative filename inside the
    # archive, this is None and `icon_bytes` carries the extracted
    # file. The publish handler decides what to do (push to S3 then
    # set icon_url) — keeping the two split keeps `parse_and_validate`
    # pure.
    icon_url: str | None
    icon_bytes: bytes | None
    icon_content_type: str | None
    tags: list[str]
    manifest: dict[str, Any]
    has_app_yaml: bool
    file_count: int


def _normalize_member_name(name: str) -> str:
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name


def _strip_top_dir(names: list[str]) -> str:
    """If every member shares a top-level dir, return its name; else "" """
    candidates = set()
    for n in names:
        parts = n.split("/", 1)
        if len(parts) > 1 and parts[0]:
            candidates.add(parts[0])
        else:
            return ""
    if len(candidates) == 1:
        return next(iter(candidates))
    return ""


def parse_and_validate(data: bytes) -> ParsedArchive:
    """Parse a .tar.gz archive of a digitorn package, validate, return metadata.

    Validates:
      - Tar opens cleanly, no path traversal, no symlinks pointing outside
      - package.toml at root (or under single top-level dir), parseable
      - app.yaml present
      - package.toml fields well-formed
    """
    sha = hashlib.sha256(data).hexdigest()
    size = len(data)
    bio = io.BytesIO(data)
    try:
        tf = tarfile.open(fileobj=bio, mode="r:gz")
    except tarfile.TarError as exc:
        raise ArchiveError(f"not a valid tar.gz: {exc}") from exc

    members = tf.getmembers()
    if not members:
        raise ArchiveError("archive is empty")

    safe_names = []
    for m in members:
        n = _normalize_member_name(m.name)
        if not n or n.startswith("/") or ".." in n.split("/"):
            raise ArchiveError(f"unsafe path in archive: {m.name!r}")
        if m.issym() or m.islnk():
            raise ArchiveError(f"links not allowed in archive: {m.name!r}")
        safe_names.append(n)

    top = _strip_top_dir(safe_names)
    rel = lambda n: n[len(top) + 1 :] if top and n.startswith(top + "/") else n  # noqa: E731

    package_toml_path = None
    app_yaml_path = None
    file_count = 0
    for m, n in zip(members, safe_names):
        if not m.isfile():
            continue
        file_count += 1
        r = rel(n)
        if r == _PACKAGE_TOML:
            package_toml_path = m
        elif r == _APP_YAML:
            app_yaml_path = m

    if package_toml_path is None:
        raise ArchiveError("missing package.toml at archive root")
    if app_yaml_path is None:
        raise ArchiveError("missing app.yaml at archive root")

    raw = tf.extractfile(package_toml_path)
    if raw is None:
        raise ArchiveError("could not read package.toml")
    try:
        manifest = tomllib.loads(raw.read().decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ArchiveError(f"package.toml is invalid: {exc}") from exc

    pkg_section = manifest.get("package")
    if not isinstance(pkg_section, dict):
        raise ArchiveError("package.toml: missing [package] table")

    package_id = pkg_section.get("id")
    name = pkg_section.get("name")
    version = pkg_section.get("version")
    if not isinstance(package_id, str) or not _PACKAGE_ID_RE.match(package_id):
        raise ArchiveError(
            "package.toml: 'id' must be 3-64 chars, [a-z0-9_-], "
            "starting/ending with [a-z0-9]"
        )
    if not isinstance(name, str) or not name.strip():
        raise ArchiveError("package.toml: 'name' is required")
    if not isinstance(version, str) or not versioning.is_valid(version):
        raise ArchiveError("package.toml: 'version' must be semver (e.g. 1.2.3)")

    description = pkg_section.get("description") or ""
    if not isinstance(description, str):
        raise ArchiveError("package.toml: 'description' must be a string")

    category = pkg_section.get("category")
    if category is not None and not isinstance(category, str):
        raise ArchiveError("package.toml: 'category' must be a string")

    perms = manifest.get("package", {}).get("permissions", {})
    risk_level = perms.get("risk_level", "medium") if isinstance(perms, dict) else "medium"
    if risk_level not in _RISK_LEVELS:
        raise ArchiveError(
            f"package.toml: permissions.risk_level must be one of {sorted(_RISK_LEVELS)}"
        )

    hub_section = pkg_section.get("hub")
    tags_raw = hub_section.get("tags", []) if isinstance(hub_section, dict) else []
    if not isinstance(tags_raw, list) or not all(isinstance(t, str) for t in tags_raw):
        raise ArchiveError("package.toml: hub.tags must be a list of strings")
    tags = [t.strip().lower() for t in tags_raw if t.strip()]
    tags = sorted(set(tags))[:20]

    icon_raw = pkg_section.get("icon")
    if not isinstance(icon_raw, str) or not icon_raw.strip():
        icon_url, icon_bytes, icon_content_type = None, None, None
    elif icon_raw.startswith(("http://", "https://")):
        # Absolute URL — publisher is hosting the icon themselves.
        # Trust them, store as-is, no extraction.
        icon_url, icon_bytes, icon_content_type = icon_raw, None, None
    else:
        # Relative filename — must exist inside the archive. Extract
        # the bytes so the publish handler can push them to S3 and
        # mint a real public URL. We leave icon_url as None until
        # then so the DB never carries a broken value.
        icon_url, icon_bytes, icon_content_type = None, None, None
        target = icon_raw.lstrip("./").replace("\\", "/")
        for m, n in zip(members, safe_names):
            if not m.isfile():
                continue
            if rel(n) != target:
                continue
            if m.size > _MAX_ICON_BYTES:
                # Hard limit — bigger than 256 KiB is not an icon, it's
                # an attack vector. Drop it silently; the card will
                # fall back to the seeded initial avatar.
                break
            ext = "." + target.rsplit(".", 1)[-1].lower() if "." in target else ""
            ct = _ICON_CONTENT_TYPES.get(ext)
            if ct is None:
                # Unknown image type — same drop-silently policy.
                break
            extracted = tf.extractfile(m)
            if extracted is None:
                break
            data = extracted.read(_MAX_ICON_BYTES + 1)
            if len(data) > _MAX_ICON_BYTES:
                break
            icon_bytes = data
            icon_content_type = ct
            break

    return ParsedArchive(
        sha256=sha,
        size=size,
        package_id=package_id,
        name=name.strip(),
        version=version,
        description=description.strip(),
        category=category,
        risk_level=risk_level,
        icon_url=icon_url,
        icon_bytes=icon_bytes,
        icon_content_type=icon_content_type,
        tags=tags,
        manifest=manifest,
        has_app_yaml=True,
        file_count=file_count,
    )
