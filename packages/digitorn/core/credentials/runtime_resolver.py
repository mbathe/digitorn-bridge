"""Runtime secret resolution — per-user credentials at activation time.

At compile time, credentials at scopes ``system_wide`` and
``per_app_shared`` are baked into the compiled app (see
``compile_resolver.build_compile_secrets``). Scopes that depend on a
user (``per_user``, ``per_app_per_user``) can NOT be baked at compile
time — the compile has no user context — so any ``{{secret.X}}``
reference to a user-scoped credential is left as a **passthrough**
template in the compiled output.

This module walks the compiled values at runtime, now that we know
which user is active, and substitutes the remaining passthroughs by
calling ``CredentialStore.resolve_field``.

Integration points:

1. ``channels/template.py::render`` — the channel pipeline rendering
   path (user message, system prompt addendum, prepare-step params).
2. ``agent_loop`` — whenever a brain_config.api_key / base_url /
   organization is about to be passed to an LLM provider.
3. Any module whose params contain user-scoped secrets (rare today,
   but the same helper works for all of them).

The resolver is *opt-in at the call site*: existing code that
doesn't care about per-user secrets continues to work because the
passthrough format is a valid (if useless) string that the LLM
would never see in a properly wired app.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from digitorn.core.credentials.store import CredentialStore

logger = logging.getLogger(__name__)


# Matches ``{{secret.X}}`` AND ``{{env.X}}``. Both are treated as
# credential references at runtime — ``env.X`` exists because the
# compile pass is lenient (passes the template through instead of
# crashing) so the runtime resolver is the single place that knows
# how to look up a per-user secret.
_SECRET_PATTERN = re.compile(
    r"\{\{\s*(?:secret|env)\.([a-zA-Z0-9_.-]+)\s*\}\}"
)


async def resolve_runtime_secrets_in_value(
    value: Any,
    *,
    store: CredentialStore | None,
    user_id: str,
    app_id: str,
    raise_on_miss: bool = False,
    provider_hint: str | None = None,
) -> Any:
    """Walk an arbitrary JSON-compatible value and substitute user secrets.

    Input ``value`` can be a string, a dict, a list, or any nested
    mix. The resolver walks it recursively. Anything that is not
    string-shaped is passed through untouched.

    For every ``{{secret.X}}`` template encountered:

    1. Extract the key ``X``
    2. Call ``store.resolve_field(X, user_id, app_id)``
    3. If a value comes back → substitute it
    4. If no value comes back:
       - ``raise_on_miss=False`` (default): leave the template as-is.
         The LLM / channel will see a literal ``{{secret.X}}`` which
         is visibly broken but won't crash the request.
       - ``raise_on_miss=True``: raise ``CredentialMissing`` so the
         activation can be aborted with a structured error.

    The ``store`` argument may be ``None`` for dev paths that don't
    have credentials configured — in that case the function is a
    no-op (returns the value unchanged).
    """
    if store is None:
        return value
    if isinstance(value, str):
        return await _resolve_string(
            value, store=store, user_id=user_id, app_id=app_id,
            raise_on_miss=raise_on_miss,
            provider_hint=provider_hint,
        )
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out[k] = await resolve_runtime_secrets_in_value(
                v, store=store, user_id=user_id, app_id=app_id,
                raise_on_miss=raise_on_miss,
                provider_hint=provider_hint,
            )
        return out
    if isinstance(value, list):
        return [
            await resolve_runtime_secrets_in_value(
                item, store=store, user_id=user_id, app_id=app_id,
                raise_on_miss=raise_on_miss,
                provider_hint=provider_hint,
            )
            for item in value
        ]
    return value


async def _resolve_string(
    text: str,
    *,
    store: CredentialStore,
    user_id: str,
    app_id: str,
    raise_on_miss: bool,
    provider_hint: str | None = None,
) -> str:
    """Substitute every ``{{secret.X}}`` / ``{{env.X}}`` in a string.

    Uses the grant-aware ``resolve_field_for_app`` which may raise
    ``CredentialAuthRequired`` when the user has candidates for the
    provider but hasn't granted any to this app yet. That exception
    propagates up so the agent loop can surface the picker flow to
    the client.

    ``provider_hint`` lets the caller disambiguate bare references
    like ``{{secret.DEEPSEEK_API_KEY}}``: when supplied, the lookup
    rewrites the key as ``"<hint>.DEEPSEEK_API_KEY"`` so the
    credential store joins on the **canonical provider name**
    (``deepseek``) instead of treating the secret's field name as a
    provider. Explicit ``{{secret.deepseek.X}}`` references ignore
    the hint and always win.
    """
    if "{{secret." not in text and "{{env." not in text:
        return text

    matches = list(_SECRET_PATTERN.finditer(text))
    if not matches:
        return text

    result_parts: list[str] = []
    last_end = 0

    from digitorn.core.credentials.store import (
        CredentialAuthRequired,
        CredentialMissing,
    )

    for match in matches:
        key = match.group(1)
        # Build the list of lookup keys to try, in priority order:
        #   1. ``{hint}.{key}`` — the canonical form
        #      (``deepseek.DEEPSEEK_API_KEY``). Matches real credentials
        #      stored under ``provider_name='deepseek'``.
        #   2. ``key`` — the bare fallback. Matches legacy credentials
        #      stored under ``provider_name='DEEPSEEK_API_KEY'`` AND
        #      explicit qualified refs ``{{secret.foo.BAR}}``.
        lookup_keys: list[str] = []
        if provider_hint and "." not in key:
            lookup_keys.append(f"{provider_hint}.{key}")
        lookup_keys.append(key)

        value: str | None = None
        lookup_used: str = key
        auth_exc: Exception | None = None
        for lookup_key in lookup_keys:
            try:
                v = await store.resolve_field_for_app(
                    provider_or_field=lookup_key,
                    user_id=user_id,
                    app_id=app_id,
                    raise_on_auth_required=True,
                )
            except CredentialAuthRequired as exc:
                # Remember the first auth_required, but keep trying
                # cheaper lookups in case a plain credential exists
                # under the bare key.
                auth_exc = exc
                continue
            except Exception as exc:
                logger.warning(
                    "runtime secret resolve failed for key=%s lookup=%s user=%s app=%s: %s",
                    key, lookup_key, user_id, app_id, exc,
                )
                v = None
            if v is not None:
                value = v
                lookup_used = lookup_key
                auth_exc = None  # a real hit supersedes any auth flow
                break

        # No hit at all → if any lookup surfaced an auth flow, bubble
        # it up now so the client gets the picker.
        if value is None and auth_exc is not None:
            raise auth_exc

        logger.debug(
            "runtime_secret_lookup key=%s hint=%s lookup=%s user=%s app=%s hit=%s",
            key, provider_hint, lookup_used, user_id, app_id, value is not None,
        )

        # ── Env var fallback ──────────────────────────────────────
        # If the credential store has no value for the user, AND
        # the corresponding environment variable IS set on the
        # daemon process, use it. This makes the daemon work in
        # single-tenant / dev mode where the operator just exports
        # ``DEEPSEEK_API_KEY=sk-…`` without going through the
        # credential API. In multi-tenant prod the env var would
        # not be set, so per-user credentials win as expected.
        if value is None:
            import os as _os
            # ``key`` looks like ``DEEPSEEK_API_KEY`` (bare) or
            # ``deepseek.DEEPSEEK_API_KEY`` (qualified). Try both.
            env_key = key.split(".")[-1] if "." in key else key
            env_val = _os.environ.get(env_key)
            if env_val:
                logger.debug(
                    "runtime secret resolved from env: key=%s",
                    env_key,
                )
                value = env_val

        result_parts.append(text[last_end:match.start()])
        if value is not None:
            result_parts.append(str(value))
        else:
            if raise_on_miss:
                raise CredentialMissing(
                    provider=key.split(".")[0] if "." in key else key,
                    field=key.split(".", 1)[1] if "." in key else key,
                    app_id=app_id,
                    user_id=user_id,
                )
            result_parts.append(match.group(0))
        last_end = match.end()

    result_parts.append(text[last_end:])
    return "".join(result_parts)


# ────────────────────────────────────────────────────────────────────
# Small synchronous wrapper for code paths that are still sync
# ────────────────────────────────────────────────────────────────────


def collect_unresolved_secrets(value: Any) -> list[str]:
    """Return the list of ``{{secret.X}}`` / ``{{env.X}}`` templates still
    present in ``value``.

    Used by observability + audit code to detect credential misses
    before they reach the LLM. Purely read-only — does not touch
    the store.
    """
    found: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            for m in _SECRET_PATTERN.finditer(node):
                found.append(m.group(1))
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(value)
    return found
