"""Digitorn - Exception hierarchy."""

from __future__ import annotations

from typing import Any

class DigitornError(Exception):
    """Base exception for all Digitorn errors."""

    def __init__(self, message: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context or {}

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.message!r}, context={self.context})"

class ProtocolError(DigitornError):
    """Base for all IML protocol errors."""

class IMLParseError(ProtocolError):
    """The incoming payload could not be parsed as valid JSON or IML structure."""

    def __init__(self, message: str, raw_payload: str | None = None) -> None:
        super().__init__(message, context={"raw_payload": raw_payload})
        self.raw_payload = raw_payload

class IMLValidationError(ProtocolError):
    """The IML plan failed Pydantic validation."""

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message, context={"validation_errors": errors or []})
        self.errors = errors or []

class TemplateResolutionError(ProtocolError):
    """A `{{result.X.Y}}` template reference could not be resolved."""

    def __init__(self, template: str, reason: str) -> None:
        super().__init__(
            f"Cannot resolve template '{template}': {reason}",
            context={"template": template, "reason": reason},
        )
        self.template = template

class SecurityError(DigitornError):
    """Base for all security-related errors."""

class PermissionDeniedError(SecurityError):
    """The active permission profile does not allow this action."""

    def __init__(self, action: str, module: str, profile: str) -> None:
        super().__init__(
            f"Permission denied: action '{module}.{action}' is not allowed "
            f"under profile '{profile}'",
            context={"action": action, "module": module, "profile": profile},
        )
        self.action = action
        self.module = module
        self.profile = profile

class ApprovalRequiredError(SecurityError):
    """The action requires explicit user approval before execution."""

    def __init__(self, action_id: str, plan_id: str) -> None:
        super().__init__(
            f"Action '{action_id}' in plan '{plan_id}' requires user approval",
            context={"action_id": action_id, "plan_id": plan_id},
        )
        self.action_id = action_id
        self.plan_id = plan_id

class PermissionNotGrantedError(SecurityError):
    """A required OS resource permission has not been granted."""

    def __init__(
        self,
        permission: str,
        module_id: str,
        action: str = "",
        risk_level: str = "medium",
    ) -> None:
        super().__init__(
            f"Permission '{permission}' not granted for module '{module_id}'"
            + (f" (action '{action}')" if action else ""),
            context={
                "permission": permission,
                "module_id": module_id,
                "action": action,
                "risk_level": risk_level,
            },
        )
        self.permission = permission
        self.module_id = module_id
        self.action = action
        self.risk_level = risk_level

class RateLimitExceededError(SecurityError):
    """An action has exceeded its configured rate limit."""

    def __init__(self, action_key: str, limit: int, window: str = "minute") -> None:
        super().__init__(
            f"Rate limit exceeded for '{action_key}': max {limit} per {window}",
            context={"action_key": action_key, "limit": limit, "window": window},
        )
        self.action_key = action_key
        self.limit = limit
        self.window = window

class SanitizationError(SecurityError):
    """An output sanitisation rule was violated."""

class IntentVerificationError(SecurityError):
    """The intent verification LLM call failed or returned an unparseable result."""

    def __init__(self, plan_id: str, reason: str) -> None:
        super().__init__(
            f"Intent verification failed for plan '{plan_id}': {reason}",
            context={"plan_id": plan_id, "reason": reason},
        )
        self.plan_id = plan_id
        self.reason = reason

class SuspiciousIntentError(SecurityError):
    """The intent verifier detected a security threat in the plan."""

    def __init__(
        self,
        plan_id: str,
        reasoning: str,
        threats: list[str] | None = None,
        risk_level: str = "high",
    ) -> None:
        super().__init__(
            f"Suspicious intent detected in plan '{plan_id}': {reasoning}",
            context={
                "plan_id": plan_id,
                "reasoning": reasoning,
                "threats": threats or [],
                "risk_level": risk_level,
            },
        )
        self.plan_id = plan_id
        self.reasoning = reasoning
        self.threats = threats or []
        self.risk_level = risk_level

