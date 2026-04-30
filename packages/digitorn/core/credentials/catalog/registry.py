"""CatalogRegistry - in-memory dictionary of provider templates.

The registry is global. The daemon loads built-in templates at boot
(`load_builtin_catalog()`) plus optional user / app templates as
they're discovered. The compiler and API surface a `ProviderTemplate`
when the user picks a provider for a credential.

Lookup is case-sensitive on `name`. Categories let the UI group
templates into sections (LLM / Database / Cloud / etc.).
"""

from __future__ import annotations

import logging
from pathlib import Path

from digitorn.core.credentials.catalog.template import (
    ProviderTemplate,
    load_template_from_toml,
)

logger = logging.getLogger(__name__)


# Where the built-in templates live, relative to this file.
_BUILTINS_DIR = Path(__file__).resolve().parent / "builtins"


class CatalogRegistry:
    """Maps provider name → ProviderTemplate."""

    def __init__(self) -> None:
        self._by_name: dict[str, ProviderTemplate] = {}

    def register(self, tmpl: ProviderTemplate, *, replace: bool = False) -> None:
        """Add a template. By default, refuses to overwrite an existing
        entry (the catalog is meant to be stable). Set `replace=True`
        to update (e.g. when a user TOML overrides a builtin)."""
        existing = self._by_name.get(tmpl.name)
        if existing is not None and not replace:
            logger.warning(
                "catalog_template_collision name=%s existing=%s new=%s "
                "(keeping existing; pass replace=True to override)",
                tmpl.name,
                existing.source_path,
                tmpl.source_path,
            )
            return
        self._by_name[tmpl.name] = tmpl

    def get(self, name: str) -> ProviderTemplate | None:
        return self._by_name.get(name)

    def list_all(self) -> list[ProviderTemplate]:
        return sorted(self._by_name.values(), key=lambda t: (t.category, t.name))

    def list_by_category(self) -> dict[str, list[ProviderTemplate]]:
        out: dict[str, list[ProviderTemplate]] = {}
        for t in self.list_all():
            out.setdefault(t.category or "other", []).append(t)
        return out

    def list_by_handler(self, handler_type: str) -> list[ProviderTemplate]:
        return [
            t for t in self._by_name.values()
            if t.handler_type == handler_type
        ]

    def load_dir(self, dir: Path, *, replace: bool = False) -> int:
        """Load every `*.toml` in `dir`. Returns the count loaded."""
        if not dir.is_dir():
            return 0
        count = 0
        for path in sorted(dir.glob("*.toml")):
            try:
                tmpl = load_template_from_toml(path)
                self.register(tmpl, replace=replace)
                count += 1
            except Exception as exc:
                logger.warning(
                    "catalog_template_load_failed path=%s error=%s",
                    path, exc,
                )
        return count

    def __len__(self) -> int:
        return len(self._by_name)


# Module-level default catalog. Boot path populates it.
default_catalog = CatalogRegistry()


def load_builtin_catalog(reg: CatalogRegistry | None = None) -> int:
    """Load every TOML in `catalog/builtins/` into the registry.

    Idempotent for `default_catalog` callers - re-loading a builtin
    on top of itself doesn't fail (logged as collision and skipped).
    Returns the number of templates loaded.
    """
    target = reg if reg is not None else default_catalog
    return target.load_dir(_BUILTINS_DIR, replace=False)
