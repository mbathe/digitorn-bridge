"""export_openapi.py — dump the FastAPI OpenAPI schema without starting the daemon.

Constructs the FastAPI app in-process (``create_app`` is pure — no lifespan,
no port bind), calls ``app.openapi()``, writes the resulting dict to
``tools/openapi.json``. Used by ``export_bruno.py`` to generate the full
collection without requiring the daemon to be running or
``server.expose_docs`` to be set.

Usage::

    py -3.12 tools/export_openapi.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

# Silence the noisy module_loaded logs during construction.
logging.basicConfig(level=logging.ERROR)
logging.getLogger("digitorn").setLevel(logging.ERROR)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGES = REPO_ROOT / "packages"
if str(PACKAGES) not in sys.path:
    sys.path.insert(0, str(PACKAGES))

OUT_PATH = REPO_ROOT / "tools" / "openapi.json"


def _build_schema_manually(app: Any) -> dict:
    """Walk app.routes directly when app.openapi() refuses to serialize.

    Produces a minimal OpenAPI 3.0 document with path / method / tags /
    summary / request body schema where available. Response types are
    omitted — we only need input shapes for Bruno request generation.
    """
    from fastapi.routing import APIRoute
    paths: dict[str, dict] = {}
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        path = r.path
        entry = paths.setdefault(path, {})
        for method in r.methods:
            m = method.lower()
            if m not in {"get", "post", "put", "patch", "delete"}:
                continue
            op: dict = {
                "tags": list(r.tags) if r.tags else ["untagged"],
                "summary": r.summary or r.name or "",
                "description": (r.description or "").strip(),
                "operationId": r.operation_id or f"{m}_{r.name}",
                "parameters": [],
            }
            # Path + query params
            for p in r.dependant.path_params:
                op["parameters"].append({
                    "name": p.name, "in": "path", "required": True,
                    "schema": {"type": "string"},
                })
            for p in r.dependant.query_params:
                op["parameters"].append({
                    "name": p.name, "in": "query", "required": p.required,
                    "schema": {"type": "string"},
                })
            # Request body schema
            if r.body_field is not None:
                try:
                    body_schema = r.body_field.field_info.annotation.model_json_schema()
                except Exception:
                    body_schema = {"type": "object"}
                op["requestBody"] = {
                    "required": True,
                    "content": {
                        "application/json": {"schema": body_schema},
                    },
                }
            entry[m] = op
    return {
        "openapi": "3.0.2",
        "info": {"title": "Digitorn", "version": "1.0.0"},
        "paths": paths,
    }


def main() -> None:
    from digitorn.core.server import create_app

    print("[export_openapi] constructing FastAPI app...", file=sys.stderr)
    asgi = create_app()
    # Socket.IO wraps FastAPI — unwrap to call openapi().
    app = getattr(asgi, "other_asgi_app", None) or asgi

    # Resolve all forward refs before generating the schema. FastAPI's
    # endpoints that reference ``Response`` / ``StreamingResponse`` via
    # ForwardRef won't serialize otherwise.
    try:
        from fastapi import Response  # noqa: F401
        from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, RedirectResponse  # noqa: F401
        import pydantic
        pydantic.TypeAdapter.rebuild = getattr(pydantic.TypeAdapter, "rebuild", lambda self: None)
    except Exception:
        pass

    # FastAPI builds the schema lazily. Force regeneration so all routes
    # registered after construction are visible.
    if hasattr(app, "openapi_schema"):
        app.openapi_schema = None

    try:
        schema = app.openapi()
    except Exception as exc:
        # Fallback: iterate routes and build a minimal schema ourselves.
        # This captures every path/method/request-body even when FastAPI
        # can't fully resolve response types.
        print(
            f"[export_openapi] app.openapi() failed: {exc}; falling back to manual introspection",
            file=sys.stderr,
        )
        schema = _build_schema_manually(app)
    paths = schema.get("paths", {})
    op_count = sum(
        1 for methods in paths.values() for m in methods if m.lower() in {"get", "post", "put", "patch", "delete"}
    )
    print(
        f"[export_openapi] {len(paths)} path(s), {op_count} operation(s)",
        file=sys.stderr,
    )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    print(f"[export_openapi] wrote {OUT_PATH.relative_to(REPO_ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
