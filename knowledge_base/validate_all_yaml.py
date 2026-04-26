r"""validate_all_yaml.py — the hallucination shield.

Extracts every ``\`\`\`yaml`` block from every ``.md`` file in the KB, plus
every ``*.yaml`` file under ``examples/`` and ``cookbook/``, and validates:

  1. YAML syntax (always)
  2. Pydantic AppDefinition shape (only when the block looks like a full
     app — i.e. starts with an ``app:`` top-level key)

Usage::

    py -3.12 knowledge_base/validate_all_yaml.py              # exits 1 on any error
    py -3.12 knowledge_base/validate_all_yaml.py --verbose     # show every block visited

Design goals:
  - ZERO false positives: a partial snippet (just a ``modules:`` fragment,
    a hook config, etc.) must not fail. We only reject blocks that
    syntactically parse as YAML but the parser rejects, OR full
    app-shaped blocks that fail Pydantic validation.
  - Fast: runs without the daemon, just loads AppDefinition.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

logging.getLogger("digitorn").setLevel(logging.ERROR)

REPO_ROOT = Path(__file__).resolve().parent.parent
KB_ROOT = REPO_ROOT / "knowledge_base"
PACKAGES_DIR = REPO_ROOT / "packages"


def _ensure_packages_on_path() -> None:
    p = str(PACKAGES_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


# ── YAML block extraction ──────────────────────────────────────────

# Accept fenced blocks starting with ```yaml or ```yml, with optional
# trailing metadata on the same line (e.g. ``` yaml compile=full``).
# The content continues until a closing ``` at the start of a line.
_CODE_BLOCK_RE = re.compile(
    r"^```y(?:a)?ml([^\n]*)\n(.*?)\n```",
    re.MULTILINE | re.DOTALL,
)


def extract_blocks_from_md(path: Path) -> list[tuple[int, str, str]]:
    """Return a list of ``(line_number, meta, block_content)`` for each ```yaml block."""
    text = path.read_text(encoding="utf-8")
    out: list[tuple[int, str, str]] = []
    for match in _CODE_BLOCK_RE.finditer(text):
        meta = (match.group(1) or "").strip()
        content = match.group(2)
        line = text[: match.start()].count("\n") + 1
        out.append((line, meta, content))
    return out


# ── Validation ──────────────────────────────────────────────────────


def _load_yaml(text: str) -> Any:
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit(f"[validate_all_yaml] pyyaml not installed → {exc}")
    return yaml.safe_load(text)


def _looks_like_full_app(parsed: Any) -> bool:
    """Heuristic: a full app YAML has a top-level ``app:`` mapping."""
    return isinstance(parsed, dict) and isinstance(parsed.get("app"), dict)


def _validate_app_shape(parsed: dict) -> str | None:
    """Run AppDefinition.model_validate on a full-app block. Returns None on success."""
    _ensure_packages_on_path()
    try:
        from digitorn.core.app.schema import AppDefinition
    except ImportError as exc:
        return f"cannot import AppDefinition: {exc}"

    try:
        AppDefinition.model_validate(parsed)
    except Exception as exc:
        return str(exc)
    return None


# ── Driver ──────────────────────────────────────────────────────────


def iter_md_files() -> list[Path]:
    return sorted(KB_ROOT.rglob("*.md"))


def iter_yaml_files() -> list[Path]:
    out: list[Path] = []
    for sub in ("examples", "cookbook"):
        d = KB_ROOT / sub
        if d.exists():
            out.extend(sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml")))
    return out


def _is_opt_out(meta: str) -> bool:
    """A block marked ``compile=skip`` is explicitly not validated (fragments)."""
    return "compile=skip" in meta or "validate=skip" in meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="Print every block visited, not just failures.")
    args = parser.parse_args()

    errors: list[str] = []
    checked_blocks = 0
    checked_full_apps = 0
    checked_files = 0

    # ── Markdown ```yaml blocks ──────────────────────────────────
    for md in iter_md_files():
        checked_files += 1
        for line_no, meta, block in extract_blocks_from_md(md):
            checked_blocks += 1
            rel = md.relative_to(REPO_ROOT)

            if _is_opt_out(meta):
                if args.verbose:
                    print(f"  · {rel}:{line_no} SKIP (opt-out)")
                continue

            # YAML syntax
            try:
                parsed = _load_yaml(block)
            except Exception as exc:
                errors.append(f"{rel}:{line_no} — YAML syntax error: {exc}")
                continue

            if parsed is None:
                if args.verbose:
                    print(f"  · {rel}:{line_no} empty block")
                continue

            # Full-app shape
            if _looks_like_full_app(parsed):
                checked_full_apps += 1
                err = _validate_app_shape(parsed)
                if err:
                    errors.append(f"{rel}:{line_no} — AppDefinition validation failed:\n    {err}")
                elif args.verbose:
                    print(f"  ✓ {rel}:{line_no} full-app OK")
            elif args.verbose:
                print(f"  · {rel}:{line_no} partial YAML — syntax OK")

    # ── Standalone .yaml files ───────────────────────────────────
    for yml in iter_yaml_files():
        checked_files += 1
        checked_blocks += 1
        rel = yml.relative_to(REPO_ROOT)
        try:
            text = yml.read_text(encoding="utf-8")
            # File-level opt-out for bundle-namespace / template-heavy
            # examples that only make sense through the real compiler.
            first_lines = "\n".join(text.splitlines()[:3])
            if "# validate: skip" in first_lines:
                if args.verbose:
                    print(f"  · {rel} SKIP (file opt-out)")
                continue
            parsed = _load_yaml(text)
        except Exception as exc:
            errors.append(f"{rel} — YAML syntax error: {exc}")
            continue
        if _looks_like_full_app(parsed):
            checked_full_apps += 1
            err = _validate_app_shape(parsed)
            if err:
                errors.append(f"{rel} — AppDefinition validation failed:\n    {err}")
            elif args.verbose:
                print(f"  ✓ {rel} full-app OK")
        elif args.verbose:
            print(f"  · {rel} not a full app (no top-level app:) — syntax OK")

    print(
        f"\n[validate_all_yaml] scanned {checked_files} file(s), "
        f"{checked_blocks} block(s), {checked_full_apps} full-app(s)"
    )

    if errors:
        print(f"\n[validate_all_yaml] ✗ {len(errors)} ERROR(S):\n", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("[validate_all_yaml] ✓ OK — every block is valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