class InputScanRejectedError(SecurityError):
    """The input scanner pipeline rejected the plan as malicious."""

    def __init__(
        self,
        plan_id: str,
        verdict: str,
        risk_score: float,
        scanners: list[str] | None = None,
    ) -> None:
        super().__init__(
            f"Scanner pipeline rejected plan '{plan_id}': "
            f"verdict={verdict}, risk={risk_score:.2f}",
            context={
                "plan_id": plan_id,
                "verdict": verdict,
                "risk_score": risk_score,
                "scanners": scanners or [],
            },
        )
        self.plan_id = plan_id
        self.verdict = verdict
        self.risk_score = risk_score
        self.scanners = scanners or []

class IdentityError(DigitornError):
    """Base for all identity/RBAC errors."""

class AuthenticationError(IdentityError):
    """API key authentication failed (missing, invalid, revoked, or expired)."""

class AuthorizationError(IdentityError):
    """Role-based access check failed - insufficient privileges."""

    def __init__(self, role: str, required: str, resource: str = "") -> None:
        msg = f"Role '{role}' insufficient (requires '{required}')"
        if resource:
            msg += f" for resource '{resource}'"
        super().__init__(msg, context={"role": role, "required": required, "resource": resource})
        self.role = role
        self.required = required

class ApplicationNotFoundError(IdentityError):
    """No application with the given ID exists."""

    def __init__(self, app_id: str) -> None:
        super().__init__(
            f"Application '{app_id}' not found",
            context={"app_id": app_id},
        )
        self.app_id = app_id

class ClusterError(DigitornError):
    """Base for all cluster/distributed errors."""

class NodeUnreachableError(ClusterError):
    """A remote node is not reachable."""

    def __init__(self, node_id: str, reason: str = "") -> None:
        super().__init__(
            f"Node '{node_id}' is unreachable" + (f": {reason}" if reason else ""),
            context={"node_id": node_id, "reason": reason},
        )
        self.node_id = node_id

class NodeNotFoundError(ClusterError):
    """No node with the given ID is registered."""

    def __init__(self, node_id: str) -> None:
        super().__init__(
            f"Node '{node_id}' is not registered",
            context={"node_id": node_id},
        )
        self.node_id = node_id

class OrchestrationError(DigitornError):
    """Base for all orchestration errors."""

class DAGCycleError(OrchestrationError):
    """The dependency graph contains a cycle."""

    def __init__(self, cycle: list[str]) -> None:
        super().__init__(
            f"Dependency cycle detected: {' -> '.join(cycle)}",
            context={"cycle": cycle},
        )
        self.cycle = cycle

class DependencyError(OrchestrationError):
    """A required dependency failed or was not found."""

    def __init__(self, action_id: str, dep_id: str, reason: str) -> None:
        super().__init__(
            f"Action '{action_id}' dependency '{dep_id}' not satisfied: {reason}",
            context={"action_id": action_id, "dep_id": dep_id, "reason": reason},
        )

class ExecutionTimeoutError(OrchestrationError):
    """An action exceeded its configured timeout."""

    def __init__(self, action_id: str, timeout_seconds: int) -> None:
        super().__init__(
            f"Action '{action_id}' timed out after {timeout_seconds}s",
            context={"action_id": action_id, "timeout_seconds": timeout_seconds},
        )
        self.action_id = action_id
        self.timeout_seconds = timeout_seconds

class ModuleError(DigitornError):
    """Base for all module errors."""

class ModuleNotFoundError(ModuleError):
    """No module with the given ID is registered."""

    def __init__(self, module_id: str) -> None:
        super().__init__(
            f"Module '{module_id}' is not registered",
            context={"module_id": module_id},
        )
        self.module_id = module_id

class ActionNotFoundError(ModuleError):
    """The module does not expose the requested action."""

    def __init__(self, module_id: str, action: str) -> None:
        super().__init__(
            f"Module '{module_id}' does not expose action '{action}'",
            context={"module_id": module_id, "action": action},
        )
        self.module_id = module_id
        self.action = action

