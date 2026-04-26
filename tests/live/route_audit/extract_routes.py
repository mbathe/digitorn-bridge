"""Extract every ``@router.<method>(path)`` from ``core/api/*.py``.

Produces ``routes_manifest.json`` with one entry per route:
    {
      "file": "apps.py",
      "router_prefix": "/api/apps",
      "method": "POST",
      "path": "/{app_id}/sessions/{session_id}/messages",
      "full_path": "/api/apps/{app_id}/sessions/{session_id}/messages",
      "handler": "session_send_message",
      "line": 1396,
    }

This is the starting point. Every route WILL be tested live by the
harness — no exception. The manifest is rejouable; a CI can diff the
manifest against the last audit to spot new routes added since.
"""
from __future__ import annotations
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
API_DIR = ROOT / "packages" / "digitorn" / "core" / "api"


def _discover_routers(tree: ast.Module) -> dict[str, str]:
    """Find every ``<name> = APIRouter(prefix="...")`` in the module.

    Returns ``{router_var_name: prefix}`` so we can later match
    ``@<name>.get(...)`` decorators against their actual prefix. This
    lets the extractor pick up routes registered on ``admin_router``,
    ``oauth_callback_router``, or any other named router — not just
    the conventional ``router`` var.
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        name = node.targets[0].id
        val = node.value
        if not isinstance(val, ast.Call):
            continue
        call_name = None
        if isinstance(val.func, ast.Name):
            call_name = val.func.id
        elif isinstance(val.func, ast.Attribute):
            call_name = val.func.attr
        if call_name != "APIRouter":
            continue
        prefix = ""
        for kw in val.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                prefix = kw.value.value or ""
                break
        out[name] = prefix
    return out


def _extract(py_path: Path) -> list[dict]:
    src = py_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    routers = _discover_routers(tree)
    if not routers:
        return []
    routes: list[dict] = []

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            self._scan(node)
        def visit_AsyncFunctionDef(self, node):
            self._scan(node)
        def _scan(self, node):
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call):
                    continue
                func = deco.func
                if not (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id in routers
                ):
                    continue
                method = func.attr.upper()
                if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    continue
                path = None
                if deco.args and isinstance(deco.args[0], ast.Constant):
                    path = deco.args[0].value
                if path is None:
                    continue
                prefix = routers[func.value.id]
                routes.append({
                    "file": py_path.name,
                    "router": func.value.id,
                    "router_prefix": prefix,
                    "method": method,
                    "path": path,
                    "full_path": prefix + path if path else prefix,
                    "handler": node.name,
                    "line": node.lineno,
                })

    _Visitor().visit(tree)
    return routes


def main() -> int:
    all_routes: list[dict] = []
    for py in sorted(API_DIR.glob("*.py")):
        if py.name.startswith("_"):
            continue
        try:
            all_routes.extend(_extract(py))
        except SyntaxError as exc:
            print(f"[skip] {py.name}: {exc}", file=sys.stderr)

    out = Path(__file__).parent / "routes_manifest.json"
    out.write_text(json.dumps(all_routes, indent=2), encoding="utf-8")

    print(f"Extracted {len(all_routes)} routes from {API_DIR}")
    print(f"Manifest written to {out}")

    # Per-file summary for quick visual.
    by_file: dict[str, int] = {}
    for r in all_routes:
        by_file[r["file"]] = by_file.get(r["file"], 0) + 1
    for f, n in sorted(by_file.items(), key=lambda x: -x[1]):
        print(f"  {f}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
