"""Template routes — gallery list + preview static-asset serve.

The chat client fetches `GET /api/apps/{app_id}/templates` to render a
native gallery (cards under the composer). Clicking a card opens a
modal whose iframe loads
`GET /api/apps/{app_id}/template-assets/<preview_path>` — a stock
static-file server scoped to the app's install dir.

Two routes, both auth-required (handled by the global middleware) but
**no extra permission gate**: template metadata + previews are inert
public-facing assets, akin to the app's icon. Mutation is impossible
through this layer — there's nothing to mutate.

The third piece of the template system (applying a template to a
session: copy seeds into the workspace + inject the system addendum)
rides on the existing message-send pipeline; see ``messages.py``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import FileResponse, Response

from ._shared import (
    AppResponse,
    _get_deployed,
    _raise_not_deployed,
    _validate_id,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["apps"])


async def _resolve_install_dir(
    request: Request, app_id: str, deployed: Any,
) -> Path | None:
    """Find the on-disk install dir for the deployed app.

    Mirrors ``web_static`` in ``preview.py``: prefer the compiled
    ``source_path`` parent when known, fall back to the package
    registry resolver.
    """
    from digitorn.core.packages.resolver import resolve_app_install_dir

    source_path = (
        getattr(deployed.compiled, "source_path", None)
        if hasattr(deployed, "compiled")
        else None
    )
    if source_path is not None:
        candidate = Path(source_path).parent
        if candidate.is_dir():
            return candidate

    user_id = getattr(request.state, "user_id", None)
    pkg_registry = getattr(request.app.state, "package_registry", None)
    install_dir = await resolve_app_install_dir(
        app_id, user_id=user_id, registry=pkg_registry,
    )
    if install_dir is None:
        return None
    p = Path(install_dir)
    return p if p.is_dir() else None


@router.get("/{app_id}/templates", response_model=AppResponse)
async def list_templates(request: Request, app_id: str) -> AppResponse:
    """List the starter templates declared by the app.

    Returns one entry per template with the minimal info the chat
    client needs to render a gallery card: ``id``, ``name``,
    ``description``, and ``preview_url`` (absolute URL the iframe can
    load directly). The ``system_prompt`` and ``seed_dir`` are kept
    private — they're applied server-side when the user sends a
    message with this template attached, and there is no UX reason to
    expose them to the client.

    Empty list when the app declares no ``templates`` block; not 404.
    The chat client shows the gallery slot only when the list is
    non-empty, so an empty response collapses the slot gracefully.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)

    templates = getattr(deployed.compiled, "templates", None) or []
    out: list[dict[str, Any]] = []
    for t in templates:
        out.append({
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "preview_url": (
                f"/api/apps/{app_id}/template-assets/{t.preview_path}"
            ),
        })
    return AppResponse(
        success=True,
        data={"app_id": app_id, "templates": out, "count": len(out)},
    )


_CACHEABLE_EXTENSIONS = {
    ".js", ".mjs", ".css", ".png", ".jpg", ".jpeg", ".webp",
    ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".otf",
}


