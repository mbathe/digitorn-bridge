"""Digitorn AppPackages - distributable units of installation.

Public surface for callers:

    from digitorn.core.packages import (
        PackageManifest,
        PackageRegistry,
        InstallFlow,
        Status,
        SourceType,
        BuiltinSource,
        LocalSource,
        HubSource,
        GitSource,
        compute_package_hash,
        require_install_permission,
        classify_existing_apps,
    )

See ``docs/APP_PACKAGES.md`` for the full design doc.
"""

from digitorn.core.packages.hash import (
    compute_package_hash,
    detect_drift,
    read_package_hash_file,
    write_package_hash_file,
)
from digitorn.core.packages.install import (
    IncompatibleDaemonVersion,
    InstallError,
    InstallFlow,
    InstallResult,
    PackageIdCollision,
    PermissionsRequired,
)
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
from digitorn.core.packages.manifest_generator import (
    generate_package_manifest,
)
from digitorn.core.packages.migration import classify_existing_apps
from digitorn.core.packages.permissions import (
    PACKAGE_INSTALL_CAPABILITY,
    has_install_permission,
    require_install_permission,
)
from digitorn.core.packages.registry import (
    PackageNotFound,
    PackageRegistry,
    SourceType,
    Status,
)
from digitorn.core.packages.source import (
    AvailablePackage,
    FetchError,
    PackageSource,
)
from digitorn.core.packages.sources import (
    BuiltinSource,
    GitSource,
    HubSource,
    LocalSource,
)

__all__ = [
    # Manifest
    "PackageManifest",
    "PackageMeta",
    "PackageSourceMeta",
    "PackageCompatibility",
    "PackageRequirements",
    "PackageCredentials",
    "PackagePermissions",
    "PackageHubMeta",
    "PackageRelease",
    "generate_package_manifest",
    # Hash
    "compute_package_hash",
    "detect_drift",
    "read_package_hash_file",
    "write_package_hash_file",
    # Registry
    "PackageRegistry",
    "Status",
    "SourceType",
    "PackageNotFound",
    # Install flow
    "InstallFlow",
    "InstallResult",
    "InstallError",
    "PermissionsRequired",
    "PackageIdCollision",
    "IncompatibleDaemonVersion",
    # Sources
    "PackageSource",
    "AvailablePackage",
    "FetchError",
    "BuiltinSource",
    "LocalSource",
    "HubSource",
    "GitSource",
    # Permissions
    "PACKAGE_INSTALL_CAPABILITY",
    "has_install_permission",
    "require_install_permission",
    # Migration
    "classify_existing_apps",
]
