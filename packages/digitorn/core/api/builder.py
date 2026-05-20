"""Builder API - CRUD for App Builder drafts."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from digitorn.core.api.apps_v2 import AppResponse, _get_manager
from digitorn.core.app.build_draft_store import (
    BuildDraftStore,
    DraftLimitExceeded,
    MAX_DRAFTS_PER_USER,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/builder", tags=["builder"])


# Dependencies


def _get_user_id(request: Request) -> str:
    """Read the authenticated user id from the request state."""
    return getattr(request.state, "user_id", None) or "anonymous"


def _get_draft_store(request: Request) -> BuildDraftStore:
    """Get or create the singleton `BuildDraftStore` on the AppManager."""
    manager = _get_manager(request)
    store = getattr(manager, "_build_draft_store", None)
    if store is None:
        from digitorn.core.database import get_session_factory
        store = BuildDraftStore(get_session_factory())
        manager._build_draft_store = store
    return store


# Request bodies


class DraftCreateRequest(BaseModel):
    """POST /api/builder/drafts body."""

    name: str = Field(default="Untitled draft", max_length=255)
    initial_yaml: str = Field(default="", description="Optional starter YAML")
    builder_state: dict[str, Any] | None = Field(
        default=None,
        description="Optional initial state-machine bookkeeping for the builder agent",
    )


class DraftPatchRequest(BaseModel):
    """PATCH /api/builder/drafts/{id} body - every field is optional."""

    name: str | None = Field(default=None, max_length=255)
    status: str | None = Field(
        default=None,
        description="One of: in_progress, compiled, deployed, abandoned",
    )
    current_yaml: str | None = None
    chat_history: list[dict[str, Any]] | None = None
    builder_state: dict[str, Any] | None = None
    append_messages: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Append these messages to chat_history without sending the full list. "
            "Mutually compatible with chat_history (both apply, append happens after replace)."
        ),
    )


# Routes


@router.get("/drafts", response_model=AppResponse)
async def list_drafts(
    request: Request,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AppResponse:
    """List the caller's drafts, most recently updated first."""
    user_id = _get_user_id(request)
    store = _get_draft_store(request)
    drafts = await store.list_for_user(
        user_id, status=status, limit=limit, offset=offset,
    )
    count = await store.count_for_user(user_id)
    return AppResponse(
        success=True,
        data={
            "drafts": drafts,
            "returned": len(drafts),
            "total_for_user": count,
            "max_per_user": MAX_DRAFTS_PER_USER,
        },
    )


@router.post("/drafts", response_model=AppResponse)
async def create_draft(request: Request, body: DraftCreateRequest) -> AppResponse:
    """Create a new draft for the caller."""
    user_id = _get_user_id(request)
    store = _get_draft_store(request)
    try:
        draft = await store.create(
            user_id,
            name=body.name,
            initial_yaml=body.initial_yaml,
            builder_state=body.builder_state,
        )
    except DraftLimitExceeded as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return AppResponse(success=True, data=draft)


@router.get("/drafts/{draft_id}", response_model=AppResponse)
async def get_draft(request: Request, draft_id: str) -> AppResponse:
    """Resume a draft - returns its full state (yaml, history, builder_state)."""
    user_id = _get_user_id(request)
    store = _get_draft_store(request)
    draft = await store.get(draft_id, user_id=user_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    return AppResponse(success=True, data=draft)


@router.patch("/drafts/{draft_id}", response_model=AppResponse)
async def patch_draft(
    request: Request, draft_id: str, body: DraftPatchRequest,
) -> AppResponse:
    """Update one or more fields on a draft."""
    user_id = _get_user_id(request)
    store = _get_draft_store(request)

    updated = await store.update(
        draft_id,
        user_id=user_id,
        name=body.name,
        status=body.status,
        current_yaml=body.current_yaml,
        chat_history=body.chat_history,
        builder_state=body.builder_state,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Draft not found")

    if body.append_messages:
        updated = await store.append_chat_messages(
            draft_id, body.append_messages, user_id=user_id,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Draft not found")

    return AppResponse(success=True, data=updated)


@router.delete("/drafts/{draft_id}", response_model=AppResponse)
async def delete_draft(request: Request, draft_id: str) -> AppResponse:
    """Delete a draft (DB row + on-disk YAML directory)."""
    user_id = _get_user_id(request)
    store = _get_draft_store(request)
    ok = await store.delete(draft_id, user_id=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Draft not found")
    return AppResponse(success=True, data={"draft_id": draft_id, "deleted": True})


@router.post("/drafts/{draft_id}/deploy", response_model=AppResponse)
async def deploy_draft(request: Request, draft_id: str) -> AppResponse:
    """Compile the draft's current YAML and deploy it via the AppManager."""
    user_id = _get_user_id(request)
    store = _get_draft_store(request)
    draft = await store.get(draft_id, user_id=user_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")

    yaml_text = draft.get("current_yaml") or ""
    if not yaml_text.strip():
        raise HTTPException(status_code=400, detail="Draft has no YAML to deploy")

    manager = _get_manager(request)
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="Module registry not initialized")

    # Compile first - if this fails we DON'T deploy, we surface the
    # errors so the builder agent can fix them and try again.
    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.app.errors import AppCompilationError

    compiler = AppYAMLCompiler(registry)
    try:
        compiled = compiler.compile_string(
            yaml_text, source=f"draft:{draft_id}",
        )
    except AppCompilationError as exc:
        msg = str(exc)
        errors = []
        if ":" in msg:
            tail = msg.split(":", 1)[1]
            errors = [e.strip() for e in tail.split(";") if e.strip()]
        else:
            errors = [msg]
        return AppResponse(
            success=False,
            data={"valid": False, "errors": errors},
            error="Draft did not compile - fix the errors and retry.",
        )
    except Exception as exc:
        return AppResponse(
            success=False,
            data={"valid": False, "errors": [f"{type(exc).__name__}: {exc}"]},
            error="Compilation crashed unexpectedly.",
        )

    from pathlib import Path as _Path

    yaml_path_str = draft.get("yaml_path") or ""
    if not yaml_path_str or not _Path(yaml_path_str).is_file():
        raise HTTPException(
            status_code=500,
            detail="Draft YAML file is missing on disk; cannot deploy.",
        )

    try:
        deployed = await manager.deploy(_Path(yaml_path_str))
        deployed_app_id = getattr(deployed, "app_id", "") or compiled.meta.app_id
    except Exception as exc:
        logger.exception("draft deploy failed for %s", draft_id)
        raise HTTPException(
            status_code=500,
            detail=f"Deploy failed after successful compile: {exc}",
        )

    # Mark the draft as deployed so the dashboard can link draft ↔ app.
    updated = await store.update(
        draft_id,
        user_id=user_id,
        status="deployed",
        deployed_app_id=deployed_app_id,
    )

    return AppResponse(
        success=True,
        data={
            "draft_id": draft_id,
            "deployed_app_id": deployed_app_id,
            "draft": updated,
        },
    )
