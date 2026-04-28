"""ApiKeyHandler - the most common case.

Handles plain API keys: OpenAI, Anthropic, DeepSeek, any service that
exposes a single bearer-token authentication. The handler:

- Validates the declared fields (regex + required)
- Does NOT have a native refresh mechanism (API keys never expire
  from the provider's side unless revoked manually)
- Implements ``refresh`` as a ``test_live_connection`` call so the
  proactive worker can mark a revoked key as ``invalid``
- The live test is **opt-in**: if the schema has a ``test_endpoint``
  field, we hit it with the key and expect a 2xx. Otherwise the test
  is a no-op.
"""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.credentials.handler import CredentialHandler, now_utc

logger = logging.getLogger(__name__)


class ApiKeyHandler(CredentialHandler):
    provider_type = "api_key"

    async def test_live_connection(
        self,
        fields: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Hit the declared test endpoint if any.

        The schema can declare::

            test:
              method: GET
              url: "https://api.openai.com/v1/models"
              auth_header: "Authorization: Bearer {{field.api_key}}"
              expected_status: 200

        This is *optional*. Without it the handler trusts the fields
        blindly - still better than nothing because the bootstrap
        resolver + form validation catch empty values, regex
        mismatches, and out-of-spec prefixes.
        """
        test = schema_provider.get("test")
        if not test:
            return True, None

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

    async def refresh(
        self,
        credential: dict[str, Any],
        schema_provider: dict[str, Any],
    ) -> dict[str, Any]:
        """Re-validate the API key - not a real refresh (keys don't expire).

        Called by the proactive worker periodically. If the live test
        passes, bumps ``last_validated_at`` and keeps status=valid. If
        it fails, flips status to ``invalid`` so the runtime refuses
        to use it and the user is notified.
        """
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
