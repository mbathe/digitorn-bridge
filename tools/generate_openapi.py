"""Generate a static openapi.json for the daemon's REST surface.

Usage:
    py -3.12 tools/generate_openapi.py [output_path]

Default output: docs/openapi.json

The resulting file can be:
- opened in Swagger UI (swagger.io editor, local Docker, etc.)
- imported in Postman / Insomnia / Bruno as a collection
- consumed by client-generators (openapi-generator, openapi-typescript)

Nothing is read from the running daemon — the schema is materialised
straight from the FastAPI app object by importing the route modules.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))

from fastapi import FastAPI

# Import all routers that the real server.py mounts.
from digitorn.core.api.apps import router as apps_router
from digitorn.core.api.auth import router as auth_router
from digitorn.core.api.user import router as user_router
from digitorn.core.api.packages import router as packages_router
from digitorn.core.api.discovery import router as discovery_router
from digitorn.core.api.credentials import router as credentials_router
from digitorn.core.api.builder import router as builder_router
from digitorn.core.api.mcp import router as mcp_router
from digitorn.core.api.modules import router as modules_router
from digitorn.core.api.requires import router as requires_router
from digitorn.core.api.security import router as security_router
from digitorn.core.api.transcribe import router as transcribe_router
from digitorn.core.api.config import router as config_router
from digitorn.core.api.ui import router as ui_router


def build_app() -> FastAPI:
    """Build a FastAPI app identical to the one served by the daemon,
    minus the middleware (we only care about routes for the schema)."""
    app = FastAPI(
        title="Digitorn",
        description=(
            "Modular agent OS — declarative AI agent framework built on "
            "YAML. This schema is auto-generated from the daemon's route "
            "handlers and Pydantic models."
        ),
        version="1.0.0",
    )
    app.include_router(auth_router)
    app.include_router(apps_router)
    app.include_router(user_router)
    app.include_router(packages_router)
    app.include_router(discovery_router)
    app.include_router(credentials_router)
    app.include_router(builder_router)
    app.include_router(mcp_router)
    app.include_router(modules_router)
    app.include_router(requires_router)
    app.include_router(security_router)
    app.include_router(transcribe_router)
    app.include_router(config_router)
    app.include_router(ui_router)
    return app


def main() -> None:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        ROOT / "docs" / "openapi.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    app = build_app()
    schema = app.openapi()
    output.write_text(json.dumps(schema, indent=2), encoding="utf-8")

    # Short report
    paths = schema.get("paths", {})
    schemas = schema.get("components", {}).get("schemas", {})
    total_ops = sum(
        1 for ops in paths.values() for m in ops
        if m in ("get", "post", "put", "delete", "patch", "head")
    )
    print(f"Wrote {output}")
    print(f"  paths:       {len(paths)}")
    print(f"  operations:  {total_ops}")
    print(f"  pydantic schemas: {len(schemas)}")
    print()
    print("Next steps:")
    print(f"  - Swagger UI:  open https://editor.swagger.io/ and paste the file")
    print(f"  - Postman:     Import → File → {output}")
    print(f"  - Local view:  `npx @scalar/cli serve {output}` or swagger-ui-watcher")


if __name__ == "__main__":
    main()
