"""UserResolver - auto-resolve user-specific delivery targets for channels.

Channels like SMS, email, or Telegram need to know *where* to send a
notification for a given user.  Instead of requiring the LLM agent to
specify ``output_config`` manually for every delivery, the resolver
automatically fetches user contact info from a data source (database,
HTTP API, etc.) using the existing module system.

Think of it like authentication middleware: the system knows who the user
is (via ``session_id``), and the resolver maps that identity to delivery
targets (phone number, email address, Telegram chat ID).

Configuration (YAML)::

    channels:
      sms_alerts:
        type: sms
        config:
          account_sid: "{{env.TWILIO_SID}}"
          from_number: "+33600000000"
        user_resolver:
          module: database
          action: fetch_results
          params:
            query: "SELECT phone, email FROM users WHERE session_id = :session_id"
          mapping:
            to_number: phone

      email_reports:
        type: email
        config:
          smtp_host: "smtp.example.com"
        user_resolver:
          module: database
          action: fetch_results
          params:
            query: "SELECT email, name FROM users WHERE session_id = :session_id"
          mapping:
            to_address: email
            recipient_name: name

At delivery time:

1. If the channel has a ``user_resolver`` and ``session_id`` is known:
   - Execute ``module.action(params)`` with ``:session_id`` replaced
   - Map result fields to per-delivery config fields via ``mapping``
   - Merge with any explicit ``output_config`` (explicit wins)
2. Otherwise: use ``output_config`` as-is (backward compatible)

The resolver uses the **same modules** the agent uses (database, http, etc.),
so there's zero new infrastructure to set up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class ModuleExecutor(Protocol):
    """Protocol for executing a module action.

    Any object with an ``execute(action, params)`` async method qualifies.
    In practice this is a module instance from the registry.
    """

    async def execute(self, action: str, params: dict[str, Any]) -> Any: ...


@dataclass
class UserResolverConfig:
    """Configuration for auto-resolving user delivery targets.

    Attributes:
        module: Module ID to query (e.g. "database", "http").
        action: Action to call on the module (e.g. "fetch_results", "get").
        params: Parameters for the action. The placeholder ``:session_id``
            is replaced with the actual session_id at resolve time.
        mapping: Maps result field names to per-delivery config field names.
            e.g. ``{"to_number": "phone"}`` means: take the ``phone`` field
            from the query result and put it in ``to_number``.
        cache_ttl: How long to cache resolved results (seconds). 0 = no cache.
    """

    module: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    mapping: dict[str, str] = field(default_factory=dict)
    cache_ttl: float = 300.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserResolverConfig:
        """Build a UserResolverConfig from a dict, validating field types.

        Raises ValueError with a clear message if the config is malformed,
        instead of crashing later with a cryptic AttributeError or TypeError.
        """
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
    """Resolves user-specific delivery config from a data source.

    One resolver per channel instance (created at deploy time).
    Uses the app's module instances for queries (no new connections needed).

    Args:
        config: Resolver configuration from YAML.
        modules: Map of module_id → module instance (from bootstrap).
    """

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
        """Resolve per-delivery config for a user.

        Args:
            session_id: User's session ID. If None, returns output_config as-is.
            output_config: Explicit per-delivery overrides. These always win
                over auto-resolved values.

        Returns:
            Final per-delivery config dict for the channel.
        """
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
        """Identity-from-UserStore is no longer supported.

        The daemon does not own user identity anymore - it lives at
        the central auth service. Channel routing must rely on the
        configured module resolver (``self._query`` below) which can
        fetch contact info from any module the app declares (typically
        a passthrough to the auth service via http.get).

        Kept as a no-op so the resolver call chain stays unchanged
        and the falls-through-to-module path keeps working.
        """
        return None

    async def _query(self, session_id: str) -> dict[str, Any] | None:
        """Execute the configured module action to look up user info."""
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
        """Get cached result if still valid."""
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
        """Cache a resolved result."""
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
    """Replace :session_id placeholders in param values.

    Works recursively on string values in the dict. The placeholder
    ``:session_id`` in string values is replaced with the actual session_id.

    Also supports ``{{session_id}}`` template syntax for consistency with
    the rest of the YAML variable system.
    """
    result: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, str):
            value = value.replace(":session_id", session_id)
            value = value.replace("{{session_id}}", session_id)
        elif isinstance(value, dict):
            value = _inject_session_id(value, session_id)
        result[key] = value
    return result
