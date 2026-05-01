"""End-to-end OAuth flow against the local mock provider.

Exercises:
  T3 - PendingFlowStore.start() -> build_auth_url
       Browser would visit auth_url -> mock redirects with `code`
       TokenExchange.exchange_code() -> store credential
  T4 - TokenExchange.refresh_token()
  T5 - oauth2 handler.revoke()

Run while both `mock_oauth_server.py` (port 9998) and the digitorn
daemon (port 8765) are up.
"""
from __future__ import annotations

import asyncio
import os
import sys

import urllib.parse
import urllib.request


def _follow_authorize(auth_url: str) -> str:
    """Hit the auth_url and capture the redirect to the callback URL.
    Returns the full callback URL (with `state` and `code` query)."""
    # Custom handler that does NOT follow redirects so we can read
    # the Location header.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect())
    try:
        resp = opener.open(auth_url, timeout=5)
        return resp.geturl()
    except urllib.error.HTTPError as e:
        # Got the 302 - read its Location header.
        loc = e.headers.get("Location") or ""
        return loc


async def main() -> None:
    os.environ.setdefault(
        "DIGITORN_MASTER_KEY",
        "KghE_laai9HFvcenA__24rr6tl6RUQC86N1RdPWW3Zg=",
    )
    from digitorn.core.credentials.oauth_flow import (
        PendingFlowStore,
        TokenExchange,
        build_auth_url,
    )
    from digitorn.core.credentials.oauth_providers import (
        OAuthProviderRegistry,
    )
    from digitorn.core.credentials.handlers.oauth2 import OAuth2Handler

    print("=" * 60)
    print("OAuth flow end-to-end test (mock provider)")
    print("=" * 60)

    # 1. Load registry from ~/.digitorn/oauth_providers.toml
    reg = OAuthProviderRegistry()
    reg.load()
    provider = reg.get("mockprovider")
    if provider is None:
        print(f"[FAIL] mockprovider not loaded. configured={reg.list_configured()}")
        sys.exit(1)
    print(f"[T3.1] provider loaded ok={provider.is_configured()}")

    # 2. PendingFlowStore.start()
    flow_store = PendingFlowStore()
    flow = await flow_store.start(
        provider=provider,
        user_id="test-user-oauth",
        app_id="test-app-oauth",
        scopes=["mock_read"],
    )
    auth_url = build_auth_url(flow)
    print(f"[T3.2] auth_url built: {auth_url[:80]}")

    # 3. Simulate the user's browser hitting the auth_url. The mock
    # redirects to the daemon's callback. We'll capture the code +
    # state from the redirect URL.
    callback_url = _follow_authorize(auth_url)
    print(f"[T3.3] mock redirected to: {callback_url[:80]}")
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query)
    code = qs.get("code", [""])[0]
    state = qs.get("state", [""])[0]
    if not code or state != flow.state:
        print(f"[FAIL] code/state mismatch code={code!r} state={state!r}")
        sys.exit(1)
    print(f"[T3.4] code={code[:15]} state matches")

    # 4. TokenExchange.exchange_code() - the daemon's callback would
    # do this internally.
    try:
        tokens = await TokenExchange.exchange_code(provider, code)
    except Exception as e:
        print(f"[FAIL] exchange_code: {e}")
        sys.exit(1)
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    if not access_token or not refresh_token:
        print(f"[FAIL] tokens missing: {tokens}")
        sys.exit(1)
    print(f"[T3.5] access_token: {access_token[:24]}..  refresh_token: {refresh_token[:24]}..")

    # T4 - refresh
    try:
        refreshed = await TokenExchange.refresh_token(provider, refresh_token)
    except Exception as e:
        print(f"[FAIL] refresh_token: {e}")
        sys.exit(1)
    new_access = refreshed.get("access_token", "")
    if not new_access or new_access == access_token:
        print(f"[FAIL] refresh did not yield new access_token: {refreshed}")
        sys.exit(1)
    print(f"[T4]   refreshed access_token: {new_access[:30]}..")

    # T5 - revoke (via the oauth2 handler).
    handler = OAuth2Handler()
    fake_credential = {
        "id": "test-cred-id",
        "provider_name": "mockprovider",
        "fields": {"access_token": new_access, "refresh_token": refresh_token},
    }
    try:
        await handler.revoke(fake_credential, schema_provider={})
    except Exception as e:
        print(f"[FAIL] revoke: {e}")
        sys.exit(1)

    # Verify the mock saw the revoke.
    state_resp = urllib.request.urlopen(
        "http://127.0.0.1:9998/__state", timeout=3,
    )
    import json as _json
    state_data = _json.loads(state_resp.read())
    if state_data["revoked_count"] >= 1:
        print(f"[T5]   revocation OK ({state_data['revoked_count']} tokens revoked)")
    else:
        print(f"[FAIL] mock did not see revoke: {state_data}")
        sys.exit(1)

    print()
    print("=" * 60)
    print("OAuth E2E: T3 + T4 + T5 PASS")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
