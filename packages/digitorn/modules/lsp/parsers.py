"""Output parsers for linters, compilers, and language servers.

Each parser takes (stdout, stderr) and returns a list of Diagnostic objects.
Used by all 3 feedback protocols (LSP, compiler, linter).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class Diagnostic:
    """A single diagnostic from any source."""

    file: str
    line: int
    column: int
    severity: str  # error, warning, info, hint
    message: str
    code: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Flat shape kept for backwards compatibility with existing
        consumers (inline `lint` field on write/edit responses)."""
        d: dict[str, Any] = {
            "file": self.file, "line": self.line, "column": self.column,
            "severity": self.severity, "message": self.message,
        }
        if self.code:
            d["code"] = self.code
        if self.source:
            d["source"] = self.source
        return d

    def to_lsp_dict(self) -> dict[str, Any]:
        """LSP-standard shape — consumed by Monaco ``setModelMarkers()``
        and the `diagnostics` preview channel.

        `line`/`column` are 1-based (our convention) and get converted
        to 0-based for LSP. ``end`` defaults to the same position +1 col
        when the source parser doesn't provide an end — enough for a
        visible marker (1-char underline)."""
        line0 = max(0, (self.line or 1) - 1)
        col0 = max(0, (self.column or 1) - 1)
        d: dict[str, Any] = {
            "severity": self.severity,
            "message": self.message,
            "range": {
                "start": {"line": line0, "character": col0},
                "end": {"line": line0, "character": col0 + 1},
            },
        }
        if self.code:
            d["code"] = self.code
        if self.source:
            d["source"] = self.source
        return d


# ── Specific parsers ─────────────────────────────────────────────


def parse_ruff(stdout: str, stderr: str) -> list[Diagnostic]:
    """Parse ruff JSON output."""
    try:
        items = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError:
        return parse_fallback(stderr or stdout)
    return [
        Diagnostic(
            file=item.get("filename", ""),
            line=item.get("location", {}).get("row", 0),
            column=item.get("location", {}).get("column", 0),
            severity="error" if item.get("fix") is None else "warning",
            message=item.get("message", ""),
            code=item.get("code", ""),
            source="ruff",
        )
        for item in items
    ]


def parse_eslint(stdout: str, stderr: str) -> list[Diagnostic]:
    """Parse eslint --format=json output."""
    try:
        files = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError:
        return parse_fallback(stderr or stdout)
    diags: list[Diagnostic] = []
    for f in files:
        path = f.get("filePath", "")
        for msg in f.get("messages", []):
            sev = "error" if msg.get("severity", 0) == 2 else "warning"
            diags.append(Diagnostic(
                file=path, line=msg.get("line", 0), column=msg.get("column", 0),
                severity=sev, message=msg.get("message", ""),
                code=msg.get("ruleId", ""), source="eslint",
            ))
    return diags


def parse_tsc(stdout: str, stderr: str) -> list[Diagnostic]:
    """Parse tsc stderr (file(line,col): error TSxxxx: message)."""
    diags: list[Diagnostic] = []
    for line in (stderr or stdout).splitlines():
        m = re.match(r"^(.+?)\((\d+),(\d+)\):\s+(error|warning)\s+(TS\d+):\s+(.+)$", line)
        if m:
            diags.append(Diagnostic(
                file=m.group(1), line=int(m.group(2)), column=int(m.group(3)),
                severity=m.group(4), message=m.group(6), code=m.group(5), source="tsc",
            ))
    return diags


def parse_cargo(stdout: str, stderr: str) -> list[Diagnostic]:
    """Parse cargo check --message-format=json output."""
    diags: list[Diagnostic] = []
    for line in stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("reason") != "compiler-message":
            continue
        inner = msg.get("message", {})
        spans = inner.get("spans", [])
        span = spans[0] if spans else {}
        level = inner.get("level", "warning")
        diags.append(Diagnostic(
            file=span.get("file_name", ""), line=span.get("line_start", 0),
            column=span.get("column_start", 0),
            severity="error" if level == "error" else "warning",
            message=inner.get("message", ""),
            code=inner.get("code", {}).get("code", "") if inner.get("code") else "",
            source="cargo",
        ))
    return diags


def parse_govet(stdout: str, stderr: str) -> list[Diagnostic]:
    """Parse go vet -json output."""
    diags: list[Diagnostic] = []
    for line in (stdout or stderr).splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "Posn" in item:
            parts = item["Posn"].split(":")
            diags.append(Diagnostic(
                file=parts[0] if parts else "",
                line=int(parts[1]) if len(parts) > 1 else 0,
                column=int(parts[2]) if len(parts) > 2 else 0,
                severity="warning", message=item.get("Message", ""), source="go vet",
            ))
    return diags


def parse_generic_json(stdout: str, stderr: str) -> list[Diagnostic]:
    """Parse generic JSON array output: [{file, line, column?, severity?, message, code?}]."""
    try:
        items = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError:
        return parse_fallback(stderr or stdout)

    if isinstance(items, dict):
        items = items.get("diagnostics", items.get("errors", items.get("results", [items])))

    diags: list[Diagnostic] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        diags.append(Diagnostic(
            file=item.get("file", item.get("path", item.get("filename", ""))),
            line=item.get("line", item.get("row", item.get("lineNumber", 0))),
            column=item.get("column", item.get("col", item.get("character", 0))),
            severity=item.get("severity", item.get("level", "warning")),
            message=item.get("message", item.get("text", item.get("description", ""))),
            code=str(item.get("code", item.get("rule", item.get("ruleId", "")))),
            source=item.get("source", ""),
        ))
    return diags


