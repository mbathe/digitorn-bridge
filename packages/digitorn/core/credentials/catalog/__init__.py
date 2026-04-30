"""Provider catalog - TOML-defined templates that bridge handler_type
to concrete providers like OpenAI, Anthropic, GitHub.

A `ProviderTemplate` declares:

  - `name`: stable slug used by the YAML (`provider: openai`).
  - `display_name`, `icon`, `category`: UI metadata.
  - `handler_type`: which credential handler executes the lifecycle.
  - `fields[]`: provider-specific FieldSpec overrides (label, help,
    placeholder, prefix_check, validation_regex). Merged on top of
    the handler's default schema_fields().
  - `verify`: optional `test_endpoint` + auth header template for
    `test_live_connection`.
  - `oauth`: for handler_type=oauth2/oauth2_pkce/device_code, the
    URLs + default scopes.

The catalog is loaded at boot from `builtins/*.toml` (always
shipped) plus an optional user catalog at `~/.digitorn/catalog/*.toml`
and per-app catalogs at `<app>/credentials/*.toml` (loaded when the
app is deployed).

The compiler / API surface a `ProviderTemplate` to the UI when the
user is picking which provider to bind a slot to.
"""

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
