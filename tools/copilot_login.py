"""One-shot GitHub Copilot device-flow login.

Usage::

    py -3.12 tools/copilot_login.py

Why this script exists
----------------------
GitHub's ``/copilot_internal/v2/token`` endpoint (the one our daemon's
``github_copilot`` provider hits to exchange a GitHub identity for a
Copilot session token) silently 404s for OAuth tokens issued by any
client_id other than VS Code Copilot's. That includes:

  - Personal access tokens created via github.com/settings/tokens
  - gh CLI's tokens (client_id ``178c6fc778ccc68e1d6a``)
  - Any custom OAuth app the user might create

Aider, copilot.lua, avante.nvim and the other open-source Copilot
clients all work around this by piggy-backing on VS Code Copilot's
own OAuth client_id ``Iv1.b507a08c87ecfe98``. Tokens issued under
that client_id are whitelisted for the Copilot endpoints.

The script runs the standard OAuth 2.0 device flow against that
client_id, polls until the user authorizes in their browser, and
prints the resulting ``ghu_...`` token. Paste that token into the
``github_copilot`` credential in the digitorn web UI and the daemon
will be able to call api.githubcopilot.com.

The token does not expire (until the user revokes it from
https://github.com/settings/applications) so this script only needs
to run once per machine.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

CLIENT_ID = "Iv1.b507a08c87ecfe98"  # VS Code Copilot Chat OAuth app
DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"

# Headers that mimic a recent VS Code Copilot Chat extension. Without
# them GitHub's device-flow returns 422 because it gates the flow on
# what looks like a real editor client.
EDITOR_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Editor-Version": "vscode/1.99.3",
    "Editor-Plugin-Version": "copilot-chat/0.27.0",
    "User-Agent": "GithubCopilot/1.270.0",
}


def _http_post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in EDITOR_HEADERS.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = "(no body)"
        raise SystemExit(
            f"\nHTTP {exc.code} from {url}: {detail}"
        ) from exc


def main() -> int:
    print("==========================================================")
    print(" GitHub Copilot device-flow login (digitorn)")
    print("==========================================================\n")

    # 1. Ask GitHub for a device code.
    print("Requesting device code from GitHub...")
    code_resp = _http_post_json(
        DEVICE_CODE_URL,
        {"client_id": CLIENT_ID, "scope": "read:user"},
    )
    user_code = code_resp.get("user_code", "")
    device_code = code_resp.get("device_code", "")
    verify_uri = code_resp.get("verification_uri", "https://github.com/login/device")
    interval = int(code_resp.get("interval", 5))
    expires_in = int(code_resp.get("expires_in", 900))
    if not user_code or not device_code:
        print(f"Unexpected response: {code_resp}")
        return 1

    # 2. Show the code to the user.
    print()
    print("--- ACTION REQUIRED ---")
    print(f"  1. Open this URL in any browser:   {verify_uri}")
    print(f"  2. Enter this 8-character code:    \033[1;36m{user_code}\033[0m")
    print(f"  3. Approve the 'GitHub for VS Code' app.")
    print()
    print(f"You have {expires_in // 60} minutes. Polling GitHub every "
          f"{interval}s...\n")

    # Best-effort: pop the URL in the default browser.
    try:
        import webbrowser
        webbrowser.open(verify_uri)
    except Exception:
        pass

    # 3. Poll the token endpoint until the user authorizes (or it times out).
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        try:
            tok_resp = _http_post_json(
                TOKEN_URL,
                {
                    "client_id": CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
        except SystemExit:
            print("(transient HTTP error, retrying...)")
            continue
        if "access_token" in tok_resp:
            access_token = tok_resp["access_token"]
            print()
            print("==========================================================")
            print(" SUCCESS")
            print("==========================================================")
            print(f"\nYour Copilot OAuth token:\n\n  \033[1;32m{access_token}\033[0m\n")
            print("Next step:")
            print("  1. Open the digitorn web UI -> /settings/credentials")
            print("  2. Edit (or create) the credential 'gh_copilot_main'")
            print("  3. Provider: GitHub Copilot, Field 'api_key': paste the token above")
            print("  4. Save.")
            print()
            print("This token does not expire. Revoke at any time at")
            print("https://github.com/settings/applications.\n")
            return 0
        err = tok_resp.get("error", "")
        if err == "authorization_pending":
            # Normal - user hasn't typed the code yet.
            print(f"  ... still waiting for {user_code} (will retry in {interval}s)")
            continue
        if err == "slow_down":
            interval += 5
            print(f"  GitHub asked us to slow down, new interval = {interval}s")
            continue
        if err in ("expired_token", "access_denied", "incorrect_device_code"):
            print(f"\nFlow ended: {err}. {tok_resp.get('error_description', '')}")
            return 1
        print(f"  Unexpected response, retrying: {tok_resp}")

    print("\nDevice code expired before you authorized. Re-run the script.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
