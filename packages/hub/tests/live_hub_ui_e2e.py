"""Real-browser end-to-end test of the Hub UI in digitorn_web.

Flow:
  1. POST /auth/login on the daemon to get a JWT.
  2. Inject it into localStorage as the Zustand `digitorn-auth`
     persisted state so the SPA boots authenticated.
  3. Navigate to /packages, click the Discover tab, wait for the
     /api/hub/search XHR to resolve.
  4. Count rendered HubSearchCards and the icons that actually
     loaded (not 404 / not broken).
  5. Open chess-coach detail page and check the same.
  6. Screenshot both views to /tmp for visual eye-balling.

Pre-req: web dev server up on :3000, daemon up on :8000.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

WEB = "http://127.0.0.1:3000"
DAEMON = "http://127.0.0.1:8000"
AUTH_SERVICE = "https://auth.digitorn.ai"
SHOTS = Path("/tmp")


def _expect(cond: bool, label: str) -> None:
    if not cond:
        print(f"  [FAIL] {label}")
        sys.exit(2)
    print(f"  [OK]   {label}")


def _login() -> dict:
    """Get a JWT from the central digitorn-auth service.

    Reads DIGITORN_TEST_EMAIL / DIGITORN_TEST_PASSWORD from env. Skips
    the test cleanly when those aren't set.
    """
    import os
    email = os.environ.get("DIGITORN_TEST_EMAIL")
    password = os.environ.get("DIGITORN_TEST_PASSWORD")
    if not email or not password:
        print("[skip] DIGITORN_TEST_EMAIL / DIGITORN_TEST_PASSWORD not set")
        sys.exit(0)
    r = httpx.post(
        f"{AUTH_SERVICE}/auth/login",
        json={"email": email, "password": password},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()


def _persist_state(creds: dict) -> str:
    """Build the Zustand `digitorn-auth` persisted blob the SPA
    expects on first boot."""
    state = {
        "state": {
            "accessToken": creds["access_token"],
            "refreshToken": creds.get("refresh_token") or creds["access_token"],
            "user": {
                "userId": creds.get("user_id", ""),
                "email": creds.get("email", ""),
                "displayName": creds.get("display_name") or creds.get("email", ""),
                "roles": creds.get("roles", []),
                "permissions": creds.get("permissions", []),
                "attributes": {},
            },
            "bridgeUrl": DAEMON,
        },
        "version": 0,
    }
    return json.dumps(state)


def main() -> int:
    print(f"[0] login against {AUTH_SERVICE}")
    creds = _login()
    token = creds["access_token"]
    _expect(len(token) > 60, "JWT minted")

    blob = _persist_state(creds)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})

        # Capture network for assertions.
        responses: list[dict] = []

        def _on_response(r):
            url = r.url
            if (
                "/api/hub/search" in url
                or "/api/hub/me" in url
                or "/api/v1/packages" in url and "/icon" in url
            ):
                responses.append({
                    "url": url,
                    "status": r.status,
                    "ct": r.headers.get("content-type", ""),
                })

        ctx.on("response", _on_response)

        # Inject auth before any page loads.
        ctx.add_init_script(
            f"window.localStorage.setItem('digitorn-auth', {json.dumps(blob)});"
        )

        page = ctx.new_page()

        print("[1] navigate /packages")
        page.goto(f"{WEB}/packages", wait_until="networkidle", timeout=30000)
        _expect("Packages" in page.title() or "Hub" in page.title() or page.url == f"{WEB}/packages", f"page loaded ({page.url})")

        print("[2] click Discover tab")
        # The PillTabBar puts the label "Discover" or t('discover')
        # - both end up as text on the button.
        try:
            page.get_by_text("Discover", exact=False).first.click(timeout=8000)
        except Exception as exc:
            print(f"  [warn] no Discover tab clickable: {exc}")

        page.wait_for_timeout(2500)  # let /api/hub/search resolve

        print("[3] check /api/hub/search hit")
        searches = [r for r in responses if "/api/hub/search" in r["url"]]
        _expect(len(searches) > 0, f"search XHR fired ({len(searches)})")
        for s in searches[:3]:
            print(f"    - {s['status']} {s['url'][:80]}")
        _expect(
            all(s["status"] == 200 for s in searches),
            "all search responses 200",
        )

        print("[4] count HubSearchCard images")
        # The cards render <img> for hits with icon_url. Count them.
        imgs = page.locator("img").all()
        urls = [im.get_attribute("src") or "" for im in imgs]
        hub_icon_imgs = [u for u in urls if "hub.digitorn.ai" in u and "/icon" in u]
        print(f"    total <img> on page: {len(urls)}")
        print(f"    Hub icon <img>: {len(hub_icon_imgs)}")
        for u in hub_icon_imgs[:5]:
            print(f"      - {u}")
        _expect(len(hub_icon_imgs) >= 1, "at least one Hub icon rendered")

        print("[5] verify icon URLs are 200 (not 404 / broken)")
        async_ok = []
        with httpx.Client(timeout=10) as c:
            for u in hub_icon_imgs[:3]:
                r = c.get(u)
                async_ok.append((r.status_code == 200, u))
                print(f"    {r.status_code}  {u[:80]}")
        _expect(all(ok for ok, _ in async_ok), "all icons return 200")

        print("[6] screenshot Discover")
        page.screenshot(path=str(SHOTS / "hub-discover.png"), full_page=True)
        print(f"    saved {SHOTS}/hub-discover.png")

        print("[7] open chess-coach detail")
        page.goto(
            f"{WEB}/hub/digitorn-official/chess-coach",
            wait_until="networkidle",
            timeout=30000,
        )
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOTS / "hub-detail-chess.png"), full_page=True)
        title = page.locator("h1, h2").first
        if title.count() > 0:
            print(f"    detail title: {title.text_content()!r}")
        # Detail page also has the icon (in the hero header).
        detail_icons = [
            (im.get_attribute("src") or "")
            for im in page.locator("img").all()
        ]
        detail_hub_icons = [u for u in detail_icons if "hub.digitorn.ai" in u]
        _expect(
            len(detail_hub_icons) >= 1,
            f"detail page has Hub icon ({len(detail_hub_icons)})",
        )

        browser.close()

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
