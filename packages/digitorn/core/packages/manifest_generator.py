"""Auto-generate ``package.toml`` from a compiled Digitorn app.

Per the locked design (D5 + §5 of APP_PACKAGES.md), ``package.toml``
is **mandatory** for every installed package - but writing it by
hand is friction we want to avoid. This module is the generator
that turns any valid ``CompiledApp`` into a fully-formed
``PackageManifest``.

Three callers use it:

1. **The CLI**: ``digitorn package init <path>`` scaffolds a
   ``package.toml`` next to an existing ``app.yaml`` so the user can
   review + commit it.
2. **The builder agent**: when a user finishes building an app via
   the conversational builder, the builder calls this generator
   and shows the result via ``ask_user(content=toml)`` for review.
3. **The discovery API**: ``POST /api/discovery/generate-package-manifest``
   takes a YAML and returns the rendered TOML so the Flutter
   client can show a "package this app" button.

The generator is **best-effort**: every field that can be inferred
is inferred. Fields the source YAML doesn't carry (license,
homepage, hub tags, etc.) are left empty so the user can fill them
in if they want to publish.
"""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.packages.manifest import (
    PackageCompatibility,
    PackageCredentials,
    PackageHubMeta,
    PackageManifest,
    PackageMeta,
    PackagePermissions,
    PackageRelease,
    PackageRequirements,
    PackageSourceMeta,
)

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Risk inference rules
# ────────────────────────────────────────────────────────────────────
#
# A package's risk_level is computed from the actions it grants in
# capabilities.grant. The lists below are FQN matches against the
# action names a Digitorn app can ask for.

_HIGH_RISK_ACTIONS: frozenset[str] = frozenset({
    "shell.bash",
    "shell.bash_background",
    "filesystem.rm",
    "filesystem.move",
    "filesystem.delete",
    "database.sql",         # arbitrary SQL = arbitrary destructive ops
    "database.execute",
    "agent_spawn.spawn_agent",  # spawning sub-agents = recursive complexity
})

_MEDIUM_RISK_ACTIONS: frozenset[str] = frozenset({
    "filesystem.write",
    "filesystem.edit",
    "filesystem.mkdir",
    "web.fetch",
    "web.download",
    "http.post",
    "http.put",
    "http.delete",
})

# Modules whose mere presence implies network access. We don't
# inspect every grant - most apps that use ``web`` or ``http`` use
# them for networking.
_NETWORK_MODULES: frozenset[str] = frozenset({
    "web",
    "http",
    "channels",   # telegram, slack, email, webhook
    "rag",        # if ingesting from URLs or hub
})

_FILESYSTEM_WRITE_ACTIONS: frozenset[str] = frozenset({
    "filesystem.write",
    "filesystem.edit",
    "filesystem.mkdir",
    "filesystem.rm",
    "filesystem.move",
    "filesystem.append",
})

_FILESYSTEM_READ_ACTIONS: frozenset[str] = frozenset({
    "filesystem.read",
    "filesystem.ls",
    "filesystem.glob",
    "filesystem.grep",
    "filesystem.file_stat",
})


# ────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────


def generate_package_manifest(
    compiled: Any,
    *,
    source_type: str = "local",
    publisher: str = "",
    license: str = "",
    homepage: str = "",
) -> PackageManifest:
    """Build a ``PackageManifest`` from a ``CompiledApp``.

    Every field that can be inferred from the compile result is
    populated. Optional metadata that requires human input
    (license, homepage, hub tags) is left empty unless the caller
    passes it in.

    The returned manifest is **valid** in the sense that it passes
    ``PackageManifest`` Pydantic validation - meaning regex checks
    on the id, semver checks on the version, etc.

    Caller responsibilities:

    - The id MUST be kebab-case in the source YAML's ``app.app_id``.
      The generator does NOT auto-rename it. If the YAML has
      ``app_id: BadName_123``, the generator raises ``ValueError``.
    - If the YAML has no version, we default to ``"0.1.0"``.

    Returns the manifest. Use ``manifest.to_toml()`` to render it.
    """
    meta = compiled.meta
    execution = compiled.execution

    # Identity
    raw_version = str(getattr(meta, "version", "") or "0.1.0")
    package_meta = PackageMeta(
        id=meta.app_id,
        name=meta.name or meta.app_id,
        version=_normalise_semver(raw_version),
        description=getattr(meta, "description", "") or "",
        author=getattr(meta, "author", "") or "",
        license=license,
        homepage=homepage,
        icon=getattr(meta, "icon", "") or "",
        category=getattr(meta, "category", "") or "other",
    )

    # Source
    source = PackageSourceMeta(
        type=source_type,
        verified=False,
        publisher=publisher,
    )

    # Compatibility - we don't have version range info from the YAML,
    # so leave everything blank by default. The caller can override.
    compatibility = PackageCompatibility()

    # Requirements: which modules does the app load?
    modules_in_app = list((getattr(compiled, "modules", {}) or {}).keys())
    # Filter out always-loaded plumbing modules
    silent = {"llm_provider", "index"}
    user_modules = sorted(m for m in modules_in_app if m not in silent)

    requirements = PackageRequirements(
        modules=user_modules,
    )

    # Credentials: pulled directly from credentials_schema
    cred_schema = getattr(execution, "credentials_schema", None) or {}
    cred_providers = cred_schema.get("providers", []) or []
    required_creds = [
        p["name"] for p in cred_providers if p.get("required", True)
    ]
    optional_creds = [
        p["name"] for p in cred_providers if not p.get("required", True)
    ]
    credentials = PackageCredentials(
        required=required_creds,
        optional=optional_creds,
    )

    # Permissions inferred from capabilities.grant
    granted_actions = _collect_granted_actions(compiled)
    permissions = _infer_permissions(granted_actions, modules_in_app)

    # Hub metadata - empty unless the user fills it in
    hub = PackageHubMeta(
        tags=list(getattr(meta, "tags", []) or []),
    )

    # Release metadata
    release = PackageRelease(
        released_at="",
        release_notes="",
        breaking=False,
    )

    return PackageManifest(
        package=package_meta,
        source=source,
        compatibility=compatibility,
        requirements=requirements,
        credentials=credentials,
        permissions=permissions,
        hub=hub,
        release=release,
    )


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _normalise_semver(version: str) -> str:
    """Pad shortened versions to a full semver.

    The Digitorn YAML schema accepts loose versions like ``"1.0"``
    or even ``"1"``, but ``PackageMeta`` requires full semver
    (``major.minor.patch``). Auto-pad with zeros so users don't
    have to update every legacy YAML at once.

    Examples::

        "1"      → "1.0.0"
        "1.0"    → "1.0.0"
        "1.2"    → "1.2.0"
        "1.2.3"  → "1.2.3"
        "1.2.3-beta" → "1.2.3-beta"  (already valid)
    """
    if not version:
        return "0.1.0"

    # Already valid semver?
    import re as _re
    if _re.match(r"^\d+\.\d+\.\d+(?:[-+].*)?$", version):
        return version

    # Strip pre-release / build for the count check
    base = version.split("-")[0].split("+")[0]
    parts = base.split(".")
    while len(parts) < 3:
        parts.append("0")
    parts = parts[:3]

    suffix = ""
    if "-" in version:
        suffix = "-" + version.split("-", 1)[1]
    elif "+" in version:
        suffix = "+" + version.split("+", 1)[1]

    return ".".join(parts) + suffix


