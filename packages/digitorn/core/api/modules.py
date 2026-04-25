"""Digitorn — Module API routes.

    GET  /api/modules              List all modules with status
    GET  /api/modules/{id}         Module details (manifest, actions)
    POST /api/modules/{id}/execute Execute an action on a module
    GET  /api/modules/{id}/health  Health check for a module
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from digitorn.core.events.models import UniversalEvent
from digitorn.modules.exceptions import (
    ActionExecutionError,
    ActionNotFoundError,
    ApprovalRequiredError,
    ModuleLoadError,
    ModuleNotFoundError,
    PermissionDeniedError,
)

router = APIRouter(prefix="/api/modules", tags=["modules"])


class ExecuteRequest(BaseModel):
    """Request body for POST /api/modules/{id}/execute."""

    action: str
    params: dict[str, Any] = {}
    app_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None


class ExecuteResponse(BaseModel):
    """Response body for POST /api/modules/{id}/execute."""

    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict[str, Any] = {}


def _get_registry(request: Request):
    """Get the module registry from app state."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="Module registry not initialized.")
    return registry


def _get_event_bus(request: Request):
    """Get the event bus from app state."""
    return getattr(request.app.state, "event_bus", None)


async def _load_profile(app_id: str | None) -> "SecurityProfile | None":
    """Load the SecurityProfile for a given app_id. Returns None if absent."""
    if not app_id:
        return None
    from digitorn.core.security import load_security_profile

    return await load_security_profile(app_id)


@router.get("")
async def list_modules(
    request: Request,
    app_id: str | None = Query(None, description="Application ID for visibility filtering."),
) -> dict[str, Any]:
    """List all modules with their status.

    When ``app_id`` is provided, modules marked *hidden* in that app's
    security profile are excluded from the response.
    """
    registry = _get_registry(request)
    report = registry.status_report()
    profile = await _load_profile(app_id)

    modules = []
    for module_id in registry.list_available():
        if profile and not profile.can_access_module(module_id):
            continue

        try:
            module = registry.get(module_id)
            actions = list(module._action_registry.keys())
            entry: dict[str, Any] = {
                "module_id": module_id,
                "version": module.VERSION,
                "status": "loaded",
                "actions": actions,
            }
            if profile:
                action_policies = {}
                for name, act_entry in module._action_registry.items():
                    action_policies[name] = profile.action_policy_info(
                        module_id, name, act_entry.spec.risk_level,
                    )
                entry["action_policies"] = action_policies
            modules.append(entry)
        except Exception as exc:
            logger.warning("Module introspection failed for '%s': %s", module_id, exc)
            modules.append({
                "module_id": module_id,
                "status": "error",
            })

    for module_id, reason in report.get("failed", {}).items():
        modules.append({
            "module_id": module_id,
            "status": "failed",
            "error": reason,
        })

    for module_id, reason in report.get("platform_excluded", {}).items():
        modules.append({
            "module_id": module_id,
            "status": "platform_excluded",
            "error": reason,
        })

    return {"modules": modules, "count": len(modules)}


@router.get("/{module_id}")
async def get_module(
    module_id: str,
    request: Request,
    app_id: str | None = Query(None, description="Application ID for visibility filtering."),
) -> dict[str, Any]:
    """Get detailed info about a specific module.

    Returns 404 if the module is hidden for the given ``app_id``.
    """
    registry = _get_registry(request)
    profile = await _load_profile(app_id)

    if profile and not profile.can_access_module(module_id):
        raise HTTPException(status_code=404, detail=f"Module '{module_id}' not found.")

    try:
        module = registry.get(module_id)
    except ModuleNotFoundError:
        raise HTTPException(status_code=404, detail=f"Module '{module_id}' not found.")
    except ModuleLoadError as exc:
        logger.warning("Module load failed: %s", module_id, exc_info=True)
        raise HTTPException(status_code=503, detail=f"Module '{module_id}' failed to load.")

    actions = []
    for name, entry in module._action_registry.items():
        action_info: dict[str, Any] = {
            "name": name,
            "description": entry.spec.description,
            "risk_level": entry.spec.risk_level,
            "permissions": entry.spec.permissions,
            "execution_mode": entry.spec.execution_mode,
            "input_schema": entry.spec.to_json_schema(),
        }
        if profile:
            action_info["policy"] = profile.action_policy_info(
                module_id, name, entry.spec.risk_level,
            )
        actions.append(action_info)

    return {
        "module_id": module.MODULE_ID,
        "version": module.VERSION,
        "platforms": [str(p) for p in module.SUPPORTED_PLATFORMS],
        "actions": actions,
    }


