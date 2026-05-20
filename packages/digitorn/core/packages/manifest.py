"""PackageManifest - Pydantic model for `package.toml`."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_KEBAB_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
_VERSION_REQ_RE = re.compile(
    r"^(>=|<=|>|<|==|~=|!=)?\s*"
    r"(\d+(?:\.\d+){0,2}(?:[-+][\w.]+)?)$"
)

def _validate_kebab(value: str) -> str:
    if not _KEBAB_RE.match(value):
        raise ValueError(
            f"must be kebab-case, 3-64 chars, start with a letter "
            f"(got: {value!r})"
        )
    return value

def _validate_semver(value: str) -> str:
    if not _SEMVER_RE.match(value):
        raise ValueError(
            f"must be a valid semver (major.minor.patch), got: {value!r}"
        )
    return value

def _validate_version_requirement(value: str) -> str:
    if not value:
        return value
    if not _VERSION_REQ_RE.match(value.strip()):
        raise ValueError(
            f"must be a version requirement like '>=2.0.0', got: {value!r}"
        )
    return value

class PackageMeta(BaseModel):
    """`[package]` section - identity + display info."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(..., description="Globally unique package id (kebab-case).")
    name: str = Field(..., description="Human-readable display name.")
    version: str = Field(..., description="Semver version string.")
    description: str = Field(
        default="", description="One-paragraph description shown in the marketplace.",
    )
    author: str = Field(default="", description="Publisher / author name.")
    license: str = Field(
        default="", description="SPDX license identifier (required for hub).",
    )
    homepage: str = Field(default="", description="Optional external link.")
    icon: str = Field(
        default="",
        description="Relative path to an icon file in the package dir.",
    )
    category: str = Field(
        default="other",
        description=(
            "Marketplace category: productivity / developer-tools / "
            "assistant / research / creative / data / communication / other"
        ),
    )

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        return _validate_kebab(v)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        return _validate_semver(v)

