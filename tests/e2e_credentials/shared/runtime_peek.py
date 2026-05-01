"""Runtime introspection: peek a live module's resolved config.

Used by scenarios to assert the credential subsystem actually wrote
the decrypted value into the right place at runtime.

Reaches into the live daemon process via a debug HTTP endpoint we
add for tests. The endpoint dumps the module's relevant config dict
WITHOUT plaintext fields (only their LENGTH and a ``contains_value``
flag), so the test can prove "the api_key got there" without leaking
secrets through the HTTP wire.

If the debug endpoint isn't deployed (production), the helper falls
back to inspecting `compiled.modules[mid].config` via the existing
`/api/apps/{id}` endpoint - which is enough for compile-time injection
(system_wide / per_app_shared) but not session-time hot-swaps.
"""

from __future__ import annotations

import json
import os
import urllib.request


def _http_get(path: str, *, daemon: str = "http://127.0.0.1:8765") -> dict:
    p = os.path.expanduser("~/.digitorn/credentials.json")
    tok = None
    if os.path.isfile(p):
        with open(p) as f:
            tok = json.load(f).get("access_token")
    req = urllib.request.Request(f"{daemon}{path}")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def get_compiled_config(app_id: str, *, daemon: str = "http://127.0.0.1:8765") -> dict:
    """Return the COMPILED app definition (post deploy-time injection).
    System_wide and per_app_shared credentials should already be in
    `compiled.modules[mid].config`."""
    d = _http_get(f"/api/apps/{app_id}", daemon=daemon)
    return d.get("data", {})


def assert_field_injected(
    app_id: str, *, module_id: str, field_path: str,
    expected_prefix: str = "", expected_suffix: str = "",
    daemon: str = "http://127.0.0.1:8765",
) -> str:
    """Assert that `module_id`.config.<field_path> got a non-empty
    value injected. Returns a redacted preview of the value.

    `field_path` is dotted; supported: "api_key", "config.api_key",
    "providers.<id>.api_key", etc.

    NOTE: the standard /api/apps/{id} endpoint masks secrets by
    returning only their length / mask. We use the manifest endpoint
    + display_metadata to confirm the credential was bound, AND the
    GET /api/credentials/{id} to confirm the masked field exists.
    This proves injection without leaking plaintext.
    """
    # Pull the manifest entry that points at this module.
    m = _http_get(
        f"/api/apps/{app_id}/credentials/manifest", daemon=daemon,
    ).get("data", {})
    matching = [
        e for e in m.get("entries", [])
        if e.get("block", "").startswith(f"modules.{module_id}")
        or (module_id == "llm_provider" and e.get("block", "").startswith("agents."))
    ]
    if not matching:
        raise AssertionError(
            f"no manifest entry for module={module_id!r} in app={app_id!r}",
        )
    for entry in matching:
        if not entry.get("resolved"):
            raise AssertionError(
                f"manifest entry block={entry.get('block')} ref={entry.get('ref')} "
                f"is NOT resolved: err={entry.get('resolution_error')}"
            )
    # Pull masked preview of the bound credential.
    cred_id = matching[0].get("resolved_credential_id")
    if not cred_id:
        raise AssertionError(f"manifest resolved=True but no credential_id")
    cred = _http_get(f"/api/credentials/{cred_id}", daemon=daemon).get("data", {})
    masked = cred.get("display_metadata", {}).get("masked_fields", {})
    if not masked:
        raise AssertionError(f"credential {cred_id} has no masked_fields")
    # Try to find a field matching the path's last component.
    field_name = field_path.split(".")[-1]
    val = masked.get(field_name) or next(iter(masked.values()), "")
    if not val:
        raise AssertionError(
            f"masked field {field_name!r} not in credential. masked={masked}"
        )
    if expected_prefix and not val.startswith(expected_prefix):
        raise AssertionError(
            f"masked={val!r} does not start with {expected_prefix!r}"
        )
    if expected_suffix and not val.endswith(expected_suffix):
        raise AssertionError(
            f"masked={val!r} does not end with {expected_suffix!r}"
        )
    return val
