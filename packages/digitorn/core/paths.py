"""Digitorn — Cross-platform path resolution.

Resolves module and config directories for all supported OS:
    Linux:   ~/.local/share/digitorn/modules/     (user)
             /usr/local/share/digitorn/modules/   (system)
    macOS:   ~/Library/Application Support/Digitorn/modules/  (user)
             /Library/Application Support/Digitorn/modules/   (system)
    Windows: %APPDATA%\\Digitorn\\modules\\          (user)
             %PROGRAMDATA%\\Digitorn\\modules\\      (system)

Builtin modules are resolved from the installed package itself.
"""

from __future__ import annotations

from pathlib import Path

from platformdirs import site_data_dir, user_config_dir, user_data_dir

APP_NAME = "digitorn"


def builtin_modules_dir() -> Path:
    """Return the path to builtin modules shipped with the package."""
    return Path(__file__).resolve().parent.parent / "modules"


def user_modules_dir() -> Path:
    """Return the path to user-installed modules."""
    return Path(user_data_dir(APP_NAME)) / "modules"


def system_modules_dir() -> Path:
    """Return the path to system-wide modules."""
    return Path(site_data_dir(APP_NAME)) / "modules"


def user_config() -> Path:
    """Return the path to user config directory."""
    return Path(user_config_dir(APP_NAME))


def models_dir() -> Path:
    """Return the path to cached ML models (embeddings, etc.)."""
    return Path(user_data_dir(APP_NAME)) / "models"


def mcp_servers_dir() -> Path:
    """Return the path to daemon-managed MCP server installs.

    Each server gets its own subdirectory::

        ~/.local/share/digitorn/mcp-servers/github/
            node_modules/       (npm)
            package.json
        ~/.local/share/digitorn/mcp-servers/git/
            .venv/              (pip)
    """
    return Path(user_data_dir(APP_NAME)) / "mcp-servers"


def builtin_middleware_dir() -> Path:
    """Return the path to builtin middlewares shipped with the package."""
    return Path(__file__).resolve().parent.parent / "middleware"


def user_middleware_dir() -> Path:
    """Return the path to user-installed middlewares.

    Each middleware gets its own subdirectory::

        ~/.local/share/digitorn/middleware/french_only/
            digitorn-middleware.toml
            middleware.py
    """
    return Path(user_data_dir(APP_NAME)) / "middleware"


def all_middleware_dirs() -> list[Path]:
    """Return all middleware directories in priority order (lowest first).

    Priority: builtin < user
    """
    return [
        builtin_middleware_dir(),
        user_middleware_dir(),
    ]


def all_module_dirs() -> list[Path]:
    """Return all module directories in priority order (lowest first).

    Priority: builtin < system < user
    A module in a higher-priority dir overrides one with the same ID below it.
    """
    return [
        builtin_modules_dir(),
        system_modules_dir(),
        user_modules_dir(),
    ]
