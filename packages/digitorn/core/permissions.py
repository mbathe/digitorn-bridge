"""Digitorn — System permission catalog.

Every system-level capability that a module action can use MUST be declared
as a permission from this catalog.  This is the Digitorn equivalent of
Android's ``<uses-permission>`` manifest declarations.

Permissions are organised by domain::

    fs.*          Filesystem operations
    process.*     Process management
    net.*         Network operations
    db.*          Database operations
    sys.*         System administration
    hw.*          Hardware / peripherals
    crypto.*      Cryptographic keys
    secret.*      Secrets / credentials
    user.*        User data
    clipboard.*   Clipboard access
    screen.*      Screen capture / control
    browser.*     Browser automation
    ai.*          External AI/LLM calls
    container.*   Container management
    ipc.*         Inter-process communication
    log.*         System log access
    cron.*        Scheduled task management
    notify.*      Notifications

Enforcement layers:
    1. Declaration — module TOML ``permissions = [...]`` + ``@action(permissions=[...])``
    2. Install-time — action permissions ⊆ module permissions (future installer)
    3. Runtime — ``security_gate()`` checks app's ``granted_permissions``

Custom permissions:
    Third-party modules may declare custom permissions prefixed with their
    module_id, e.g. ``"mymodule:cloud_sync"``.  These are NOT in this catalog
    and are NOT validated against it — only system permissions are enforced.
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    """System-level permission catalog.

    Each value is a dot-separated domain.capability string.
    Modules declare these in their ``@action(permissions=[...])`` and in
    their TOML manifest ``permissions = [...]``.
    """

    FS_READ       = "fs.read"
    FS_WRITE      = "fs.write"
    FS_DELETE     = "fs.delete"
    FS_LIST       = "fs.list"
    FS_PERMISSION = "fs.permission"

    PROCESS_EXEC         = "process.exec"
    PROCESS_KILL         = "process.kill"
    PROCESS_INSPECT      = "process.inspect"
    PROCESS_SPAWN_DAEMON = "process.spawn_daemon"

    NET_HTTP     = "net.http"
    NET_SOCKET   = "net.socket"
    NET_LISTEN   = "net.listen"
    NET_FIREWALL = "net.firewall"
    NET_DNS      = "net.dns"

    DB_READ   = "db.read"
    DB_WRITE  = "db.write"
    DB_SCHEMA = "db.schema"

    SYS_ENV      = "sys.env"
    SYS_INFO     = "sys.info"
    SYS_CONFIG   = "sys.config"
    SYS_POWER    = "sys.power"
    SYS_SERVICE  = "sys.service"
    SYS_PACKAGE  = "sys.package"
    SYS_USER_MGMT = "sys.user_mgmt"
    SYS_TIME     = "sys.time"
    SYS_MOUNT    = "sys.mount"

    HW_GPIO      = "hw.gpio"
    HW_USB       = "hw.usb"
    HW_BLUETOOTH = "hw.bluetooth"
    HW_CAMERA    = "hw.camera"
    HW_AUDIO     = "hw.audio"

    CRYPTO_KEYS  = "crypto.keys"
    SECRET_READ  = "secret.read"
    SECRET_WRITE = "secret.write"

    USER_READ  = "user.read"
    USER_WRITE = "user.write"

    CLIPBOARD_READ  = "clipboard.read"
    CLIPBOARD_WRITE = "clipboard.write"

    SCREEN_CAPTURE = "screen.capture"
    SCREEN_CONTROL = "screen.control"

    BROWSER_AUTOMATE = "browser.automate"

    AI_CALL = "ai.call"

    CONTAINER_MANAGE = "container.manage"

    IPC_SHARED_MEM = "ipc.shared_mem"

    LOG_READ = "log.read"

    CRON_MANAGE = "cron.manage"

    NOTIFY_SEND     = "notify.send"
    NOTIFY_EXTERNAL = "notify.external"


# Lookup sets (built once at import time)

# All valid system permission strings.
SYSTEM_PERMISSIONS: frozenset[str] = frozenset(p.value for p in Permission)

PERMISSION_DOMAINS: frozenset[str] = frozenset(
    p.value.split(".")[0] for p in Permission
)


def is_system_permission(perm: str) -> bool:
    """Return True if *perm* is a known system permission."""
    return perm in SYSTEM_PERMISSIONS


def is_custom_permission(perm: str) -> bool:
    """Return True if *perm* looks like a custom (non-system) permission.

    Custom permissions use ``:`` as separator (e.g. ``mymodule:cloud_sync``).
    System permissions use ``.`` (e.g. ``fs.read``).
    """
    return ":" in perm


def validate_permissions(permissions: list[str]) -> list[str]:
    """Validate a list of permission strings.

    Returns a list of invalid permissions (empty = all valid).
    System permissions must exist in the catalog.
    Custom permissions (containing ``:``) are always accepted.
    """
    invalid: list[str] = []
    for perm in permissions:
        if is_custom_permission(perm):
            continue  
        if not is_system_permission(perm):
            invalid.append(perm)
    return invalid


def validate_action_permissions(
    action_permissions: list[str],
    module_permissions: list[str],
) -> list[str]:
    """Check that every action permission is declared at the module level.

    Returns a list of undeclared permissions (empty = all declared).
    This is the install-time check: action perms ⊆ module perms.
    """
    module_set = set(module_permissions)
    return [p for p in action_permissions if p not in module_set]
