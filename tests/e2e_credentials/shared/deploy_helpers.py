"""Deploy + manifest helpers for e2e tests."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


def _bearer() -> str | None:
    """User-issued JWT from credentials.json (carries ``perms``);
    falls back to the device-pair token from LocalDeviceAuth."""
    p = os.path.expanduser("~/.digitorn/credentials.json")
    try:
        with open(p) as f:
            tok = json.load(f).get("access_token")
        if tok:
            return tok
    except Exception:
        pass
    try:
        from digitorn.core.auth.local_device import LocalDeviceAuth
        auth = LocalDeviceAuth.load()
        return auth.device_token
    except Exception:
        return None


def _http(
    method: str, url: str, body: dict | None = None,
    *, timeout: int = 30,
) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    tok = _bearer()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except Exception as e:
        return 0, {"_error": str(e)}


def deploy_app(
    yaml_path: str,
    *,
    daemon: str = "http://127.0.0.1:8765",
    timeout_s: int = 60,
) -> dict:
    """Deploy a YAML at `yaml_path` and wait for `deployed` status.
    Returns the deploy response dict. Raises on timeout."""
    abs_path = os.path.abspath(yaml_path).replace("\\", "/")
    s, d = _http(
        "POST", f"{daemon}/api/apps/deploy",
        body={"yaml_path": abs_path, "force": True},
    )
    if s not in (200, 201, 202):
        raise RuntimeError(f"deploy failed: {s} {d}")
    app_id = d.get("data", {}).get("app_id", "")
    if not app_id:
        raise RuntimeError(f"no app_id in deploy response: {d}")

    # Async deploy - poll until the manifest endpoint returns 200.
    # Manifest is the canonical "ready" signal because it only
    # resolves after _build_and_deploy registered the app in
    # manager._deployed AND the credentials.compile path ran.
    # A few consecutive successful manifest hits guards against
    # rare in-flight deregistration races between teardown of a
    # previous test and the new deploy.
    deadline = time.time() + timeout_s
    consecutive_ok = 0
    while time.time() < deadline:
        s3, _ = _http(
            "GET",
            f"{daemon}/api/apps/{app_id}/credentials/manifest",
        )
        if s3 == 200:
            consecutive_ok += 1
            if consecutive_ok >= 2:
                # Re-fetch the GET /apps/{id} for completeness in
                # the return value.
                _, st = _http("GET", f"{daemon}/api/apps/{app_id}")
                return st
        else:
            consecutive_ok = 0
        time.sleep(1)
    raise RuntimeError(f"deploy of {app_id} did not complete within {timeout_s}s")


def get_manifest(
    app_id: str,
    *,
    daemon: str = "http://127.0.0.1:8765",
) -> dict:
    """Return the credentials manifest for an app."""
    s, d = _http(
        "GET", f"{daemon}/api/apps/{app_id}/credentials/manifest",
    )
    if s != 200:
        raise RuntimeError(f"manifest GET failed: {s} {d}")
    return d.get("data", {})


def assert_credential_resolved(app_id: str, ref: str, *, daemon: str = "http://127.0.0.1:8765") -> dict:
    """Assert a specific credential ref is resolved for the given app.
    Returns the manifest entry on success, raises AssertionError on failure."""
    m = get_manifest(app_id, daemon=daemon)
    for entry in m.get("entries", []):
        if entry.get("ref") == ref:
            assert entry.get("resolved"), (
                f"credential ref {ref!r} not resolved: "
                f"err={entry.get('resolution_error')} "
                f"alts={entry.get('available')}"
            )
            return entry
    raise AssertionError(
        f"no manifest entry for ref={ref!r}. "
        f"entries={[e.get('ref') for e in m.get('entries', [])]}"
    )


def undeploy_app(
    app_id: str,
    *,
    daemon: str = "http://127.0.0.1:8765",
) -> None:
    """Best-effort undeploy. Test cleanup."""
    _http("POST", f"{daemon}/api/apps/{app_id}/undeploy")
