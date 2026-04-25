"""Docs validation — static guarantee that documentation matches code.

Runs four checks:

  1. Extract every ```yaml block from docs/**/*.md.
  2. Compile full-app YAMLs through AppYAMLCompiler — PASS/FAIL report.
  3. Verify every action name mentioned in docs exists in @action decorators.
  4. Verify every REST route documented in protocol/REST_API.md matches a real
     FastAPI @router decorator.

Writes a consolidated report to docs/VALIDATION_REPORT.md.

Usage:
    py -3.12 tools/validate_docs.py            # full run
    py -3.12 tools/validate_docs.py --yaml     # only (1)+(2)
    py -3.12 tools/validate_docs.py --actions  # only (3)
    py -3.12 tools/validate_docs.py --routes   # only (4)
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PACKAGES = ROOT / "packages" / "digitorn"
MODULES_DIR = PACKAGES / "modules"
API_DIR = PACKAGES / "core" / "api"

# Make the digitorn package importable.
sys.path.insert(0, str(ROOT / "packages"))


# ── 1. YAML extraction ──────────────────────────────────────────

YAML_BLOCK_RX = re.compile(
    r"^(?P<fence>```+)\s*yaml\s*\n(?P<body>.*?)\n(?P=fence)\s*$",
    re.MULTILINE | re.DOTALL,
)


@dataclass
class YamlBlock:
    path: Path
    line: int
    body: str
    is_full_app: bool


def extract_yaml_blocks() -> list[YamlBlock]:
    blocks: list[YamlBlock] = []
    for md in sorted(DOCS.rglob("*.md")):
        try:
            txt = md.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in YAML_BLOCK_RX.finditer(txt):
            body = m.group("body")
            start_line = txt[: m.start()].count("\n") + 1
            is_full_app = _looks_like_full_app(body)
            blocks.append(
                YamlBlock(
                    path=md.relative_to(ROOT),
                    line=start_line,
                    body=body,
                    is_full_app=is_full_app,
                )
            )
    return blocks


def _looks_like_full_app(body: str) -> bool:
    """A full app YAML typically has top-level `app:` + (`agents:` or `execution:`)."""
    has_app_id = re.search(r"^\s*app\s*:\s*$", body, re.MULTILINE) is not None
    has_app_id |= "app_id:" in body
    has_agents = re.search(r"^\s*agents\s*:\s*$", body, re.MULTILINE) is not None
    has_execution = re.search(r"^\s*execution\s*:\s*$", body, re.MULTILINE) is not None
    return has_app_id and (has_agents or has_execution)


# ── 2. Compile YAMLs ────────────────────────────────────────────

@dataclass
class CompileResult:
    block: YamlBlock
    ok: bool
    error: str = ""


def compile_blocks(blocks: list[YamlBlock]) -> list[CompileResult]:
    try:
        from digitorn.core.app.compiler import AppYAMLCompiler, AppCompilationError
        from digitorn.core.loader import load_modules
        from digitorn.modules.registry import ModuleRegistry
    except Exception as exc:
        print(f"FATAL: can't import compiler stack: {exc}", flush=True)
        traceback.print_exc()
        return []

    registry = ModuleRegistry()
    try:
        load_modules(registry)
    except Exception as exc:
        print(f"WARN: load_modules failed: {exc} — continuing with partial registry", flush=True)

    compiler = AppYAMLCompiler(registry)
    results: list[CompileResult] = []

    for block in blocks:
        if not block.is_full_app:
            continue
        try:
            compiler.compile_string(block.body, source=str(block.path))
            results.append(CompileResult(block, ok=True))
        except AppCompilationError as exc:
            msg = "; ".join(getattr(exc, "errors", [])) or str(exc)
            results.append(CompileResult(block, ok=False, error=msg))
        except Exception as exc:
            results.append(CompileResult(block, ok=False, error=f"{type(exc).__name__}: {exc}"))
    return results


# ── 3. Action-name audit ────────────────────────────────────────

def scan_code_actions() -> dict[str, set[str]]:
    """Return {module_id: set of @action function names}."""
    results: dict[str, set[str]] = {}
    for mod_dir in sorted(MODULES_DIR.iterdir()):
        if not mod_dir.is_dir() or mod_dir.name.startswith("_"):
            continue
        actions: set[str] = set()
        for py in mod_dir.rglob("*.py"):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except Exception:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    if not isinstance(dec, ast.Call):
                        continue
                    fname = None
                    if isinstance(dec.func, ast.Name):
                        fname = dec.func.id
                    elif isinstance(dec.func, ast.Attribute):
                        fname = dec.func.attr
                    if fname == "action":
                        actions.add(node.name)
        if actions:
            results[mod_dir.name] = actions
    return results


# Extract "module.action" tokens from module reference docs.
FQN_RX = re.compile(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b")

# These module names are not Digitorn modules — skip them.
SKIP_MODULE_TOKENS = {
    "os", "sys", "json", "yaml", "logging", "re", "md", "html", "py",
    "txt", "pdf", "csv", "example", "en", "fr", "com", "org", "io", "ai",
    "github", "google", "slack", "telegram", "discord", "tools", "widgets",
    "rss", "brave", "tavily", "searxng", "duckduckgo", "openai", "anthropic",
    "claude", "deepseek", "groq", "mistral", "ollama", "gemini", "llama",
    "self", "cls", "this", "item", "form", "state", "ctx", "event",
    "package", "digitorn", "dashboard", "demo", "pattern", "url",
    "localhost", "console", "pkg", "app", "msg", "res", "err", "data",
    "stats", "main", "local", "default", "test", "name", "id", "type",
    "value", "key", "src", "dst", "file", "path", "dir", "foo", "bar",
    "x", "y", "z", "a", "b", "c", "d", "e", "f", "g", "h", "i",
    "node_modules", "dist", "public", "static", "build", "docs", "src",
    "user", "admin", "owner", "role", "v", "v1", "v2", "v3",
}


def scan_doc_action_mentions() -> list[tuple[Path, int, str]]:
    """Find `module.action` references inside docs/modules/reference/*.md."""
    mentions: list[tuple[Path, int, str]] = []
    refs = DOCS / "modules" / "reference"
    for md in sorted(refs.glob("*.md")):
        try:
            txt = md.read_text(encoding="utf-8")
        except Exception:
            continue
        for ln_idx, line in enumerate(txt.splitlines(), start=1):
            # Skip link-anchor lines: [text](path.md#anchor) — noisy.
            if "](" in line and ".md" in line:
                continue
            for m in FQN_RX.finditer(line):
                mod, act = m.group(1), m.group(2)
                if mod in SKIP_MODULE_TOKENS:
                    continue
                # Skip internal attributes (underscore-prefixed) — not actions.
                if act.startswith("_"):
                    continue
                mentions.append((md.relative_to(ROOT), ln_idx, f"{mod}.{act}"))
    return mentions


@dataclass
class ActionAuditResult:
    unknown: list[tuple[Path, int, str]] = field(default_factory=list)
    total_mentions: int = 0


def audit_actions() -> ActionAuditResult:
    code_actions = scan_code_actions()
    mentions = scan_doc_action_mentions()
    out = ActionAuditResult(total_mentions=len(mentions))
    for path, line, fqn in mentions:
        mod, act = fqn.split(".", 1)
        if mod not in code_actions:
            # Module not in code at all — may be fine (provider name, file ext, etc.)
            continue
        if act not in code_actions[mod]:
            out.unknown.append((path, line, fqn))
    return out


# ── 4. REST route audit ────────────────────────────────────────

@dataclass
class RouteAuditResult:
    missing_in_code: list[str] = field(default_factory=list)   # doc says it, code doesn't have it
    extra_in_code: list[str] = field(default_factory=list)     # code has it, doc doesn't
    doc_count: int = 0
    code_count: int = 0


_ROUTER_PREFIX_RX = re.compile(
    r"^\s*(?P<var>[a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*APIRouter\([^)]*prefix\s*=\s*\"(?P<prefix>[^\"]*)\"",
    re.MULTILINE,
)


def scan_code_routes() -> set[tuple[str, str]]:
    """Return {(METHOD, full_path)} from every @router.* or @app.* decorator.

    Handles multiple routers per file (each with its own prefix) and app-level
    routes without a prefix.
    """
    routes: set[tuple[str, str]] = set()
    scan_dirs = [API_DIR, PACKAGES / "core"]
    scanned: set[Path] = set()
    for base in scan_dirs:
        for py in sorted(base.rglob("*.py")):
            if py in scanned:
                continue
            scanned.add(py)
            try:
                txt = py.read_text(encoding="utf-8")
                tree = ast.parse(txt)
            except Exception:
                continue
            # Collect every `name = APIRouter(prefix="...")` in the file.
            router_prefixes: dict[str, str] = {}
            for m in _ROUTER_PREFIX_RX.finditer(txt):
                router_prefixes[m.group("var")] = m.group("prefix")
            # Also catch prefix-less routers: `name = APIRouter(` without a prefix kwarg.
            for m in re.finditer(
                r"^\s*(?P<var>[a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*APIRouter\(",
                txt,
                re.MULTILINE,
            ):
                var = m.group("var")
                if var not in router_prefixes:
                    router_prefixes[var] = ""

            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    if not isinstance(dec, ast.Call):
                        continue
                    func = dec.func
                    method = None
                    obj = None
                    if isinstance(func, ast.Attribute) and func.attr in {
                        "get", "post", "put", "patch", "delete", "head", "options"
                    }:
                        method = func.attr.upper()
                        if isinstance(func.value, ast.Name):
                            obj = func.value.id
                    if not method or not dec.args:
                        continue
                    if not isinstance(dec.args[0], ast.Constant):
                        continue
                    path = dec.args[0].value
                    if obj in router_prefixes:
                        path = router_prefixes[obj] + path
                    # @app.* → absolute path, leave as-is.
                    routes.add((method, path))
    return routes


DOC_ROUTE_RX = re.compile(
    r"\|\s*`([^`]+)`\s*\|\s*([A-Z][A-Z/]*)\s*\|", re.MULTILINE
)


def scan_doc_routes() -> set[tuple[str, str]]:
    rest = DOCS / "protocol" / "REST_API.md"
    try:
        txt = rest.read_text(encoding="utf-8")
    except Exception:
        return set()
    routes: set[tuple[str, str]] = set()
    for m in DOC_ROUTE_RX.finditer(txt):
        path = m.group(1).strip()
        methods = m.group(2).strip()
        # A cell like `GET/PUT/DELETE` → split into three.
        for method in methods.split("/"):
            if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                # Normalize `{id}` → `{xxx}` pattern noise — keep as-is for now.
                routes.add((method, path))
    return routes


def _normalize_path(p: str) -> str:
    """Collapse differing path variable names so fuzzy match works.

    Doc often uses `{id}` / `{sid}` while code uses `{app_id}` / `{session_id}`.
    """
    return re.sub(r"\{[^}]+\}", "{}", p)


def audit_routes() -> RouteAuditResult:
    code = scan_code_routes()
    doc = scan_doc_routes()
    out = RouteAuditResult(doc_count=len(doc), code_count=len(code))

    code_norm = {(m, _normalize_path(p)) for m, p in code}
    doc_norm = {(m, _normalize_path(p)) for m, p in doc}

    for m, p in sorted(doc):
        if (m, _normalize_path(p)) not in code_norm:
            out.missing_in_code.append(f"{m:6} {p}")

    for m, p in sorted(code):
        np = _normalize_path(p)
        if (m, np) not in doc_norm:
            out.extra_in_code.append(f"{m:6} {p}")
    return out


# ── Report ────────────────────────────────────────────────────

def write_report(
    yaml_blocks: list[YamlBlock],
    compile_results: list[CompileResult],
    action_result: ActionAuditResult,
    route_result: RouteAuditResult,
) -> Path:
    lines: list[str] = []
    lines.append("# Documentation Validation Report")
    lines.append("")
    lines.append(f"_Generated by `tools/validate_docs.py`. Regenerate any time._")
    lines.append("")

    # Summary
    total = len(compile_results)
    passed = sum(1 for r in compile_results if r.ok)
    failed = total - passed
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- YAML blocks extracted: **{len(yaml_blocks)}**")
    lines.append(f"- Full-app YAMLs compiled: **{total}**  → pass: **{passed}**  fail: **{failed}**")
    lines.append(f"- Action mentions scanned: **{action_result.total_mentions}**  → unknown: **{len(action_result.unknown)}**")
    lines.append(f"- REST routes: doc **{route_result.doc_count}** vs code **{route_result.code_count}**  → doc-only: **{len(route_result.missing_in_code)}**, code-only: **{len(route_result.extra_in_code)}**")
    lines.append("")

    # (1) compile failures
    lines.append("## 1. YAML Compile Failures")
    lines.append("")
    if failed == 0:
        lines.append("All full-app YAMLs compile. ✅")
    else:
        lines.append("| Doc | Line | Error |")
        lines.append("|---|---|---|")
        for r in compile_results:
            if r.ok:
                continue
            err = r.error.replace("|", "\\|").replace("\n", " ")[:250]
            lines.append(f"| `{r.block.path}` | {r.block.line} | {err} |")
    lines.append("")

    # (2) unknown actions
    lines.append("## 2. Unknown `module.action` References")
    lines.append("")
    if not action_result.unknown:
        lines.append("Every `module.action` reference in module reference docs maps to a real `@action`. ✅")
    else:
        lines.append("| Doc | Line | Reference |")
        lines.append("|---|---|---|")
        for path, line, fqn in action_result.unknown:
            lines.append(f"| `{path}` | {line} | `{fqn}` |")
    lines.append("")

    # (3) routes
    lines.append("## 3. REST Routes")
    lines.append("")
    if not route_result.missing_in_code and not route_result.extra_in_code:
        lines.append("REST_API.md routes match code exactly. ✅")
    else:
        if route_result.missing_in_code:
            lines.append("### Documented but not in code")
            lines.append("")
            lines.append("```")
            for r in route_result.missing_in_code:
                lines.append(r)
            lines.append("```")
            lines.append("")
        if route_result.extra_in_code:
            lines.append("### In code but not documented")
            lines.append("")
            lines.append("```")
            for r in route_result.extra_in_code[:100]:
                lines.append(r)
            if len(route_result.extra_in_code) > 100:
                lines.append(f"... ({len(route_result.extra_in_code) - 100} more)")
            lines.append("```")
            lines.append("")

    out = DOCS / "VALIDATION_REPORT.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yaml", action="store_true", help="run YAML extraction + compile only")
    ap.add_argument("--actions", action="store_true", help="run action-name audit only")
    ap.add_argument("--routes", action="store_true", help="run REST route audit only")
    args = ap.parse_args()

    run_all = not (args.yaml or args.actions or args.routes)

    print("-> extracting YAML blocks...", flush=True)
    blocks = extract_yaml_blocks() if (run_all or args.yaml) else []
    full_apps = [b for b in blocks if b.is_full_app]
    print(f"   {len(blocks)} blocks, {len(full_apps)} full-app", flush=True)

    print("-> compiling full-app YAMLs...", flush=True)
    compile_results = compile_blocks(blocks) if (run_all or args.yaml) else []
    print(f"   pass={sum(1 for r in compile_results if r.ok)} fail={sum(1 for r in compile_results if not r.ok)}", flush=True)

    print("-> auditing action names...", flush=True)
    action_result = audit_actions() if (run_all or args.actions) else ActionAuditResult()
    print(f"   mentions={action_result.total_mentions}  unknown={len(action_result.unknown)}", flush=True)

    print("-> auditing REST routes...", flush=True)
    route_result = audit_routes() if (run_all or args.routes) else RouteAuditResult()
    print(f"   doc={route_result.doc_count}  code={route_result.code_count}  doc-only={len(route_result.missing_in_code)}  code-only={len(route_result.extra_in_code)}", flush=True)

    report = write_report(blocks, compile_results, action_result, route_result)
    print(f"-> wrote {report.relative_to(ROOT)}", flush=True)

    # Exit non-zero if anything failed — useful for CI.
    bad = (
        sum(1 for r in compile_results if not r.ok)
        + len(action_result.unknown)
        + len(route_result.missing_in_code)
    )
    return 1 if bad > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
