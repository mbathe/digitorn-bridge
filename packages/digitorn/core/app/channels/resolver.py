"""UserResolver - auto-resolve user-specific delivery targets for channels."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class ModuleExecutor(Protocol):
    """Protocol for executing a module action."""

    async def execute(self, action: str, params: dict[str, Any]) -> Any: ...


@dataclass
class UserResolverConfig:
    """Configuration for auto-resolving user delivery targets."""

    module: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    mapping: dict[str, str] = field(default_factory=dict)
    cache_ttl: float = 300.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserResolverConfig:
        """Build a UserResolverConfig from a dict, validating field types."""
        if not isinstance(data, dict):
            raise ValueError(f"UserResolverConfig requires a dict, got {type(data).__name__}")

        # Required fields
        module = data.get("module")
        action = data.get("action")
        if not isinstance(module, str) or not module:
            raise ValueError("UserResolverConfig.module must be a non-empty string")
        if not isinstance(action, str) or not action:
            raise ValueError("UserResolverConfig.action must be a non-empty string")

        # Optional fields with type validation
        params = data.get("params", {})
        if not isinstance(params, dict):
            raise ValueError(f"UserResolverConfig.params must be a dict, got {type(params).__name__}")

        mapping = data.get("mapping", {})
        if not isinstance(mapping, dict):
            raise ValueError(f"UserResolverConfig.mapping must be a dict, got {type(mapping).__name__}")

        cache_ttl = data.get("cache_ttl", 300.0)
        try:
            cache_ttl = float(cache_ttl)
        except (TypeError, ValueError):
            raise ValueError(f"UserResolverConfig.cache_ttl must be a number, got {cache_ttl!r}")

        return cls(
            module=module,
            action=action,
            params=params,
            mapping=mapping,
            cache_ttl=cache_ttl,
        )


class UserResolver:
    """Resolves user-specific delivery config from a data source."""

    def __init__(
        self,
        config: UserResolverConfig,
        modules: dict[str, ModuleExecutor] | None = None,
    ) -> None:
        self._config = config
        self._modules = modules or {}
        self._user_store: Any | None = None
        self._cache: dict[str, tuple[dict[str, Any], float]] = {}

    def set_modules(self, modules: dict[str, ModuleExecutor]) -> None:
        """Update the module map (called at bootstrap after modules are ready)."""
        self._modules = modules

    def set_user_store(self, user_store: Any) -> None:
        """Inject the UserStore for unified user resolution."""
        self._user_store = user_store

    async def resolve(
        self,
        session_id: str | None,
        output_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve per-delivery config for a user."""
        output_config = output_config or {}

        if not session_id:
            return output_config

        resolved = await self._resolve_from_user_store(session_id)

        if resolved is None:
            resolved = self._get_cached(session_id)
            if resolved is None:
                resolved = await self._query(session_id)
                if resolved is not None:
                    self._set_cached(session_id, resolved)

        if resolved is None:
            logger.warning(
                "user_resolver_no_result session=%s module=%s action=%s",
                session_id, self._config.module, self._config.action,
            )
            return output_config

        mapped: dict[str, Any] = {}
        for delivery_field, result_field in self._config.mapping.items():
            if result_field in resolved:
                mapped[delivery_field] = resolved[result_field]

        mapped.update(output_config)
        return mapped

    async def _resolve_from_user_store(
        self, session_id: str
    ) -> dict[str, Any] | None:
        """Identity-from-UserStore is no longer supported."""
        return None

    async def _query(self, session_id: str) -> dict[str, Any] | None:
        module = self._modules.get(self._config.module)
        if module is None:
            logger.error(
                "user_resolver_module_not_found module=%s",
                self._config.module,
            )
            return None

        params = _inject_session_id(self._config.params, session_id)

        try:
            result = await module.execute(self._config.action, params)

            if hasattr(result, "success"):
                if not result.success:
                    logger.warning(
                        "user_resolver_action_failed module=%s error=%s",
                        self._config.module, getattr(result, "error", ""),
                    )
                    return None
                result = result.data

            if isinstance(result, dict):
                if "rows" in result and isinstance(result["rows"], list):
                    rows = result["rows"]
                    if rows and isinstance(rows[0], dict):
                        return rows[0]
                    return None
                return result
            if isinstance(result, list) and result:
                first = result[0]
                if isinstance(first, dict):
                    return first
            return None

        except Exception as exc:
            logger.exception(
                "user_resolver_query_error module=%s session=%s",
                self._config.module, session_id,
            )
            return None

    def _get_cached(self, session_id: str) -> dict[str, Any] | None:
        if self._config.cache_ttl <= 0:
            return None
        entry = self._cache.get(session_id)
        if entry is None:
            return None
        resolved, expiry = entry
        import time
        if time.time() > expiry:
            del self._cache[session_id]
            return None
        return resolved

    def _set_cached(self, session_id: str, resolved: dict[str, Any]) -> None:
        if self._config.cache_ttl <= 0:
            return
        import time
        self._cache[session_id] = (resolved, time.time() + self._config.cache_ttl)
        if len(self._cache) > 10000:
            now = time.time()
            stale = [k for k, (_, exp) in self._cache.items() if now > exp]
            for k in stale:
                del self._cache[k]

    def clear_cache(self, session_id: str | None = None) -> None:
        """Clear cached results. If session_id is None, clears all."""
        if session_id is None:
            self._cache.clear()
        else:
            self._cache.pop(session_id, None)


def _inject_session_id(params: dict[str, Any], session_id: str) -> dict[str, Any]:
    """Replace :session_id placeholders in param values."""
    result: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, str):
            value = value.replace(":session_id", session_id)
            value = value.replace("{{session_id}}", session_id)
        elif isinstance(value, dict):
            value = _inject_session_id(value, session_id)
        result[key] = value
    return result
