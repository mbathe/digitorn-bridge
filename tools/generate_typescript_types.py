"""Generate TypeScript types from the published JSON Schema.

Reads ``docs/schema/v1.json`` (the canonical AppDefinition export)
and emits a single ``.d.ts`` file with one TypeScript interface per
Pydantic model, plus type aliases for the literal unions.

Output paths:

  - ``packages/digitorn/builtins/digitorn-builder/web/src/lib/app-schema.d.ts``
    (canvas can `import type { AppDefinition } from "./lib/app-schema"`)
  - any other consumer can copy/symlink the same file

This stays a one-way generation: the Python schema is the source of
truth, the TS types are an artifact. Re-run after changing
``schema.py`` (or after ``generate_json_schema.py``).

Usage::

    py -3.12 tools/generate_typescript_types.py
    py -3.12 tools/generate_typescript_types.py --check   # CI gate

Limitations:

  - Free-form ``Record<string, unknown>`` for ``additionalProperties:
    true`` blocks (we don't try to be more precise).
  - Discriminated unions emitted as anonymous unions (TS 5+ infers the
    discriminator from the literal types).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "docs" / "schema" / "v1.json"
DEFAULT_OUTPUT = (
    ROOT / "packages" / "digitorn" / "builtins" / "digitorn-builder"
    / "web" / "src" / "lib" / "app-schema.d.ts"
)


# ─── JSON Schema → TypeScript type translation ─────────────────


def _safe_id(name: str) -> str:
    """Coerce a JSON Schema name into a valid TS identifier."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _json_to_ts(schema: dict, defs: dict[str, dict]) -> str:
    """Recursive: convert a JSON Schema fragment to a TS type expression."""
    # $ref → reference the definition by name
    if "$ref" in schema:
        ref = schema["$ref"]
        # "#/$defs/SomeName" → SomeName
        ref_name = ref.rsplit("/", 1)[-1]
        return _safe_id(ref_name)

    # const → literal
    if "const" in schema:
        v = schema["const"]
        return json.dumps(v) if isinstance(v, str) else str(v)

    # enum → union of literals
    if "enum" in schema:
        return " | ".join(
            json.dumps(v) if isinstance(v, str) else str(v)
            for v in schema["enum"]
        )

    # anyOf / oneOf → union
    for key in ("anyOf", "oneOf"):
        if key in schema:
            members = [_json_to_ts(s, defs) for s in schema[key]]
            return "(" + " | ".join(members) + ")"

    t = schema.get("type")
    if isinstance(t, list):
        # type: ["string", "null"] → string | null
        return "(" + " | ".join(_json_to_ts({"type": x}, defs) for x in t) + ")"

    if t == "null":
        return "null"
    if t == "string":
        return "string"
    if t == "boolean":
        return "boolean"
    if t == "number" or t == "integer":
        return "number"
    if t == "array":
        item = schema.get("items", {})
        if isinstance(item, list):
            # Tuple type
            return "[" + ", ".join(_json_to_ts(s, defs) for s in item) + "]"
        return f"Array<{_json_to_ts(item, defs)}>"
    if t == "object":
        # Inline object (no $ref) - prefer additionalProperties.
        ap = schema.get("additionalProperties")
        if isinstance(ap, dict):
            return f"Record<string, {_json_to_ts(ap, defs)}>"
        if ap is True:
            return "Record<string, unknown>"
        # No properties + no additionalProperties → empty record.
        props = schema.get("properties") or {}
        if props:
            required = set(schema.get("required") or [])
            parts = []
            for k, v in props.items():
                opt = "" if k in required else "?"
                parts.append(f"{json.dumps(k)}{opt}: {_json_to_ts(v, defs)}")
            return "{ " + "; ".join(parts) + " }"
        return "Record<string, unknown>"

    # Anything else
    return "unknown"


def _emit_interface(name: str, schema: dict, defs: dict[str, dict]) -> str:
    """Emit one `export interface` for a $defs entry."""
    sname = _safe_id(name)
    title = schema.get("title", name)
    description = schema.get("description", "")
    out = []
    if description:
        # JSDoc with description
        wrapped = "\n * ".join(description.strip().split("\n"))
        out.append(f"/** {wrapped} */")

    # Pure enum at top level → type alias.
    if "enum" in schema and "type" in schema and schema["type"] == "string":
        union = " | ".join(json.dumps(v) for v in schema["enum"])
        out.append(f"export type {sname} = {union};")
        return "\n".join(out)

    # Object with properties → interface.
    if schema.get("type") == "object" and "properties" in schema:
        required = set(schema.get("required") or [])
        body = []
        for k, v in schema["properties"].items():
            opt = "" if k in required else "?"
            field_desc = v.get("description", "")
            if field_desc:
                wrapped = "\n   * ".join(field_desc.strip().split("\n"))
                body.append(f"  /** {wrapped} */")
            body.append(f"  {json.dumps(k)}{opt}: {_json_to_ts(v, defs)};")
        out.append(f"export interface {sname} {{\n" + "\n".join(body) + "\n}")
        return "\n".join(out)

    # Fallback: emit as a type alias.
    out.append(f"export type {sname} = {_json_to_ts(schema, defs)};")
    return "\n".join(out)


def _build_dts(schema: dict) -> str:
    defs = schema.get("$defs", {})
    parts: list[str] = [
        "/* eslint-disable */",
        "/**",
        " * AUTO-GENERATED FROM `docs/schema/v1.json`.",
        " * DO NOT EDIT - run `py -3.12 tools/generate_typescript_types.py`",
        " * after changing `packages/digitorn/core/app/schema.py`.",
        " */",
        "",
    ]
    # Root AppDefinition → an interface named AppDefinition + supporting
    # types from $defs.
    if "properties" in schema:
        parts.append(_emit_interface("AppDefinition", schema, defs))
        parts.append("")
    for name in sorted(defs):
        parts.append(_emit_interface(name, defs[name], defs))
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


# ─── CLI ────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--check", action="store_true",
                    help="Exit 1 if the on-disk file differs.")
    args = ap.parse_args()

    if not args.input.exists():
        print(f"[!] {args.input} not found - run "
              f"`py -3.12 tools/generate_json_schema.py` first.",
              file=sys.stderr)
        return 1

    schema = json.loads(args.input.read_text(encoding="utf-8"))
    dts = _build_dts(schema)

    if args.check:
        if not args.output.exists():
            print(f"[!] {args.output} does not exist. Run without --check to create it.")
            return 1
        existing = args.output.read_text(encoding="utf-8")
        if existing != dts:
            print(f"[!] {args.output} drifted from the JSON Schema. "
                  f"Run `py -3.12 tools/generate_typescript_types.py` to refresh.")
            return 1
        print(f"[ok] {args.output} is up to date.")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(dts, encoding="utf-8")
    print(f"[ok] wrote {args.output} ({len(dts)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
