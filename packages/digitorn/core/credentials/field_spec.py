"""FieldSpec - typed declaration of one credential field."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class FieldType(str, Enum):
    """How the UI should render the input."""

    TEXT = "text"           # plain text, single line
    PASSWORD = "password"   # masked single-line input
    TEXTAREA = "textarea"   # multi-line plain (e.g. SSH private key, PEM cert)
    URL = "url"             # validated as URL
    EMAIL = "email"         # validated as email
    NUMBER = "number"       # numeric input
    SELECT = "select"       # one-of dropdown - requires `choices`
    MULTISELECT = "multiselect"  # tagged list of choices
    JSON = "json"           # large blob, validated as JSON (e.g. GCP SA key)
    FILE = "file"           # binary file upload (e.g. cert)
    BOOLEAN = "boolean"     # checkbox


@dataclass(frozen=True)
class FieldSpec:
    """Specification of one credential field."""

    name: str
    label: str
    type: FieldType = FieldType.TEXT
    required: bool = True
    masked: bool = False
    help: str = ""
    placeholder: str = ""
    default: Any = None
    choices: list[tuple[str, str]] = field(default_factory=list)
    validation_regex: str = ""
    min_length: int | None = None
    max_length: int | None = None
    prefix_check: str = ""
    mime_types: list[str] = field(default_factory=list)
    inject_path_default: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Legacy-compatible dict representation. The legacy handler"""
        d = asdict(self)
        d["type"] = self.type.value
        return d


def fields_to_dicts(specs: list[FieldSpec]) -> list[dict[str, Any]]:
    """Convert a list of FieldSpec to the legacy dict format."""
    return [s.to_dict() for s in specs]


def common_api_key(
    label: str = "API Key",
    help: str = "",
    placeholder: str = "",
    prefix_check: str = "",
    inject_path: str = "{block}.config.api_key",
) -> FieldSpec:
    """Convenience constructor for the most common case: a single"""
    return FieldSpec(
        name="api_key",
        label=label,
        type=FieldType.PASSWORD,
        required=True,
        masked=True,
        help=help,
        placeholder=placeholder,
        prefix_check=prefix_check,
        min_length=8,
        inject_path_default=inject_path,
    )
