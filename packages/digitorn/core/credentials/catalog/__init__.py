"""Provider catalog - TOML-defined templates that bridge handler_type"""

from __future__ import annotations

from digitorn.core.credentials.catalog.template import (
    ProviderTemplate,
    ProviderField,
    ProviderVerify,
    ProviderOAuth,
    load_template_from_toml,
)
from digitorn.core.credentials.catalog.registry import (
    CatalogRegistry,
    default_catalog,
    load_builtin_catalog,
)

__all__ = [
    "ProviderTemplate",
    "ProviderField",
    "ProviderVerify",
    "ProviderOAuth",
    "load_template_from_toml",
    "CatalogRegistry",
    "default_catalog",
    "load_builtin_catalog",
]
