"""Module policy enforcement.

Checks runtime constraints (max_parallel_calls, cooldowns) before action
dispatch.  Wired into the PlanExecutor between permission check and
module.execute().

Usage::

    enforcer = PolicyEnforcer(registry)
    await enforcer.check_and_acquire(module_id, action)
    try:
        result = await module.execute(action, params)
    finally:
        enforcer.release(module_id)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from digitorn.modules.exceptions import PolicyViolationError
from digitorn.modules.log import get_logger

if TYPE_CHECKING:
    from digitorn.modules.base import ModulePolicy
    from digitorn.modules.registry import ModuleRegistry

log = get_logger(__name__)


@dataclass
class PolicyState:
    """Per-module runtime policy tracking."""

    active_calls: int = 0
    last_call_time: float = 0.0
    semaphore: asyncio.Semaphore | None = None


class PolicyEnforcer:
    """Enforces ModulePolicy constraints at runtime.

    For each module, it tracks:
    - Number of active concurrent calls
    - Last call timestamp (for cooldown enforcement)
    - An asyncio.Semaphore (for max_parallel_calls enforcement)
    """

    def __init__(self, registry: ModuleRegistry) -> None:
        self._registry = registry
        self._states: dict[str, PolicyState] = {}
        self._policies: dict[str, ModulePolicy] = {}

    def load_policy(self, module_id: str) -> ModulePolicy:
        """Load and cache the policy for a module."""
        if module_id not in self._policies:
            module = self._registry.get(module_id)
            self._policies[module_id] = module.policy_rules()
        return self._policies[module_id]

    def _get_state(self, module_id: str) -> PolicyState:
        """Get or create the runtime state for a module."""
        if module_id not in self._states:
            policy = self.load_policy(module_id)
            sem = None
            if policy.max_parallel_calls > 0:
                sem = asyncio.Semaphore(policy.max_parallel_calls)
            self._states[module_id] = PolicyState(semaphore=sem)
        return self._states[module_id]

    async def check_and_acquire(self, module_id: str, action: str) -> None:
        """Check policy constraints and acquire an execution slot.

        Raises :class:`PolicyViolationError` if the cooldown has not elapsed.
        Blocks (up to timeout) if max_parallel_calls is reached.
        """
        policy = self.load_policy(module_id)
        state = self._get_state(module_id)

        if policy.cooldown_seconds > 0 and state.last_call_time > 0:
            elapsed = time.monotonic() - state.last_call_time
            if elapsed < policy.cooldown_seconds:
                remaining = policy.cooldown_seconds - elapsed
                raise PolicyViolationError(
                    module_id=module_id,
                    violation=f"Cooldown: {remaining:.1f}s remaining",
                )

        if state.semaphore is not None:
            try:
                await asyncio.wait_for(state.semaphore.acquire(), timeout=30.0)
            except asyncio.TimeoutError:
                raise PolicyViolationError(
                    module_id=module_id,
                    violation=(
                        f"Concurrency limit ({policy.max_parallel_calls}) "
                        f"reached, timed out waiting for a slot"
                    ),
                )

        state.active_calls += 1
        state.last_call_time = time.monotonic()

    def release(self, module_id: str) -> None:
        """Release a concurrent execution slot."""
        state = self._states.get(module_id)
        if state is not None:
            state.active_calls = max(0, state.active_calls - 1)
            if state.semaphore is not None:
                state.semaphore.release()

    def reset(self, module_id: str) -> None:
        """Reset policy state for a module (e.g. after restart)."""
        self._states.pop(module_id, None)
        self._policies.pop(module_id, None)

    def status(self) -> dict[str, dict[str, Any]]:
        """Return policy enforcement status for all tracked modules."""
        result: dict[str, dict[str, Any]] = {}
        for mid, state in self._states.items():
            policy = self._policies.get(mid)
            result[mid] = {
                "active_calls": state.active_calls,
                "max_parallel_calls": policy.max_parallel_calls if policy else 0,
                "cooldown_seconds": policy.cooldown_seconds if policy else 0.0,
                "last_call_time": state.last_call_time,
            }
        return result
