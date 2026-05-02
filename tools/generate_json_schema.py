"""Generate the canonical JSON Schema for a Digitorn app.yaml.

Pydantic v2 builds the schema natively from ``AppDefinition``. We
post-process it lightly to:

  - Add a stable ``$id`` so editors can pin the schema URL.
  - Add a ``title`` and ``description`` at the root.
  - Inject the ``yaml-language-server`` comment hint in the description
    so users know how to wire it.

Output: ``docs/schema/v1.json`` (relative to the repo root).

Usage::

    py -3.12 tools/generate_json_schema.py
    py -3.12 tools/generate_json_schema.py --output /tmp/schema.json
    py -3.12 tools/generate_json_schema.py --check        # exit 1 if drifted

The ``--check`` flag is a CI gate: it regenerates the schema and
exits non-zero if the on-disk file differs from the freshly generated
one. Drop it in CI to ensure the published schema stays in sync with
the Pydantic source of truth.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "docs" / "schema" / "v1.json"
SCHEMA_URL = "https://digitorn.ai/schema/v1.json"

sys.path.insert(0, str(ROOT / "packages"))


def _build_schema() -> dict:
    """Generate the JSON Schema and post-process it for publishing."""
    from digitorn.core.app.schema import AppDefinition  # noqa: WPS433

    schema = AppDefinition.model_json_schema()
    # Pydantic's auto-generated title is the model class name. Replace
    # with a human-friendly one and prepend our metadata.
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = SCHEMA_URL
    schema["title"] = "Digitorn app.yaml"
    schema["description"] = (
        "Schema for a Digitorn application. Wire it into your editor "
        "by adding this comment as the first line of your app.yaml:\n"
        f"\n    # yaml-language-server: $schema={SCHEMA_URL}\n"
        "\nVSCode + the YAML extension by Red Hat will then auto-complete "
        "every field, surface inline error messages, and document each "
        "value on hover."
    )
    return schema


def _serialize(schema: dict) -> str:
    """Pretty-print, sorted keys, trailing newline. Matches the format
    a human or a CI bot would produce so ``--check`` is stable."""
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def _display_path(path: Path) -> str:
    """Return a path string relative to the repo root when possible.

    Falls back to the absolute string when the target sits outside the
    tree (e.g. in a temp dir for tests)."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Where to write the schema (default: {_display_path(DEFAULT_OUTPUT)})",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="Compare against existing file, exit 1 on drift (CI gate).",
    )
    args = ap.parse_args()

    schema = _build_schema()
    payload = _serialize(schema)

    if args.check:
        if not args.output.is_file():
            print(f"[!] schema not generated yet: {args.output}", file=sys.stderr)
            return 1
        existing = args.output.read_text(encoding="utf-8")
        if existing != payload:
            print(
                f"[!] {_display_path(args.output)} drifted from "
                "AppDefinition. Run `py -3.12 tools/generate_json_schema.py` "
                "to refresh, then commit.",
                file=sys.stderr,
            )
            return 1
        print(f"[ok] {_display_path(args.output)} in sync.")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload, encoding="utf-8")
    print(f"[ok] wrote {_display_path(args.output)} ({len(payload)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
