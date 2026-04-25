"""Digitorn — Security profile CRUD routes.

    POST   /api/security/profiles              Create a security profile for an app
    GET    /api/security/profiles/{app_id}      Get a profile
    PATCH  /api/security/profiles/{app_id}      Update a profile
    DELETE /api/security/profiles/{app_id}      Delete a profile

    POST   /api/security/profiles/{app_id}/grants              Add a module grant
    GET    /api/security/profiles/{app_id}/grants               List module grants
    PUT    /api/security/profiles/{app_id}/grants/{module_id}  Upsert a module grant
    DELETE /api/security/profiles/{app_id}/grants/{module_id}  Remove a module grant
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

router = APIRouter(prefix="/api/security", tags=["security"])


class ProfileCreateRequest(BaseModel):
    """Create a security profile for an application."""

    app_id: str
    is_active: bool = True
    default_policy: str = "approve"
    granted_permissions: list[str] = []
    max_risk_level: str = "medium"
    risk_approval_rules: dict[str, str] = {
        "low": "auto",
        "medium": "approve",
        "high": "block",
    }
    approval_timeout: float = 300.0


class ProfileUpdateRequest(BaseModel):
    """Partial update of a security profile."""

    is_active: bool | None = None
    default_policy: str | None = None
    granted_permissions: list[str] | None = None
    max_risk_level: str | None = None
    risk_approval_rules: dict[str, str] | None = None
    approval_timeout: float | None = None


class GrantRequest(BaseModel):
    """Create or update a module grant."""

    module_id: str
    visibility: str = "full"
    default_action_policy: str = "approve"
    action_overrides: dict[str, str] = {}


VALID_POLICIES = {"auto", "approve", "block"}
VALID_RISK_LEVELS = {"low", "medium", "high"}
VALID_VISIBILITY = {"full", "hidden"}


def _validate_policy(value: str, field: str) -> None:
    if value not in VALID_POLICIES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {field}: '{value}'. Must be one of {sorted(VALID_POLICIES)}.",
        )


def _validate_risk(value: str) -> None:
    if value not in VALID_RISK_LEVELS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid risk level: '{value}'. Must be one of {sorted(VALID_RISK_LEVELS)}.",
        )


async def _get_profile(app_id: str):
    """Fetch AppProfile or raise 404."""
    from sqlalchemy import select

    from digitorn.core.database import _session_factory
    from digitorn.core.models import AppProfile

    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Database not initialized.")

    async with _session_factory() as session:
        result = await session.execute(
            select(AppProfile).where(AppProfile.app_id == app_id)
        )
        profile = result.scalar_one_or_none()
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"No security profile for app '{app_id}'.",
            )
        return profile, session


@router.post("/profiles", status_code=201)
async def create_profile(body: ProfileCreateRequest) -> dict[str, Any]:
    """Create a security profile for an application.

    The application must already exist in the ``applications`` table.
    """
    from sqlalchemy import select

    from digitorn.core.database import _session_factory
    from digitorn.core.models import AppProfile, Application

    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Database not initialized.")

    _validate_policy(body.default_policy, "default_policy")
    _validate_risk(body.max_risk_level)
    for level, policy in body.risk_approval_rules.items():
        _validate_risk(level)
        _validate_policy(policy, f"risk_approval_rules[{level}]")

    async with _session_factory() as session:
        app_result = await session.execute(
            select(Application).where(Application.app_id == body.app_id)
        )
        if app_result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=404,
                detail=f"Application '{body.app_id}' not found. Register it first.",
            )

        existing = await session.execute(
            select(AppProfile).where(AppProfile.app_id == body.app_id)
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Profile for app '{body.app_id}' already exists. Use PATCH to update.",
            )

        profile = AppProfile(
            app_id=body.app_id,
            is_active=body.is_active,
            default_policy=body.default_policy,
            granted_permissions=body.granted_permissions,
            max_risk_level=body.max_risk_level,
            risk_approval_rules=body.risk_approval_rules,
            approval_timeout=body.approval_timeout,
        )
        session.add(profile)
        await session.commit()
        await session.refresh(profile)

        return _profile_to_dict(profile)


@router.get("/profiles/{app_id}")
async def get_profile(app_id: str) -> dict[str, Any]:
    """Get the security profile for an application, including module grants."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from digitorn.core.database import _session_factory
    from digitorn.core.models import AppProfile

    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Database not initialized.")

    async with _session_factory() as session:
        result = await session.execute(
            select(AppProfile)
            .where(AppProfile.app_id == app_id)
            .options(selectinload(AppProfile.module_grants))
        )
        profile = result.scalar_one_or_none()
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"No security profile for app '{app_id}'.",
            )

        data = _profile_to_dict(profile)
        data["module_grants"] = [
            _grant_to_dict(g) for g in profile.module_grants
        ]
        return data


