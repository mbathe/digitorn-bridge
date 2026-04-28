"""Full Hub flow against the prod daemon at api.digitorn.ai.

Covers every user-facing action that should work end-to-end:
  - Daemon login (real /auth/login, not minted JWT)
  - Hub session (/api/hub/me)
  - Search + detail + icon
  - Install with consent dance
  - Re-install (collision)
  - Direct upgrade endpoint with HUB source
  - Delete + verify gone from /api/apps
  - Reviews POST (rate-limited)
  - Reports POST (rate-limited)
  - Stats GET

Each step prints status and a one-line body excerpt.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any

import httpx

DAEMON = "https://api.digitorn.ai"
USER = "admin"
PASS = "admin1234admin"

# Pick a non-builtin package so DELETE actually removes everything.
TARGET_PUB = "community-tools"
TARGET_PKG = "alice-helper"

PASS_MARK = "[OK]"
FAIL_MARK = "[FAIL]"
SKIP_MARK = "[SKIP]"


class Probe:
    def __init__(self) -> None:
        self.client = httpx.Client(
            base_url=DAEMON, timeout=120, follow_redirects=True
        )
        self.token: str | None = None
        self.results: list[tuple[str, str]] = []

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def step(self, label: str, ok: bool, detail: str = "") -> None:
        mark = PASS_MARK if ok else FAIL_MARK
        self.results.append((mark, label))
        line = f"  {mark} {label}"
        if detail:
            line += f" :: {detail[:200]}"
        print(line)

    def skip(self, label: str, why: str) -> None:
        self.results.append((SKIP_MARK, label))
        print(f"  {SKIP_MARK} {label} :: {why}")

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.client.get(path, headers={**self.headers, **kw.pop("headers", {})}, **kw)

    def post(self, path: str, **kw: Any) -> httpx.Response:
        return self.client.post(path, headers={**self.headers, **kw.pop("headers", {})}, **kw)

    def delete(self, path: str, **kw: Any) -> httpx.Response:
        return self.client.delete(path, headers={**self.headers, **kw.pop("headers", {})}, **kw)


def main() -> int:
    p = Probe()

    print("\n=== A. Auth ===")
    r = p.client.post(
        "/auth/login",
        json={"username": USER, "password": PASS},
    )
    if r.status_code != 200:
        p.step("login", False, f"{r.status_code} {r.text[:200]}")
        return 2
    body = r.json()
    p.token = body.get("access_token")
    p.step("login", bool(p.token), f"got {len(p.token or '')}c access_token")

    me = p.get("/auth/me")
    p.step("/auth/me", me.status_code == 200, f"{me.status_code} email={me.json().get('email','-')}")

    print("\n=== B. Hub session ===")
    me2 = p.get("/api/hub/me")
    body2 = me2.json() if me2.status_code == 200 else {}
    bridge = body2.get("bridge_enabled")
    logged = body2.get("logged_in")
    p.step(
        "/api/hub/me",
        me2.status_code == 200,
        f"logged_in={logged} bridge_enabled={bridge} hub_url={body2.get('hub_url','-')}",
    )

    print("\n=== C. Hub browse (proxied) ===")
    s = p.get("/api/hub/search", params={"q": "alice", "page_size": 5})
    sb = s.json() if s.status_code == 200 else {}
    p.step(
        "search 'alice'",
        s.status_code == 200,
        f"total={sb.get('total','-')} hits={[(h.get('publisher_slug'),h.get('package_id')) for h in sb.get('hits',[])][:3]}",
    )

    d = p.get(f"/api/hub/packages/{TARGET_PUB}/{TARGET_PKG}")
    db = d.json() if d.status_code == 200 else {}
    p.step(
        "detail alice-helper",
        d.status_code == 200,
        f"latest={db.get('latest_version','-')} icon_url={(db.get('icon_url') or '')[:60]}",
    )

    rv = p.get(f"/api/hub/packages/{TARGET_PUB}/{TARGET_PKG}/reviews")
    rvb = rv.json() if rv.status_code == 200 else {}
    p.step(
        "reviews list",
        rv.status_code == 200,
        f"total={rvb.get('total','-')} avg={rvb.get('avg_rating','-')}",
    )

    st = p.get(
        f"/api/hub/packages/{TARGET_PUB}/{TARGET_PKG}/stats",
        params={"range": 7},
    )
    stb = st.json() if st.status_code == 200 else {}
    p.step(
        "stats range=7",
        st.status_code == 200,
        f"total_in_range={stb.get('total_downloads_in_range','-')}",
    )

    print("\n=== D. Install / consent dance ===")
    # First call without consent -> expect 409 with permissions
    r1 = p.post(
        "/api/hub/install",
        json={
            "publisher": TARGET_PUB,
            "package_id": TARGET_PKG,
            "accept_permissions": False,
        },
    )
    if r1.status_code == 409:
        det = r1.json().get("detail", {})
        p.step(
            "install w/o consent -> 409",
            isinstance(det, dict) and det.get("error") == "permissions_required",
            f"perms={det.get('permissions',{})}",
        )
    else:
        p.step("install w/o consent -> 409", False, f"{r1.status_code} {r1.text[:200]}")

    # Real install
    r2 = p.post(
        "/api/hub/install",
        json={
            "publisher": TARGET_PUB,
            "package_id": TARGET_PKG,
            "accept_permissions": True,
        },
    )
    rb2 = r2.json() if r2.status_code == 200 else {}
    p.step(
        "install accept=true",
        r2.status_code == 200 and rb2.get("installed") is True,
        f"{r2.status_code} version={rb2.get('version','-')} deployed={rb2.get('deployed')}",
    )

    print("\n=== E. Bug #2 — re-install of already-installed ===")
    r3 = p.post(
        "/api/hub/install",
        json={
            "publisher": TARGET_PUB,
            "package_id": TARGET_PKG,
            "accept_permissions": True,
        },
    )
    if r3.status_code == 200:
        p.step("re-install transparent upgrade", True, "fixed (200)")
    elif r3.status_code == 500:
        p.step(
            "re-install transparent upgrade",
            False,
            "STILL BROKEN: 500 PackageIdCollision unhandled",
        )
    else:
        p.step("re-install transparent upgrade", False, f"unexpected {r3.status_code} {r3.text[:200]}")

    print("\n=== F. Bug #1 — direct /upgrade with HUB source ===")
    r4 = p.post(
        f"/api/apps/{TARGET_PKG}/upgrade",
        json={
            "source_type": "hub",
            "source_uri": f"hub://{TARGET_PUB}/{TARGET_PKG}",
            "accept_permissions": True,
        },
    )
    if r4.status_code == 200:
        p.step("/upgrade HUB", True, "fixed (200)")
    elif r4.status_code == 501:
        p.step("/upgrade HUB", False, "STILL BROKEN: 501 'HUB upgrade deferred to v2'")
    else:
        p.step("/upgrade HUB", False, f"unexpected {r4.status_code} {r4.text[:200]}")

    print("\n=== G. Reviews POST (live, rate-limited) ===")
    rev = p.post(
        f"/api/hub/packages/{TARGET_PUB}/{TARGET_PKG}/reviews",
        json={"rating": 5, "body": "live test from prod e2e probe"},
    )
    if rev.status_code in (200, 201):
        p.step("submit review", True, f"{rev.status_code} id={rev.json().get('id','-')[:8]}")
    elif rev.status_code == 401:
        p.step("submit review", False, "401 — daemon Hub session not authenticated. Bridge or login needed.")
    elif rev.status_code == 403:
        p.step("submit review", False, "403 — owner of package can't review own package")
    elif rev.status_code == 429:
        p.skip("submit review", "429 rate limited (already reviewed in last hour)")
    else:
        p.step("submit review", False, f"{rev.status_code} {rev.text[:200]}")

    print("\n=== H. Reports POST (live, rate-limited) ===")
    rep = p.post(
        f"/api/hub/packages/{TARGET_PUB}/{TARGET_PKG}/reports",
        json={"reason": "other", "details": "live test from prod e2e probe — please ignore"},
    )
    if rep.status_code in (200, 201):
        p.step("submit report", True, f"{rep.status_code} id={rep.json().get('id','-')[:8]}")
    elif rep.status_code == 401:
        p.step("submit report", False, "401 — daemon Hub session not authenticated")
    elif rep.status_code == 429:
        p.skip("submit report", "429 already reported in last 24h")
    else:
        p.step("submit report", False, f"{rep.status_code} {rep.text[:200]}")

    print("\n=== I. Bug #3 — DELETE app and check it's gone ===")
    d1 = p.delete(f"/api/apps/{TARGET_PKG}")
    p.step(
        "DELETE response",
        d1.status_code == 200,
        f"{d1.status_code} body={str(d1.json())[:150]}",
    )
    apps = p.get("/api/apps")
    items = (apps.json() or {}).get("data", [])
    still = [a for a in items if (a.get("app_id") or a.get("appId")) == TARGET_PKG]
    if still:
        p.step(
            "alice-helper gone from /api/apps",
            False,
            f"STILL BROKEN: still visible (registry not cleaned, src={still[0].get('source_type')})",
        )
    else:
        p.step("alice-helper gone from /api/apps", True, f"{len(items)} other apps remain")

    print("\n=== Summary ===")
    n_ok = sum(1 for m, _ in p.results if m == PASS_MARK)
    n_fail = sum(1 for m, _ in p.results if m == FAIL_MARK)
    n_skip = sum(1 for m, _ in p.results if m == SKIP_MARK)
    print(f"  {n_ok} pass, {n_fail} fail, {n_skip} skipped")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
