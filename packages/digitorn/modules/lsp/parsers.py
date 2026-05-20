"""Output parsers for linters, compilers, and language servers."""

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
        """Flat shape kept for backwards compatibility with existing."""
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
        """LSP-standard shape - consumed by Monaco `setModelMarkers()`."""
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

def parse_tectonic(stdout: str, stderr: str) -> list[Diagnostic]:
    """Parse tectonic compiler output."""
    diags: list[Diagnostic] = []
    text = (stdout or "") + "\n" + (stderr or "")

    # Pattern 1: structured tectonic errors
    #   error: file.tex:42: Undefined control sequence
    for m in re.finditer(
        r"^error:\s+([^:\n]+?):(\d+):\s+(.+)$",
        text, flags=re.MULTILINE,
    ):
        diags.append(Diagnostic(
            file=m.group(1).strip(),
            line=int(m.group(2)),
            column=1,
            severity="error",
            message=m.group(3).strip(),
            source="tectonic",
        ))

    # Pattern 2: LaTeX-level warnings (file context inferred from
    # the most recent `(file.tex` token tectonic emits in its
    # transcript).
    current_file = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        # Track current file via `(filename` markers tectonic prints
        # when opening a .tex file in the transcript stream.
        for f in re.findall(r"\(([^()\s]+\.tex)", line):
            current_file = f
        m = re.match(
            r"^LaTeX (Warning|Error|Info):\s+(.+?)(?:\s+on input line\s+(\d+))?\.?\s*$",
            line,
        )
        if m and m.group(1) in ("Warning", "Error"):
            sev = "error" if m.group(1) == "Error" else "warning"
            lineno = int(m.group(3)) if m.group(3) else 0
            diags.append(Diagnostic(
                file=current_file,
                line=lineno,
                column=1,
                severity=sev,
                message=m.group(2).strip(),
                source="tectonic",
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

def parse_fallback(stdout: str, stderr: str = "") -> list[Diagnostic]:
    """Best-effort parse of unstructured output (file:line:col: message)."""
    diags: list[Diagnostic] = []
    for text in (stdout, stderr):
        if not text:
            continue
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

_CHKTEX_RE = re.compile(
    r"^(?P<sev>Warning|Error|Message)\s+(?P<code>\d+)\s+in\s+"
    r"(?P<file>.+?)\s+line\s+(?P<line>\d+):\s*(?P<msg>.+)$"
)

def parse_chktex(stdout: str, stderr: str = "") -> list[Diagnostic]:
    """Parse chktex's native multi-line output."""
    diags: list[Diagnostic] = []
    # chktex prints diagnostics to stdout; legacy builds swap streams.
    for src in (stdout, stderr):
        if not src or not src.strip():
            continue
        lines = src.splitlines()
        i = 0
        while i < len(lines):
            m = _CHKTEX_RE.match(lines[i])
            if not m:
                i += 1
                continue
            col = 1
            if i + 2 < len(lines):
                caret_line = lines[i + 2]
                caret_pos = caret_line.find("^")
                if caret_pos >= 0:
                    col = caret_pos + 1
            sev_raw = m.group("sev").lower()
            severity = "error" if sev_raw == "error" else "warning"
            diags.append(Diagnostic(
                file=m.group("file"),
                line=int(m.group("line")),
                column=col,
                severity=severity,
                message=m.group("msg").strip(),
                code=m.group("code"),
                source="chktex",
            ))
            i += 1
        if diags:
            return diags  # stop scanning subsequent streams once we have hits
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

PARSERS: dict[str, Any] = {
    "ruff": parse_ruff,
    "mypy": parse_ruff,  # Similar JSON format
    "eslint": parse_eslint,
    "tsc": parse_tsc,
    "cargo": parse_cargo,
    "govet": parse_govet,
    "tectonic": parse_tectonic,
    "chktex": parse_chktex,
    "generic_json": parse_generic_json,
    "generic_lines": parse_generic_lines,
    "fallback": parse_fallback,
}

def get_parser(name: str) -> Any:
    """Get a parser by name, with fallback."""
    return PARSERS.get(name, parse_fallback)