def _collect_granted_actions(compiled: Any) -> set[str]:
    """Walk the compiled SecurityProfile and return granted FQN actions.

    The capabilities block in YAML compiles into a SecurityProfile
    (``compiled.security_profile``) where each module's grants live
    under ``module_grants[<module_id>].action_overrides`` as a
    ``{action_name: policy}`` dict. ``policy`` can be ``"auto"``,
    ``"approve"``, or ``"block"``. We collect every action that is
    NOT blocked.

    Falls back to the older shapes (compiled.modules[X].actions,
    compiled.capabilities, compiled.hidden_actions) for safety in
    case some app uses a different compile path.
    """
    granted: set[str] = set()

    # Primary path: SecurityProfile.module_grants[X].action_overrides
    profile = getattr(compiled, "security_profile", None)
    if profile is not None:
        module_grants = getattr(profile, "module_grants", {}) or {}
        for module_id, grant in module_grants.items():
            overrides = getattr(grant, "action_overrides", {}) or {}
            for action_name, policy in overrides.items():
                if policy != "block":
                    granted.add(f"{module_id}.{action_name}")
            # Hidden actions are also "granted" - they're in the
            # frozenset and policy-checked the same way.
            hidden = getattr(grant, "hidden_actions", None) or frozenset()
            for action_name in hidden:
                granted.add(f"{module_id}.{action_name}")

    # Fallback paths - these are rarely populated when the security
    # profile path works, but kept for safety.
    modules = getattr(compiled, "modules", {}) or {}
    for module_id, mod_cfg in modules.items():
        actions = getattr(mod_cfg, "actions", None)
        if actions:
            for a in actions:
                granted.add(f"{module_id}.{a}")
        grant = getattr(mod_cfg, "grant", None)
        if grant:
            for a in grant:
                granted.add(f"{module_id}.{a}")

    # hidden_actions on the CompiledApp itself
    hidden_top = getattr(compiled, "hidden_actions", None) or []
    for h in hidden_top:
        if isinstance(h, dict):
            mod = h.get("module", "")
            for a in h.get("actions", []) or []:
                if mod and a:
                    granted.add(f"{mod}.{a}")

    return granted


def _infer_permissions(
    granted_actions: set[str],
    modules_in_app: list[str],
) -> PackagePermissions:
    """Compute the permission summary from the action set.

    Rules:

    - ``risk_level``: ``high`` if any high-risk action is granted,
      ``medium`` if any medium-risk action is granted, otherwise ``low``.
    - ``network_access``: True if any of the network modules is loaded.
    - ``filesystem_access``: ``["read"]`` and/or ``["write"]`` based
      on whether the matching action sets are granted.
    - ``filesystem_scopes``: empty by default (user fills in later).
    - ``requires_approval``: every high-risk action lands here so the
      user is prompted before each call.
    """
    high = granted_actions & _HIGH_RISK_ACTIONS
    medium = granted_actions & _MEDIUM_RISK_ACTIONS

    if high:
        risk_level = "high"
    elif medium:
        risk_level = "medium"
    else:
        risk_level = "low"

    network_access = any(m in modules_in_app for m in _NETWORK_MODULES)

    fs_access: list[str] = []
    if granted_actions & _FILESYSTEM_READ_ACTIONS:
        fs_access.append("read")
    if granted_actions & _FILESYSTEM_WRITE_ACTIONS:
        fs_access.append("write")

    requires_approval = sorted(high)

    return PackagePermissions(
        risk_level=risk_level,
        network_access=network_access,
        filesystem_access=fs_access,
        filesystem_scopes=[],
        requires_approval=requires_approval,
    )