@router.api_route(
    "/{app_id}/template-assets/{path:path}",
    methods=["GET", "HEAD"],
)
async def template_assets(request: Request, app_id: str, path: str):
    """Serve a static file from the app's install directory.

    Path is interpreted relative to ``install_dir``. The expected
    layout is::

        install_dir/
          templates/<id>/dist/index.html   ← preview entry (Vite build)
          templates/<id>/dist/assets/*.js  ← bundled chunks
          templates/<id>/files/...         ← seed files (NOT served here;
                                              copied into the workspace
                                              at template-apply time)

    Sandbox: any path that resolves outside ``install_dir`` is
    rejected with 403. The schema's ``preview_path`` validator already
    blocks ``..`` segments at deploy time; this is the second line of
    defense for arbitrary URL paths.

    Cache: hashed build artefacts (.js / .css / images / fonts) get a
    long-lived cache header. ``index.html`` and other HTML never cache
    so iframe reloads always see fresh content.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)

    install_dir = await _resolve_install_dir(request, app_id, deployed)
    if install_dir is None:
        raise HTTPException(
            status_code=404,
            detail=f"install dir not found for app '{app_id}'",
        )

    rel = (path or "").lstrip("/")
    if not rel:
        # Empty path - no implicit index. Caller must address a file.
        return Response(status_code=404)

    target = (install_dir / rel).resolve()
    try:
        target.relative_to(install_dir.resolve())
    except ValueError:
        return Response(status_code=403)

    if not target.is_file():
        return Response(status_code=404)

    headers: dict[str, str] = {}
    suffix = target.suffix.lower()
    if suffix == ".html" or suffix == ".htm":
        headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        # Inject a premium scrollbar stylesheet so every template's
        # iframe gets the same thin, themed scrollbar inset from the
        # content edge — without touching the templates' source files.
        # GET only: HEAD requests must not carry a body.
        #
        # Off-loaded to a worker thread: ``read_text`` + the CSS
        # injection's ``html.lower()`` + ``find('</head>')`` both
        # walk the whole HTML buffer, and for large index.html files
        # (Lovable / builder bundles can be 100+ KB pre-hydration)
        # the lower/find pass blocks the main loop. Threadpool I/O
        # releases the GIL for the disk read; the lower/find is small
        # enough to be acceptable inline post-read.
        if request.method == "GET":
            def _read_and_patch() -> str | None:
                try:
                    return _inject_scrollbar_css(
                        target.read_text(encoding="utf-8"),
                    )
                except (OSError, UnicodeDecodeError):
                    return None
            patched = await asyncio.to_thread(_read_and_patch)
            if patched is None:
                return FileResponse(str(target), headers=headers)
            return Response(
                content=patched,
                media_type="text/html; charset=utf-8",
                headers=headers,
            )
    elif suffix in _CACHEABLE_EXTENSIONS:
        # Built artefacts are content-hashed by Vite; immutable.
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return FileResponse(str(target), headers=headers)


# Premium scrollbar styles for template-asset HTML responses. Injected
# at serve time so every template iframe inherits the same thin
# floating scrollbar with a transparent gutter — no template source
# edit, no per-template CSS asset to ship.
_SCROLLBAR_STYLE: str = """\
<style data-injected="digitorn-template-scrollbar">
  html {
    scrollbar-width: thin;
    scrollbar-color: rgba(120, 124, 138, 0.45) transparent;
    scrollbar-gutter: stable;
  }
  html::-webkit-scrollbar {
    width: 12px;
    height: 12px;
  }
  html::-webkit-scrollbar-track { background: transparent; }
  html::-webkit-scrollbar-thumb {
    background-color: rgba(120, 124, 138, 0.45);
    border-radius: 10px;
    /* Transparent border + padding-box clip = visible gap between the
       thumb and the iframe edge so the scrollbar reads as "floating"
       and offset from the preview content. */
    border: 3px solid transparent;
    background-clip: padding-box;
    transition: background-color 160ms ease;
  }
  html::-webkit-scrollbar-thumb:hover {
    background-color: rgba(120, 124, 138, 0.7);
  }
  html::-webkit-scrollbar-corner { background: transparent; }
</style>
"""


def _inject_scrollbar_css(html: str) -> str:
    """Insert the premium scrollbar stylesheet just before ``</head>``.

    Falls back to the original HTML when there is no head tag (partial
    fragment, malformed template) so the response stays usable. The
    injection is idempotent — if the marker attribute is already
    present the original HTML is returned unchanged.
    """
    if 'data-injected="digitorn-template-scrollbar"' in html:
        return html
    idx = html.lower().find("</head>")
    if idx == -1:
        return html
    return html[:idx] + _SCROLLBAR_STYLE + html[idx:]