def parse_generic_lines(stdout: str, stderr: str) -> list[Diagnostic]:
    """Parse generic line-based output: file:line:col: severity: message."""
    return parse_fallback(stderr or stdout)


def parse_fallback(text: str) -> list[Diagnostic]:
    """Best-effort parse of unstructured output (file:line:col: message)."""
    diags: list[Diagnostic] = []
    for line in text.splitlines():
        m = re.match(r"^(.+?):(\d+):(\d+):\s*(\w+:?\s*)?(.+)$", line)
        if m:
            sev_raw = (m.group(4) or "").strip().rstrip(":").lower()
            diags.append(Diagnostic(
                file=m.group(1), line=int(m.group(2)), column=int(m.group(3)),
                severity="error" if "error" in sev_raw else "warning",
                message=m.group(5).strip(),
            ))
    return diags


def parse_lsp_diagnostics(raw_diags: list[dict[str, Any]], path: str = "") -> list[Diagnostic]:
    """Convert LSP publishDiagnostics format to our Diagnostic objects."""
    severity_map = {1: "error", 2: "warning", 3: "info", 4: "hint"}
    diags: list[Diagnostic] = []
    for d in raw_diags:
        rng = d.get("range", {}).get("start", {})
        diags.append(Diagnostic(
            file=path,
            line=rng.get("line", 0) + 1,
            column=rng.get("character", 0) + 1,
            severity=severity_map.get(d.get("severity", 2), "warning"),
            message=d.get("message", ""),
            code=str(d.get("code", "")),
            source=d.get("source", "lsp"),
        ))
    return diags


# ── Built-in validators (no external tools needed) ──────────────


def validate_json_file(path: str) -> list[Diagnostic]:
    """Validate a JSON file using Python's json module."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        json.loads(content)
        return []
    except json.JSONDecodeError as e:
        return [Diagnostic(
            file=path, line=e.lineno or 1, column=e.colno or 1,
            severity="error", message=str(e.msg), code="json.decode_error", source="json",
        )]
    except Exception as e:
        return [Diagnostic(
            file=path, line=1, column=1,
            severity="error", message=str(e), source="json",
        )]


def validate_yaml_file(path: str) -> list[Diagnostic]:
    """Validate a YAML file using PyYAML."""
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        # Parse all documents in the YAML
        list(yaml.safe_load_all(content))
        return []
    except yaml.YAMLError as e:
        line, col = 1, 1
        if hasattr(e, "problem_mark") and e.problem_mark is not None:
            line = e.problem_mark.line + 1
            col = e.problem_mark.column + 1
        msg = getattr(e, "problem", str(e))
        return [Diagnostic(
            file=path, line=line, column=col,
            severity="error", message=msg, code="yaml.parse_error", source="yaml",
        )]
    except ImportError:
        return []  # PyYAML not installed — skip silently
    except Exception as e:
        return [Diagnostic(
            file=path, line=1, column=1,
            severity="error", message=str(e), source="yaml",
        )]


def validate_toml_file(path: str) -> list[Diagnostic]:
    """Validate a TOML file using Python's tomllib (3.11+)."""
    try:
        import tomllib
        with open(path, "rb") as f:
            tomllib.load(f)
        return []
    except Exception as e:
        msg = str(e)
        # Try to extract line number from error message
        line = 1
        m = re.search(r"line (\d+)", msg)
        if m:
            line = int(m.group(1))
        return [Diagnostic(
            file=path, line=line, column=1,
            severity="error", message=msg, code="toml.parse_error", source="toml",
        )]


def validate_python_syntax(path: str) -> list[Diagnostic]:
    """Validate Python syntax using compile() — catches syntax errors only."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        compile(source, path, "exec")
        return []
    except SyntaxError as e:
        return [Diagnostic(
            file=path, line=e.lineno or 1, column=e.offset or 1,
            severity="error", message=e.msg, code="SyntaxError", source="python",
        )]
    except Exception:
        return []


# Extension → built-in validator
BUILTIN_VALIDATORS: dict[str, Any] = {
    ".json": validate_json_file,
    ".jsonc": validate_json_file,
    ".yaml": validate_yaml_file,
    ".yml": validate_yaml_file,
    ".toml": validate_toml_file,
    ".py": validate_python_syntax,
    ".pyi": validate_python_syntax,
}


# ── Parser registry ──────────────────────────────────────────────

PARSERS: dict[str, Any] = {
    "ruff": parse_ruff,
    "mypy": parse_ruff,  # Similar JSON format
    "eslint": parse_eslint,
    "tsc": parse_tsc,
    "cargo": parse_cargo,
    "govet": parse_govet,
    "generic_json": parse_generic_json,
    "generic_lines": parse_generic_lines,
    "fallback": parse_fallback,
}


def get_parser(name: str) -> Any:
    """Get a parser by name, with fallback."""
    return PARSERS.get(name, parse_fallback)