@router.patch("/profiles/{app_id}")
async def update_profile(app_id: str, body: ProfileUpdateRequest) -> dict[str, Any]:
    """Partially update a security profile."""
    from sqlalchemy import select

    from digitorn.core.database import _session_factory
    from digitorn.core.models import AppProfile

    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Database not initialized.")

    if body.default_policy is not None:
        _validate_policy(body.default_policy, "default_policy")
    if body.max_risk_level is not None:
        _validate_risk(body.max_risk_level)
    if body.risk_approval_rules is not None:
        for level, policy in body.risk_approval_rules.items():
            _validate_risk(level)
            _validate_policy(policy, f"risk_approval_rules[{level}]")

    async with _session_factory() as session:
        result = await session.execute(
            select(AppProfile).where(AppProfile.app_id == app_id)
        )
        profile = result.scalar_one_or_none()
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"No security profile for app '{app_id}'.",
            )

        if body.is_active is not None:
            profile.is_active = body.is_active
        if body.default_policy is not None:
            profile.default_policy = body.default_policy
        if body.granted_permissions is not None:
            profile.granted_permissions = body.granted_permissions
        if body.max_risk_level is not None:
            profile.max_risk_level = body.max_risk_level
        if body.risk_approval_rules is not None:
            profile.risk_approval_rules = body.risk_approval_rules
        if body.approval_timeout is not None:
            profile.approval_timeout = body.approval_timeout

        await session.commit()
        await session.refresh(profile)
        return _profile_to_dict(profile)


@router.delete("/profiles/{app_id}")
async def delete_profile(app_id: str) -> Response:
    """Delete a security profile and all its module grants."""
    from sqlalchemy import select

    from digitorn.core.database import _session_factory
    from digitorn.core.models import AppProfile

    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Database not initialized.")

    async with _session_factory() as session:
        result = await session.execute(
            select(AppProfile).where(AppProfile.app_id == app_id)
        )
        profile = result.scalar_one_or_none()
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"No security profile for app '{app_id}'.",
            )

        await session.delete(profile)
        await session.commit()

    return Response(status_code=204)


@router.get("/profiles/{app_id}/grants")
async def list_grants(app_id: str) -> dict[str, Any]:
    """List all module grants for an app's security profile."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from digitorn.core.database import _session_factory
    from digitorn.core.models import AppProfile

    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Database not initialized.")

    async with _session_factory() as session:
        result = await session.execute(
            select(AppProfile)
            .where(AppProfile.app_id == app_id)
            .options(selectinload(AppProfile.module_grants))
        )
        profile = result.scalar_one_or_none()
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"No security profile for app '{app_id}'.",
            )

        grants = [_grant_to_dict(g) for g in profile.module_grants]
        return {"app_id": app_id, "grants": grants, "count": len(grants)}


@router.put("/profiles/{app_id}/grants/{module_id}")
async def upsert_grant(
    app_id: str, module_id: str, body: GrantRequest,
) -> dict[str, Any]:
    """Create or update a module grant for an app's security profile."""
    from sqlalchemy import select

    from digitorn.core.database import _session_factory
    from digitorn.core.models import AppModuleGrant, AppProfile

    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Database not initialized.")

    if body.visibility not in VALID_VISIBILITY:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid visibility: '{body.visibility}'. Must be one of {sorted(VALID_VISIBILITY)}.",
        )
    _validate_policy(body.default_action_policy, "default_action_policy")
    for action, policy in body.action_overrides.items():
        _validate_policy(policy, f"action_overrides[{action}]")

    async with _session_factory() as session:
        result = await session.execute(
            select(AppProfile).where(AppProfile.app_id == app_id)
        )
        profile = result.scalar_one_or_none()
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"No security profile for app '{app_id}'.",
            )

        grant_result = await session.execute(
            select(AppModuleGrant).where(
                AppModuleGrant.profile_id == profile.id,
                AppModuleGrant.module_id == module_id,
            )
        )
        grant = grant_result.scalar_one_or_none()

        if grant is None:
            grant = AppModuleGrant(
                profile_id=profile.id,
                module_id=module_id,
                visibility=body.visibility,
                default_action_policy=body.default_action_policy,
                action_overrides=body.action_overrides,
            )
            session.add(grant)
        else:
            grant.visibility = body.visibility
            grant.default_action_policy = body.default_action_policy
            grant.action_overrides = body.action_overrides

        await session.commit()
        await session.refresh(grant)
        return _grant_to_dict(grant)


@router.delete("/profiles/{app_id}/grants/{module_id}")
async def delete_grant(app_id: str, module_id: str) -> Response:
    """Remove a module grant from an app's security profile."""
    from sqlalchemy import select

    from digitorn.core.database import _session_factory
    from digitorn.core.models import AppModuleGrant, AppProfile

    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Database not initialized.")

    async with _session_factory() as session:
        result = await session.execute(
            select(AppProfile).where(AppProfile.app_id == app_id)
        )
        profile = result.scalar_one_or_none()
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"No security profile for app '{app_id}'.",
            )

        grant_result = await session.execute(
            select(AppModuleGrant).where(
                AppModuleGrant.profile_id == profile.id,
                AppModuleGrant.module_id == module_id,
            )
        )
        grant = grant_result.scalar_one_or_none()
        if grant is None:
            raise HTTPException(
                status_code=404,
                detail=f"No grant for module '{module_id}' in app '{app_id}'.",
            )

        await session.delete(grant)
        await session.commit()

    return Response(status_code=204)


def _profile_to_dict(profile) -> dict[str, Any]:
    return {
        "app_id": profile.app_id,
        "is_active": profile.is_active,
        "default_policy": profile.default_policy,
        "granted_permissions": profile.granted_permissions,
        "max_risk_level": profile.max_risk_level,
        "risk_approval_rules": profile.risk_approval_rules,
        "approval_timeout": profile.approval_timeout,
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


def _grant_to_dict(grant) -> dict[str, Any]:
    return {
        "module_id": grant.module_id,
        "visibility": grant.visibility,
        "default_action_policy": grant.default_action_policy,
        "action_overrides": grant.action_overrides,
    }
