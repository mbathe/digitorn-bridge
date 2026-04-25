"""Verify that my @model_validator fix + the defensive JSON sanitizer
in validation_exception_handler combine to produce 422, not 500, on
rejected audio fields. This reproduces the exact handler chain the
daemon uses in production.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from digitorn.core.api.apps import SessionMessageRequest  # noqa: E402


def _build_app() -> FastAPI:
    """Stand-alone app that re-registers the same exception handlers
    the real daemon installs in ``server.py``. This lets us test the
    full validation→handler→JSONResponse chain without booting the
    full lifespan (which is slow and pulls in DB/auth/etc)."""

    app = FastAPI()

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if isinstance(exc.detail, dict):
            body = {"success": False, "detail": exc.detail, "status_code": exc.status_code}
        else:
            body = {"success": False, "error": exc.detail, "detail": exc.detail, "status_code": exc.status_code}
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        raw_errors = exc.errors()
        details: list[dict[str, Any]] = []
        for err in raw_errors:
            safe: dict[str, Any] = {}
            for k, v in err.items():
                try:
                    json.dumps(v)
                    safe[k] = v
                except (TypeError, ValueError):
                    safe[k] = repr(v)[:500]
            details.append(safe)
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": "Validation error",
                "details": details,
                "status_code": 422,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Internal server error"},
        )

    @app.post("/messages")
    def post_messages(body: SessionMessageRequest):
        return {"ok": True, "message": body.message}

    return app


def run() -> int:
    failures: list[str] = []
    client = TestClient(_build_app())

    # 1. Normal message → 200
    r = client.post("/messages", json={"message": "hi"})
    if r.status_code != 200:
        failures.append(f"normal: {r.status_code} {r.text[:200]}")

    # 2. Unknown field tolerated → 200
    r = client.post("/messages", json={"message": "hi", "metadata": {"x": 1}})
    if r.status_code != 200:
        failures.append(f"unknown-field: {r.status_code} {r.text[:200]}")

    # 3. audio → 422 (the post-daemon-restart expectation)
    r = client.post("/messages", json={"message": "hi", "audio": {"data": "x"}})
    if r.status_code != 422:
        failures.append(f"audio: expected 422 got {r.status_code} {r.text[:300]}")
    else:
        body = r.json()
        if body.get("error") != "Validation error":
            failures.append(f"audio: body.error mismatch: {body}")
        # Details must be fully JSON-serialisable (the whole point of
        # the sanitizer).
        try:
            json.dumps(body)
        except (TypeError, ValueError) as exc:
            failures.append(f"audio: response not JSON-serialisable: {exc}")

    # 4. audios (plural) → 422
    r = client.post("/messages", json={"message": "hi", "audios": [{"data": "x"}]})
    if r.status_code != 422:
        failures.append(f"audios: expected 422 got {r.status_code}")

    # 5. audio with empty value (None / empty dict) → 200 (validator
    #    short-circuits on empty values so a client that sends
    #    ``audio: null`` alongside its text still works)
    r = client.post("/messages", json={"message": "hi", "audio": None})
    if r.status_code != 200:
        failures.append(f"audio-null: expected 200 got {r.status_code}")

    # 6. Oversize message → 422 (Pydantic ``max_length``). The server
    #    middleware catches >2 MiB at 413, but the ≤2 MiB / >1 MiB slice
    #    hits Pydantic.
    big = "x" * (1_200_000)  # > 1 MiB cap in SessionMessageRequest
    r = client.post("/messages", json={"message": big})
    if r.status_code != 422:
        failures.append(f"oversize: expected 422 got {r.status_code}")

    if failures:
        print("FAIL — exception handler regression:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — validation handler returns 422 + JSON-serialisable body on audio/extra/oversize")
    return 0


if __name__ == "__main__":
    sys.exit(run())