async def _persist_execution(
    request: Request,
    *,
    app_id: str | None,
    session_id: str | None,
    agent_id: str | None,
    module_id: str,
    action: str,
    params: dict[str, Any],
    correlation_id: str,
) -> "ActionExecution | None":
    """Create an ActionExecution record in status 'started'. Returns None on failure."""
    try:
        from digitorn.core.database import _session_factory
        from digitorn.core.models import ActionExecution

        if _session_factory is None:
            return None

        async with _session_factory() as session:
            execution = ActionExecution(
                app_id=app_id,
                session_id=session_id,
                agent_id=agent_id,
                module_id=module_id,
                action=action,
                params=params,
                status="started",
                correlation_id=correlation_id,
            )
            session.add(execution)
            await session.flush()
            # Expunge immediately so the detached object is safe across
            # concurrent requests (no lazy-load / refresh races).
            session.expunge(execution)
            await session.commit()
            return execution
    except Exception as exc:
        logger.warning("Execution record creation failed: %s", exc)
        return None


async def _complete_execution(
    execution: "ActionExecution | None",
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Update an execution record to completed or failed."""
    if execution is None:
        return
    try:
        from datetime import datetime, timezone

        from digitorn.core.database import _session_factory

        if _session_factory is None:
            return

        async with _session_factory() as session:
            execution.status = status
            execution.result = result
            execution.error = error
            execution.completed_at = datetime.now(timezone.utc)
            merged = await session.merge(execution)
            session.add(merged)
            await session.commit()
    except Exception:
        logger.debug("failed to record execution completion", exc_info=True)


def _require_admin_for_execute(request: Request) -> None:
    """Block non-admin callers from the raw module execute endpoint.

    This route bypasses ALL of the per-app sandboxing (security profile,
    workspace root, capability grants, path traversal guards) that only
    fires when modules are invoked through the agent loop. Any caller
    that reaches it can shell-out, read/write anywhere the daemon
    process can, etc. — so it is strictly an admin/debug tool.

    The loopback auth bypass (``_is_loopback_self_call``) already
    refuses POST to this path when the caller has no credentials, so
    the only way to get here is with a real token; we then require
    admin (or the narrow ``modules.execute`` permission).
    """
    perms = getattr(request.state, "permissions", []) or []
    if "*" in perms or "modules.execute" in perms:
        return
    raise HTTPException(
        status_code=403,
        detail={
            "error": "admin_required",
            "message": (
                "POST /api/modules/{id}/execute is an admin/debug route "
                "and requires the '*' or 'modules.execute' permission. "
                "Normal agent tool calls go through the agent loop, "
                "which enforces the app's security profile."
            ),
        },
    )


@router.post("/{module_id}/execute")
async def execute_action(
    module_id: str,
    body: ExecuteRequest,
    request: Request,
) -> ExecuteResponse:
    """Execute an action on a module."""
    _require_admin_for_execute(request)
    registry = _get_registry(request)
    event_bus = _get_event_bus(request)

    try:
        module = registry.get(module_id)
    except ModuleNotFoundError:
        raise HTTPException(status_code=404, detail=f"Module '{module_id}' not found.")
    except ModuleLoadError as exc:
        logger.warning("Module load failed: %s", module_id, exc_info=True)
        raise HTTPException(status_code=503, detail=f"Module '{module_id}' failed to load.")

    started_event = UniversalEvent(
        topic=f"digitorn.module.{module_id}.action_started",
        source=module_id,
        event_type="action_started",
        app_id=body.app_id,
        session_id=body.session_id,
        agent_id=body.agent_id,
        data={"action": body.action, "params": body.params},
    )
    correlation_id = started_event.correlation_id or started_event.event_id

    if event_bus:
        await event_bus.publish(started_event)

    execution = await _persist_execution(
        request,
        app_id=body.app_id,
        session_id=body.session_id,
        agent_id=body.agent_id,
        module_id=module_id,
        action=body.action,
        params=body.params,
        correlation_id=correlation_id,
    )

    security_profile = await _load_profile(body.app_id)

    if security_profile and security_profile.is_active:
        grant = security_profile.module_grants.get(module_id)
        if grant is not None and not grant.is_visible():
            raise HTTPException(status_code=404, detail=f"Module '{module_id}' not found.")

    from digitorn.modules.base import ExecutionContext

    stream = None
    if event_bus:
        from digitorn.core.events.stream import ExecutionStream

        stream = ExecutionStream(
            event_bus=event_bus,
            parent_event=started_event,
            module_id=module_id,
            action=body.action,
        )

    watcher_service = getattr(request.app.state, "watcher_service", None)
    service_bus = getattr(request.app.state, "service_bus", None)

    context = ExecutionContext(
        plan_id=correlation_id,
        action_id=started_event.event_id,
        service_bus=service_bus,
        stream=stream,
        session_id=body.session_id,
        security_profile=security_profile,
        watcher_service=watcher_service,
    )

    try:
        result = await module.execute(body.action, body.params, context=context)

        if hasattr(result, "to_dict"):
            response = ExecuteResponse(**result.to_dict())
        elif hasattr(result, "success"):
            response = ExecuteResponse(
                success=result.success,
                data=result.data,
                error=result.error,
                metadata=result.metadata,
            )
        else:
            response = ExecuteResponse(success=True, data=result)

        result_data = None
        if response.data is not None:
            try:
                result_data = {"data": response.data}
            except Exception:
                result_data = {"data": str(response.data)}
        await _complete_execution(execution, status="completed", result=result_data)

        if event_bus:
            await event_bus.publish(started_event.child(
                topic=f"digitorn.module.{module_id}.action_completed",
                event_type="action_completed",
                data={
                    "action": body.action,
                    "success": response.success,
                    "params": body.params,
                },
            ))

        return response

    except PermissionDeniedError as exc:
        await _complete_execution(execution, status="failed", error=str(exc))
        raise HTTPException(
            status_code=403,
            detail={
                "error": "permission_denied",
                "message": exc.message,
                "action": exc.action,
                "module": exc.module,
                "profile": exc.profile,
            },
        )

    except ApprovalRequiredError as exc:
        await _complete_execution(execution, status="failed", error=str(exc))
        return ExecuteResponse(
            success=False,
            error="approval_required",
            metadata={
                "approval_required": True,
                "action_id": exc.action_id,
                "plan_id": exc.plan_id,
                "message": exc.message,
            },
        )

    except ActionNotFoundError:
        await _complete_execution(execution, status="failed", error=f"Action '{body.action}' not found.")
        raise HTTPException(
            status_code=404,
            detail=f"Action '{body.action}' not found in module '{module_id}'.",
        )
    except ActionExecutionError as exc:
        await _complete_execution(execution, status="failed", error=str(exc))

        if event_bus:
            await event_bus.publish(started_event.child(
                topic=f"digitorn.module.{module_id}.action_failed",
                event_type="error",
                data={"action": body.action, "error": str(exc)},
            ))

        return ExecuteResponse(success=False, error=str(exc))


@router.get("/{module_id}/health")
async def module_health(module_id: str, request: Request) -> dict[str, Any]:
    """Health check for a specific module."""
    registry = _get_registry(request)

    try:
        module = registry.get(module_id)
    except ModuleNotFoundError:
        raise HTTPException(status_code=404, detail=f"Module '{module_id}' not found.")
    except ModuleLoadError as exc:
        logger.warning("Module load failed: %s", module_id, exc_info=True)
        raise HTTPException(status_code=503, detail=f"Module '{module_id}' failed to load.")

    try:
        health = await module.health_check()
        return health
    except Exception as exc:
        return {"status": "error", "module_id": module_id, "error": str(exc)}
