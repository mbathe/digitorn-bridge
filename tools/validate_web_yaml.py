"""Validate every YAML embedded in the digitorn_web marketing site.

Three sources are scanned:

  1. ``digitorn_web/content/blog/*.md``      - ```yaml fences
  2. ``digitorn_web/src/lib/templates.ts``   - ``yaml: `...` `` template literals
  3. ``digitorn_web/src/lib/patterns.ts``    - ``yaml: `...` `` template literals
  4. ``digitorn_web/src/lib/migrations.ts``  - ``digitornYaml: `...` ``

Each extracted YAML is classified as full-app (contains an ``app:`` key) or
partial snippet (a fragment of an app: hooks block, agents block, modules
block). Both are parsed for syntactic validity. Full-apps are additionally
validated through ``AppDefinition.model_validate``. Partials are wrapped in a
minimal app shell to test their structural shape against the schema.

A consolidated report is written to the same dir as this script:
``tools/web_yaml_report.md``. Exit code 0 if all blocks pass, 1 otherwise.

Usage::

    py -3.12 tools/validate_web_yaml.py
    py -3.12 tools/validate_web_yaml.py --web-root /path/to/digitorn_web
    py -3.12 tools/validate_web_yaml.py --verbose
"""
from __future__ import annotations

import argparse
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WEB = ROOT.parent / "digitorn_web"
sys.path.insert(0, str(ROOT / "packages"))


# ── 1. Extraction ──────────────────────────────────────────────


@dataclass
class YamlBlock:
    """One YAML snippet found in a source file."""

    source: Path
    """Origin file."""
    line: int
    """Line where the fence/template literal opens."""
    label: str
    """Friendly label: the slug, the scenario name, or the heading."""
    body: str
    """The YAML text itself."""
    kind: str
    """``full_app`` if the body declares ``app:`` at column 0,
    ``partial`` if it is a fragment of an app."""


def _classify(body: str) -> str:
    # Top-level ``app:`` at column 0 = full app definition.
    if re.search(r"^app:\s*$", body, re.MULTILINE):
        return "full_app"
    return "partial"


_FENCE_RE = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)


