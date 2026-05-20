"""ProviderTemplate dataclass + TOML loader."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - we require 3.12 anyway
    import tomli as tomllib


@dataclass(frozen=True)
class ProviderField:
    """Field-level overrides for a provider template."""

    name: str
    label: str | None = None
    help: str | None = None
    placeholder: str | None = None
    prefix_check: str | None = None
    validation_regex: str | None = None
    required: bool | None = None
    default: Any = None
    choices: list[tuple[str, str]] | None = None


@dataclass(frozen=True)
class ProviderVerify:
    """How to perform `test_live_connection` for this provider."""

    endpoint: str
    method: str = "GET"
    auth_template: str = ""  # e.g. "Authorization: Bearer {api_key}"
    success_codes: list[int] = field(default_factory=lambda: [200])
    body_template: str | None = None  # optional templated body (POST)


@dataclass(frozen=True)
class ProviderOAuth:
    """OAuth-specific URLs and defaults for handlers oauth2 / oauth2_pkce / device_code."""

    auth_url: str
    token_url: str
    scopes: list[str] = field(default_factory=list)
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    revoke_url: str | None = None
    userinfo_url: str | None = None
    # For device_code:
    device_code_url: str | None = None


@dataclass(frozen=True)
class ProviderTemplate:
    """One declarative provider entry in the catalog."""

    name: str  # stable slug used by app.yaml `provider: <name>`
    handler_type: str  # which CredentialHandler to dispatch to
    display_name: str = ""
    icon: str = ""  # icon slug or URL for the UI
    category: str = ""  # llm / database / cloud / messaging / ... (UI grouping)
    description: str = ""
    homepage: str = ""
    fields: list[ProviderField] = field(default_factory=list)
    verify: ProviderVerify | None = None
    oauth: ProviderOAuth | None = None
    # Module-author or builtin?
    builtin: bool = True
    source_path: str | None = None  # populated by loader for debugging

    def effective_fields(self, handler_default_fields: list[Any]) -> list[Any]:
        """Merge `fields` overrides on top of the handler's default"""
        from digitorn.core.credentials.field_spec import FieldSpec
        from dataclasses import replace

        # Index handler defaults by name.
        defaults: dict[str, FieldSpec] = {f.name: f for f in handler_default_fields}
        # Index template overrides by name.
        overrides: dict[str, ProviderField] = {f.name: f for f in self.fields}

        result: list[FieldSpec] = []
        seen: set[str] = set()
        for d in handler_default_fields:
            ov = overrides.get(d.name)
            if ov is None:
                result.append(d)
            else:
                # apply non-None override values
                kw: dict[str, Any] = {}
                if ov.label is not None:
                    kw["label"] = ov.label
                if ov.help is not None:
                    kw["help"] = ov.help
                if ov.placeholder is not None:
                    kw["placeholder"] = ov.placeholder
                if ov.prefix_check is not None:
                    kw["prefix_check"] = ov.prefix_check
                if ov.validation_regex is not None:
                    kw["validation_regex"] = ov.validation_regex
                if ov.required is not None:
                    kw["required"] = ov.required
                if ov.default is not None:
                    kw["default"] = ov.default
                if ov.choices is not None:
                    kw["choices"] = list(ov.choices)
                result.append(replace(d, **kw))
            seen.add(d.name)

        # Any catalog-only fields (not in handler defaults) - construct
        # a fresh FieldSpec from the override.
        for ov in self.fields:
            if ov.name in seen:
                continue
            from digitorn.core.credentials.field_spec import FieldType
            spec = FieldSpec(
                name=ov.name,
                label=ov.label or ov.name,
                type=FieldType.TEXT,
                required=ov.required if ov.required is not None else False,
                masked=False,
                help=ov.help or "",
                placeholder=ov.placeholder or "",
                default=ov.default,
                choices=list(ov.choices) if ov.choices is not None else [],
                validation_regex=ov.validation_regex or "",
            )
            result.append(spec)

        return result

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the manifest endpoint (UI consumption)."""
        d: dict[str, Any] = {
            "name": self.name,
            "handler_type": self.handler_type,
            "display_name": self.display_name or self.name,
            "icon": self.icon,
            "category": self.category,
            "description": self.description,
            "homepage": self.homepage,
            "builtin": self.builtin,
        }
        if self.verify:
            d["verify"] = {
                "endpoint": self.verify.endpoint,
                "method": self.verify.method,
                "success_codes": list(self.verify.success_codes),
                # auth_template intentionally NOT included - it can
                # contain field placeholders we don't need to expose.
            }
        if self.oauth:
            d["oauth"] = {
                "auth_url": self.oauth.auth_url,
                "token_url": self.oauth.token_url,
                "scopes": list(self.oauth.scopes),
                "userinfo_url": self.oauth.userinfo_url,
                "device_code_url": self.oauth.device_code_url,
            }
        return d


def load_template_from_toml(path: Path) -> ProviderTemplate:
    """Parse a TOML file into a ProviderTemplate."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    prov = data.get("provider", {})
    if not isinstance(prov, dict):
        raise ValueError(f"{path}: [provider] section must be a table")
    name = prov.get("name", "")
    handler_type = prov.get("handler_type", "")
    if not name or not handler_type:
        raise ValueError(
            f"{path}: provider.name and provider.handler_type are required",
        )

    fields_list: list[ProviderField] = []
    for f in data.get("fields", []) or []:
        if not isinstance(f, dict) or "name" not in f:
            continue
        choices_raw = f.get("choices")
        choices = None
        if isinstance(choices_raw, list):
            tup_choices: list[tuple[str, str]] = []
            for c in choices_raw:
                if isinstance(c, list) and len(c) == 2:
                    tup_choices.append((str(c[0]), str(c[1])))
                elif isinstance(c, dict) and "value" in c and "label" in c:
                    tup_choices.append((str(c["value"]), str(c["label"])))
            choices = tup_choices
        fields_list.append(ProviderField(
            name=f["name"],
            label=f.get("label"),
            help=f.get("help"),
            placeholder=f.get("placeholder"),
            prefix_check=f.get("prefix_check"),
            validation_regex=f.get("validation_regex"),
            required=f.get("required"),
            default=f.get("default"),
            choices=choices,
        ))

    verify = None
    if "verify" in data and isinstance(data["verify"], dict):
        v = data["verify"]
        if v.get("endpoint"):
            verify = ProviderVerify(
                endpoint=v["endpoint"],
                method=v.get("method", "GET"),
                auth_template=v.get("auth_template", ""),
                success_codes=list(v.get("success_codes", [200])),
                body_template=v.get("body_template"),
            )

    oauth = None
    if "oauth" in data and isinstance(data["oauth"], dict):
        o = data["oauth"]
        if o.get("auth_url") and o.get("token_url"):
            oauth = ProviderOAuth(
                auth_url=o["auth_url"],
                token_url=o["token_url"],
                scopes=list(o.get("scopes", [])),
                extra_auth_params=dict(o.get("extra_auth_params", {})),
                revoke_url=o.get("revoke_url"),
                userinfo_url=o.get("userinfo_url"),
                device_code_url=o.get("device_code_url"),
            )

    return ProviderTemplate(
        name=name,
        handler_type=handler_type,
        display_name=prov.get("display_name", ""),
        icon=prov.get("icon", ""),
        category=prov.get("category", ""),
        description=prov.get("description", ""),
        homepage=prov.get("homepage", ""),
        fields=fields_list,
        verify=verify,
        oauth=oauth,
        builtin=prov.get("builtin", True),
        source_path=str(path),
    )