class ModuleLoadError(ModuleError):
    """A module failed to initialise (missing dependency, wrong platform, etc.)."""

    def __init__(self, module_id: str, reason: str) -> None:
        super().__init__(
            f"Module '{module_id}' failed to load: {reason}",
            context={"module_id": module_id, "reason": reason},
        )

class ActionExecutionError(ModuleError):
    """An action raised an unexpected error during execution."""

    def __init__(self, module_id: str, action: str, cause: Exception) -> None:
        super().__init__(
            f"Action '{module_id}.{action}' failed: {cause}",
            context={"module_id": module_id, "action": action, "cause": str(cause)},
        )
        self.cause = cause

class WorkerError(ModuleError):
    """Base for all isolated worker errors."""

class WorkerStartError(WorkerError):
    """The worker subprocess failed to start or complete handshake."""

    def __init__(self, module_id: str, reason: str) -> None:
        super().__init__(
            f"Worker for '{module_id}' failed to start: {reason}",
            context={"module_id": module_id, "reason": reason},
        )

class WorkerCommunicationError(WorkerError):
    """JSON-RPC communication with the worker failed."""

    def __init__(self, module_id: str, reason: str) -> None:
        super().__init__(
            f"Worker '{module_id}' communication error: {reason}",
            context={"module_id": module_id, "reason": reason},
        )

class WorkerCrashedError(WorkerError):
    """The worker subprocess terminated unexpectedly."""

    def __init__(self, module_id: str, exit_code: int) -> None:
        super().__init__(
            f"Worker '{module_id}' crashed with exit code {exit_code}",
            context={"module_id": module_id, "exit_code": exit_code},
        )

class VenvCreationError(WorkerError):
    """Failed to create or validate a virtual environment."""

    def __init__(self, module_id: str, reason: str) -> None:
        super().__init__(
            f"Venv creation for '{module_id}' failed: {reason}",
            context={"module_id": module_id, "reason": reason},
        )

class ModuleLifecycleError(ModuleError):
    """Invalid lifecycle state transition."""

    def __init__(self, module_id: str, current_state: str, target_state: str) -> None:
        super().__init__(
            f"Module '{module_id}' cannot transition from "
            f"'{current_state}' to '{target_state}'",
            context={
                "module_id": module_id,
                "current_state": current_state,
                "target_state": target_state,
            },
        )
        self.module_id = module_id
        self.current_state = current_state
        self.target_state = target_state

class ServiceNotFoundError(ModuleError):
    """No service with this name is registered on the ServiceBus."""

    def __init__(self, service: str) -> None:
        super().__init__(
            f"Service '{service}' is not registered",
            context={"service": service},
        )
        self.service = service

class ActionDisabledError(ModuleError):
    """An action has been administratively disabled by the Module Manager."""

    def __init__(self, module_id: str, action: str, reason: str = "") -> None:
        msg = f"Action '{module_id}.{action}' is disabled"
        if reason:
            msg += f": {reason}"
        super().__init__(
            msg,
            context={"module_id": module_id, "action": action, "reason": reason},
        )
        self.module_id = module_id
        self.action = action
        self.reason = reason

class PolicyViolationError(ModuleError):
    """A module policy constraint was violated (cooldown, concurrency, etc.)."""

    def __init__(self, module_id: str, violation: str) -> None:
        super().__init__(
            f"Policy violation for module '{module_id}': {violation}",
            context={"module_id": module_id, "violation": violation},
        )
        self.module_id = module_id
        self.violation = violation

class PerceptionError(DigitornError):
    """Base for all perception errors."""

class ScreenCaptureError(PerceptionError):
    """Failed to capture a screenshot."""

class OCRError(PerceptionError):
    """Failed to perform OCR on a captured image."""

class MemoryError(DigitornError):
    """Base for all memory errors."""

class StateStoreError(MemoryError):
    """SQLite state store operation failed."""

class VectorStoreError(MemoryError):
    """ChromaDB vector store operation failed."""
