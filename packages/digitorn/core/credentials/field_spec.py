"""FieldSpec - typed declaration of one credential field.

Replaces the dict-based schema fields used by the legacy handler
contract with a structured dataclass. The UI uses the spec to render
forms (label, help, placeholder, validation), the handler uses it to
validate inputs, and the storage layer uses it to know which fields
to mask in display previews.

Backward compat: `to_dict()` returns the same shape the legacy schema
used so old code reading `schema_fields = [{...}, ...]` keeps working.
"""

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
    """Specification of one credential field.

    Used by handlers to declare what fields they need, by the UI to
    render the form, by the validator to enforce constraints.

    Field design:
      - `name`: machine name (used as dict key in encrypted_fields).
      - `label`: human-readable label for the form.
      - `type`: FieldType - drives the input widget.
      - `required`: must be non-empty to save.
      - `masked`: hide value in UI + register with LogScrubber after save.
      - `help`: short description / link below the input.
      - `placeholder`: greyed text inside empty input.
      - `default`: default value (only for non-secret fields).
      - `choices`: list of (value, label) pairs for SELECT / MULTISELECT.
      - `validation_regex`: extra regex check on the value.
      - `min_length` / `max_length`: bounds for TEXT/PASSWORD/TEXTAREA.
      - `prefix_check`: heuristic check that value starts with this
        string (used by `api_key.openai` to ensure user pastes `sk-...`).
      - `mime_types`: for FILE type, the accepted MIME types.
      - `inject_path_default`: where the YAML inject target should
        point by default if the user doesn't override (e.g. for
        api_key the natural target is `<block>.config.api_key`).
    """

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
        """Legacy-compatible dict representation. The legacy handler
        contract used dicts of this shape - we serialise to it for
        any caller that hasn't been migrated to typed FieldSpec yet."""
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
    """Convenience constructor for the most common case: a single
    masked api_key field. Used by the OpenAI / Anthropic / DeepSeek /
    etc. provider templates."""
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