def extract_blog(blog_dir: Path) -> list[YamlBlock]:
    """Pull every ```yaml block from blog markdown files.

    Tracks the nearest preceding ``## `` or ``### `` heading as the label
    so a failure points at a section a human can find."""
    blocks: list[YamlBlock] = []
    for md in sorted(blog_dir.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        # Map line-of-fence -> nearest heading
        last_heading = "(top)"
        line_to_heading: dict[int, str] = {}
        for n, line in enumerate(text.splitlines(), start=1):
            if line.startswith("##"):
                last_heading = line.lstrip("#").strip() or "(top)"
            line_to_heading[n] = last_heading
        for m in _FENCE_RE.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            label = line_to_heading.get(line_no, "(top)")
            body = m.group(1).strip("\n")
            blocks.append(
                YamlBlock(
                    source=md,
                    line=line_no,
                    label=label,
                    body=body,
                    kind=_classify(body),
                )
            )
    return blocks


# Match `yaml: `...`,` and `digitornYaml: `...`,` template literals.
# We require the field name to disambiguate from `sourceCode: `...``.
_TS_TEMPLATE_RE = re.compile(
    r"(?:^|\s)(?P<key>yaml|digitornYaml):\s*`(?P<body>.*?)`",
    re.DOTALL,
)
_SLUG_RE = re.compile(r'slug:\s*"(?P<slug>[^"]+)"')
_SCENARIO_RE = re.compile(r'scenario:\s*"(?P<scenario>[^"]+)"')
_NAME_RE = re.compile(r'name:\s*"(?P<name>[^"]+)"')


def extract_ts(ts_path: Path, key_filter: str | None = None) -> list[YamlBlock]:
    """Pull template-literal YAMLs from a TS source file.

    The label resolution is best-effort: it walks back from the template
    literal to the nearest ``slug:``, ``scenario:``, or ``name:`` field
    in the same object literal."""
    if not ts_path.is_file():
        return []
    text = ts_path.read_text(encoding="utf-8")
    blocks: list[YamlBlock] = []
    for m in _TS_TEMPLATE_RE.finditer(text):
        if key_filter and m.group("key") != key_filter:
            continue
        body = m.group("body").strip("\n")
        line_no = text[: m.start()].count("\n") + 1
        # Walk back up to ~80 lines to find a slug/scenario/name field.
        head_start = max(0, m.start() - 4000)
        head = text[head_start : m.start()]
        label_match = (
            list(_SLUG_RE.finditer(head))
            or list(_SCENARIO_RE.finditer(head))
            or list(_NAME_RE.finditer(head))
        )
        label = label_match[-1].group(label_match[-1].lastgroup) if label_match else f"line {line_no}"
        blocks.append(
            YamlBlock(
                source=ts_path,
                line=line_no,
                label=label,
                body=body,
                kind=_classify(body),
            )
        )
    return blocks


# ── 2. Validation ───────────────────────────────────────────────


@dataclass
class ValidationResult:
    block: YamlBlock
    syntactic_ok: bool
    schema_ok: bool
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.syntactic_ok and self.schema_ok


def _semantic_warnings(parsed: dict[str, Any]) -> list[str]:
    """Catch the things Pydantic accepts but that are semantically wrong.

    The schema marks ``execution.mode`` optional with default ``conversation``.
    A YAML that declares triggers without ``mode: background`` will silently
    never fire. Apps that explicitly opt into ``mode: one_shot`` plus channels
    are also flagged because that combination rarely matches user intent."""

    warnings: list[str] = []
    execution = parsed.get("execution") or {}
    mode = execution.get("mode", "conversation")

    # 1. Triggers require background.
    triggers = execution.get("triggers") or []
    if triggers and mode != "background":
        warnings.append(
            f"execution.triggers declared but mode={mode!r}. "
            f"Triggers only fire in mode: background. The runtime will "
            f"silently ignore them."
        )

    # 2. Channels (Slack/email/webhook) usually mean conversation or background.
    modules = parsed.get("modules") or {}
    channels_block = modules.get("channels")
    if channels_block and mode == "one_shot":
        warnings.append(
            "modules.channels declared but mode=one_shot. Slack/email/webhook "
            "channels typically need 'conversation' (chat replies) or "
            "'background' (event-triggered). One-shot is rarely the intent."
        )

    # 3. Context compaction without conversation.
    hooks = execution.get("hooks") or []
    has_compact = any(
        (h.get("action") or {}).get("type") == "compact_context" for h in hooks if isinstance(h, dict)
    )
    if has_compact and mode == "one_shot":
        warnings.append(
            "compact_context hook declared but mode=one_shot. Compaction "
            "only matters across multiple turns, switch to mode: conversation."
        )

    return warnings


def _wrap_partial(body: str) -> str:
    """Prepend the minimal ``app:`` block so a partial passes Pydantic.

    Indentation is preserved verbatim - we only add what's missing."""
    shell = "app:\n  app_id: validator-shell\n  name: \"validator shell\"\n"
    return shell + body


def _validate_one(block: YamlBlock) -> ValidationResult:
    try:
        import yaml  # PyYAML
    except ImportError:
        return ValidationResult(block, False, False, "PyYAML not installed")

    # 1. Syntactic check via PyYAML.
    try:
        parsed = yaml.safe_load(block.body)
        if parsed is None:
            return ValidationResult(block, False, False, "empty document")
        if not isinstance(parsed, dict):
            # A bare list/string is valid YAML but not a Digitorn config.
            return ValidationResult(
                block, True, False, f"top-level is {type(parsed).__name__}, expected mapping"
            )
    except yaml.YAMLError as exc:
        return ValidationResult(block, False, False, f"YAML syntax: {exc}")

    # 2. Schema validation via Pydantic. Wrap partials.
    try:
        from digitorn.core.app.schema import AppDefinition  # noqa: WPS433
    except Exception as exc:
        return ValidationResult(
            block, True, False, f"cannot import AppDefinition: {exc}"
        )

    candidate = block.body
    if block.kind == "partial":
        candidate = _wrap_partial(candidate)

    try:
        parsed_full = yaml.safe_load(candidate)
        AppDefinition.model_validate(parsed_full)
        warnings = _semantic_warnings(parsed_full)
        return ValidationResult(block, True, True, warnings=warnings)
    except Exception as exc:
        # Pydantic errors are verbose - keep the first 800 chars so the
        # report is readable.
        msg = str(exc)
        if len(msg) > 800:
            msg = msg[:800] + " ... [truncated]"
        return ValidationResult(block, True, False, msg)


# ── 3. Report ───────────────────────────────────────────────────


def _format_summary(results: list[ValidationResult]) -> str:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    syntactic = sum(1 for r in results if r.syntactic_ok)
    schema_only = sum(1 for r in results if r.syntactic_ok and r.schema_ok)
    with_warnings = sum(1 for r in results if r.warnings)
    return (
        f"- Total YAMLs scanned: **{total}**\n"
        f"- Pass syntactic check: **{syntactic}/{total}**\n"
        f"- Pass schema check: **{schema_only}/{total}**\n"
        f"- All-green: **{passed}/{total}**\n"
        f"- With semantic warnings: **{with_warnings}/{total}**\n"
    )


def write_report(results: list[ValidationResult], path: Path) -> None:
    lines: list[str] = []
    lines.append("# Web YAML validation report\n")
    lines.append(_format_summary(results))
    lines.append("\n## Failures\n")
    failures = [r for r in results if not r.passed]
    if not failures:
        lines.append("\nNone. Every YAML passes syntactic + schema checks.\n")
    else:
        for r in failures:
            rel = r.block.source
            try:
                rel = r.block.source.relative_to(ROOT.parent)
            except ValueError:
                pass
            stage = "syntax" if not r.syntactic_ok else "schema"
            lines.append(
                f"\n### `{rel}` line {r.block.line} - `{r.block.label}` "
                f"({r.block.kind}, failed at {stage})\n"
            )
            lines.append("```\n" + r.error + "\n```\n")
    warnings = [r for r in results if r.warnings]
    if warnings:
        lines.append("\n## Semantic warnings\n")
        lines.append(
            "\nThese YAMLs pass the schema but the runtime semantics are "
            "probably wrong. Read each warning and decide whether to fix.\n"
        )
        for r in warnings:
            rel = r.block.source
            try:
                rel = r.block.source.relative_to(ROOT.parent)
            except ValueError:
                pass
            lines.append(
                f"\n### `{rel}` line {r.block.line} - `{r.block.label}`\n"
            )
            for w in r.warnings:
                lines.append(f"- {w}")
            lines.append("")

    lines.append("\n## Per-source breakdown\n")
    by_source: dict[str, list[ValidationResult]] = {}
    for r in results:
        by_source.setdefault(str(r.block.source.name), []).append(r)
    for src, rs in sorted(by_source.items()):
        passed = sum(1 for r in rs if r.passed)
        lines.append(f"- **{src}**: {passed}/{len(rs)} pass")
    path.write_text("\n".join(lines), encoding="utf-8")


# ── 4. Main ─────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--web-root",
        type=Path,
        default=DEFAULT_WEB,
        help=f"Path to the digitorn_web checkout (default: {DEFAULT_WEB})",
    )
    ap.add_argument("--verbose", action="store_true", help="print every result, not just failures")
    ap.add_argument(
        "--report",
        type=Path,
        default=ROOT / "tools" / "web_yaml_report.md",
        help="Where to write the markdown report",
    )
    args = ap.parse_args()

    web = args.web_root.resolve()
    if not web.is_dir():
        print(f"[!] web root not found: {web}", file=sys.stderr)
        return 2

    blog_dir = web / "content" / "blog"
    lib_dir = web / "src" / "lib"

    print(f"-> scanning {blog_dir}", flush=True)
    blocks: list[YamlBlock] = []
    if blog_dir.is_dir():
        blocks.extend(extract_blog(blog_dir))
    print(f"-> scanning {lib_dir}/templates.ts", flush=True)
    blocks.extend(extract_ts(lib_dir / "templates.ts", key_filter="yaml"))
    print(f"-> scanning {lib_dir}/patterns.ts", flush=True)
    blocks.extend(extract_ts(lib_dir / "patterns.ts", key_filter="yaml"))
    print(f"-> scanning {lib_dir}/migrations.ts", flush=True)
    blocks.extend(extract_ts(lib_dir / "migrations.ts", key_filter="digitornYaml"))
    print(f"-> {len(blocks)} YAML blocks extracted", flush=True)

    print("-> validating...", flush=True)
    results: list[ValidationResult] = []
    for b in blocks:
        try:
            results.append(_validate_one(b))
        except Exception as exc:  # noqa: BLE001
            results.append(
                ValidationResult(b, False, False, f"validator crashed: {exc}\n{traceback.format_exc()}")
            )

    if args.verbose:
        for r in results:
            mark = "OK" if r.passed else "FAIL"
            print(f"  [{mark}] {r.block.source.name}:{r.block.line} {r.block.label}")
            if not r.passed:
                print(f"        -> {r.error.splitlines()[0]}")

    print("\n" + _format_summary(results))

    write_report(results, args.report)
    print(f"-> report: {args.report}")

    failures = [r for r in results if not r.passed]
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