class PackageSourceMeta(BaseModel):
    """`[package.source]` - where this package came."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(
        default="local",
        description="official | community | local | private",
    )
    verified: bool = Field(
        default=False,
        description="Set to true after signature check on hub packages.",
    )
    publisher: str = Field(default="", description="Publisher id on the hub.")

class PackageCompatibility(BaseModel):
    """`[package.compatibility]` - daemon + python version ranges."""

    model_config = ConfigDict(extra="allow")

    digitorn_min: str = Field(
        default="",
        description="Minimum daemon version (e.g. '>=2.0.0').",
    )
    digitorn_max: str = Field(
        default="",
        description="Maximum daemon version (e.g. '<3.0.0').",
    )
    python_min: str = Field(default="")
    platforms: list[str] = Field(
        default_factory=list,
        description="Allowed platforms: linux / darwin / win32. Empty = all.",
    )

    @field_validator("digitorn_min", "digitorn_max", "python_min")
    @classmethod
    def _validate_req(cls, v: str) -> str:
        return _validate_version_requirement(v)

class PackageRequirements(BaseModel):
    """`[package.requirements]` - runtime dependencies (advisory)."""

    model_config = ConfigDict(extra="allow")

    modules: list[str] = Field(
        default_factory=list,
        description="Digitorn modules the app needs at runtime.",
    )
    recommended_models: list[str] = Field(
        default_factory=list,
        description="LLM model ids the app works best with.",
    )
    min_disk_mb: int = Field(default=0, ge=0)
    min_memory_mb: int = Field(default=0, ge=0)
    external_tools: list[str] = Field(
        default_factory=list,
        description="Binaries expected on PATH (git, docker, npm, ...).",
    )

class PackageCredentials(BaseModel):
    """`[package.credentials]` - links to credentials_schema in app.yaml."""

    model_config = ConfigDict(extra="allow")

    required: list[str] = Field(
        default_factory=list,
        description="Provider names that must be configured before the app runs.",
    )
    optional: list[str] = Field(
        default_factory=list,
        description="Provider names that enhance the app but aren't required.",
    )

class PackagePermissions(BaseModel):
    """`[package.permissions]` - install-time consent dialog data."""

    model_config = ConfigDict(extra="allow")

    risk_level: str = Field(
        default="low",
        description="low | medium | high - computed from capabilities.grant",
    )
    network_access: bool = Field(default=False)
    filesystem_access: list[str] = Field(
        default_factory=list,
        description="['read'], ['write'], or ['read','write']",
    )
    filesystem_scopes: list[str] = Field(
        default_factory=list,
        description="workspace / user_home / system / custom paths",
    )
    requires_approval: list[str] = Field(
        default_factory=list,
        description="FQN actions that need user approval per call.",
    )

    @field_validator("risk_level")
    @classmethod
    def _validate_risk(cls, v: str) -> str:
        if v not in ("low", "medium", "high"):
            raise ValueError(
                f"risk_level must be one of low/medium/high, got: {v!r}"
            )
        return v

class PackageHubMeta(BaseModel):
    """`[package.hub]` - only filled when published to the hub."""

    model_config = ConfigDict(extra="allow")

    tags: list[str] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)
    demo_video: str = Field(default="")
    minimum_rating: float = Field(default=0.0, ge=0.0, le=5.0)
    downloads: int = Field(default=0, ge=0)

class PackageRelease(BaseModel):
    """`[package.release]` - changelog metadata."""

    model_config = ConfigDict(extra="allow")

    released_at: str = Field(default="")
    release_notes: str = Field(default="")
    breaking: bool = Field(default=False)
    upgrade_from: list[str] = Field(default_factory=list)

class PackageManifest(BaseModel):
    """The full `package.toml` model."""

    model_config = ConfigDict(extra="allow")

    package: PackageMeta
    source: PackageSourceMeta = Field(default_factory=PackageSourceMeta)
    compatibility: PackageCompatibility = Field(default_factory=PackageCompatibility)
    requirements: PackageRequirements = Field(default_factory=PackageRequirements)
    credentials: PackageCredentials = Field(default_factory=PackageCredentials)
    permissions: PackagePermissions = Field(default_factory=PackagePermissions)
    hub: PackageHubMeta = Field(default_factory=PackageHubMeta)
    release: PackageRelease = Field(default_factory=PackageRelease)

    @property
    def id(self) -> str:
        return self.package.id

    @property
    def name(self) -> str:
        return self.package.name

    @property
    def version(self) -> str:
        return self.package.version

    @property
    def description(self) -> str:
        return self.package.description

    @classmethod
    def from_path(cls, path: Path) -> "PackageManifest":
        """Load and validate a `package.toml` from disk."""
        if not path.is_file():
            raise FileNotFoundError(f"package.toml not found: {path}")

        try:
            import tomllib  # Python 3.11+
        except ImportError:
            import tomli as tomllib  # type: ignore

        try:
            with path.open("rb") as f:
                raw = tomllib.load(f)
        except Exception as exc:
            raise ValueError(f"failed to parse {path}: {exc}") from exc

        # Hoist nested `[package.<x>]` sections to the top level so
        # each Pydantic submodel sees its own dict.
        normalised = cls._hoist_package_sections(raw)
        try:
            return cls(**normalised)
        except Exception as exc:
            raise ValueError(
                f"package.toml validation failed at {path}: {exc}"
            ) from exc

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PackageManifest":
        normalised = cls._hoist_package_sections(raw)
        return cls(**normalised)

    @staticmethod
    def _hoist_package_sections(raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError(f"package.toml root must be a table, got {type(raw).__name__}")

        package_section = raw.get("package", {})
        if not isinstance(package_section, dict):
            raise ValueError("[package] section must be a table")

        out: dict[str, Any] = {}

        # The metadata fields stay under "package"
        meta_keys = {
            "id", "name", "version", "description", "author", "license",
            "homepage", "icon", "category",
        }
        out["package"] = {
            k: v for k, v in package_section.items() if k in meta_keys
        }

        # Sub-sections that live as nested tables under [package.X]
        for sub in (
            "source", "compatibility", "requirements", "credentials",
            "permissions", "hub", "release",
        ):
            if sub in package_section and isinstance(package_section[sub], dict):
                out[sub] = package_section[sub]

        return out

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict representation. Same shape as the model."""
        return self.model_dump(mode="json")

    def to_toml(self) -> str:
        """Render the manifest back to TOML text."""
        try:
            import tomli_w  # type: ignore
        except ImportError:
            return self._render_toml_manual()

        # tomli_w expects the nested layout (package.source as a sub-table)
        nested = {"package": dict(self.package.model_dump(mode="json"))}
        for sub in (
            "source", "compatibility", "requirements", "credentials",
            "permissions", "hub", "release",
        ):
            sub_model = getattr(self, sub)
            sub_dict = sub_model.model_dump(mode="json", exclude_defaults=True)
            if sub_dict:
                nested["package"][sub] = sub_dict
        return tomli_w.dumps(nested)

    def _render_toml_manual(self) -> str:
        lines: list[str] = []

        def _section(name: str, data: dict[str, Any]) -> None:
            data = {k: v for k, v in data.items() if v not in (None, "", [], {})}
            if not data:
                return
            lines.append(f"[{name}]")
            for k, v in data.items():
                lines.append(f"{k} = {_render_value(v)}")
            lines.append("")

        def _render_value(v: Any) -> str:
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, (int, float)):
                return str(v)
            if isinstance(v, list):
                items = ", ".join(_render_value(item) for item in v)
                return f"[{items}]"
            if isinstance(v, dict):
                items = ", ".join(f"{k} = {_render_value(val)}" for k, val in v.items())
                return f"{{ {items} }}"
            s = str(v).replace("\\", "\\\\").replace('"', '\\"')
            return f'"{s}"'

        _section("package", self.package.model_dump(mode="json"))
        for sub in (
            "source", "compatibility", "requirements", "credentials",
            "permissions", "hub", "release",
        ):
            data = getattr(self, sub).model_dump(mode="json", exclude_defaults=True)
            if data:
                _section(f"package.{sub}", data)

        return "\n".join(lines).rstrip() + "\n"
