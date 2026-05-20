"""ApiKeyHandler - the most common case."""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.credentials.handler import CredentialHandler, now_utc

logger = logging.getLogger(__name__)


class ApiKeyHandler(CredentialHandler):
    provider_type = "api_key"
    # API keys are simple secrets - usable at any scope.
    allowed_scopes = (
        "system_wide",
        "per_app_shared",
        "per_user",
        "per_app_per_user",
    )

    @classmethod
    def schema_fields(cls) -> list[Any]:
        from digitorn.core.credentials.field_spec import common_api_key
        return [common_api_key()]

    async def test_live_connection(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Hit the declared test endpoint if any."""
        test = schema_provider.get("test")
        if not test:
            return await _ping_with_auth(fields)

        try:
            import aiohttp
        except ImportError:
            logger.debug(
                "api_key live test skipped: aiohttp not available"
            )
            return True, None

        url = test.get("url")
        method = (test.get("method") or "GET").upper()
        expected = int(test.get("expected_status") or 200)
        auth_header_template = test.get("auth_header")

        headers: dict[str, str] = {}
        if auth_header_template:
            # Minimal {{field.X}} substitution - not a full template
            # engine, just enough for the test endpoint declaration.
            rendered = auth_header_template
            for k, v in (fields or {}).items():
                rendered = rendered.replace("{{field." + k + "}}", str(v or ""))
            if ": " in rendered:
                name, _, value = rendered.partition(": ")
                headers[name.strip()] = value.strip()

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=headers) as r:
                    ok = r.status == expected
                    return ok, None if ok else f"HTTP {r.status}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


async def _ping_with_auth(
    fields: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    """Generic fallback test for custom api_key credentials."""
    fields = fields or {}
    api_key = str(fields.get("api_key") or "").strip()
    base_url = str(fields.get("base_url") or "").strip()
    if not api_key:
        return False, "API key is empty"
    if not base_url:
        return True, "API key set (no base_url to ping)"
    header_name = str(fields.get("auth_header") or "Authorization").strip()
    auth_value = (
        f"Bearer {api_key}"
        if header_name.lower() == "authorization"
        else api_key
    )
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as cl:
            resp = await cl.get(base_url, headers={header_name: auth_value})
            if resp.status_code in (401, 403):
                return False, f"Auth rejected (HTTP {resp.status_code})"
            return True, f"Reachable (HTTP {resp.status_code})"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    async def refresh(
        self,
        credential: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> dict[str, Any]:
        """Re-validate the API key - not a real refresh (keys don't expire)."""
        ok, err = await self.test_live_connection(
            credential.get("fields") or {}, schema_provider,
        )
        updated = dict(credential)
        if ok:
            updated["status"] = "valid"
            updated["last_validated_at"] = now_utc().isoformat()
            updated["last_error"] = None
        else:
            updated["status"] = "invalid"
            updated["last_error"] = err
        return updated
