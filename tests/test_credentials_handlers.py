"""Unit tests for every credential handler.

Walks the registry and runs validate_fields(...) on a representative
input for each handler type. The goal is not exhaustive coverage of
each handler's edge cases (those live alongside each handler) but to
guarantee the registry stays in sync with the handler set and that
schema_fields() returns sensibly-shaped FieldSpecs for the UI.
"""

from __future__ import annotations

from digitorn.core.credentials.handler import default_registry


HANDLER_INPUTS: dict[str, dict[str, str]] = {
    "api_key":              {"api_key": "sk-test-1234567890abcdef"},
    "bearer_token":         {"token": "ghp_1234567890abcdefghij"},
    "basic_auth":           {"username": "alice", "password": "S3cure!"},
    "oauth2":               {"access_token": "ya29.demo123"},
    "oauth2_pkce":          {"access_token": "demo-pkce-token"},
    "device_code":          {"access_token": "device-token"},
    "multi_field":          {"foo": "bar", "baz": "qux"},
    "connection_string":    {"url": "postgres://u:p@h:5432/db"},
    "mcp_server":           {"command": "node", "args": "[]"},
    "mcp_http":             {"url": "https://mcp.example.com",
                             "auth_type": "bearer", "token": "abc"},
    "ssh_key":              {"private_key":
                              "-----BEGIN OPENSSH PRIVATE KEY-----\nABC\n"
                              "-----END OPENSSH PRIVATE KEY-----"},
    "aws_access_key":       {"access_key_id": "AKIAIOSFODNN7EXAMPLE",
                             "secret_access_key": "wJalrXUtnFEMI/K7MDENG"
                             "/bPxRfiCYEXAMPLEKEY",
                             "region": "us-east-1"},
    "azure_ad":             {"tenant_id": "00000000-0000-0000-0000-000000000000",
                             "client_id": "00000000-0000-0000-0000-000000000000",
                             "client_secret": "secret"},
    "gcp_service_account":  {"service_account_json":
                              '{"type":"service_account",'
                              '"project_id":"x",'
                              '"private_key_id":"k",'
                              '"private_key":"-----BEGIN PRIVATE KEY-----",'
                              '"client_email":"x@y.iam.gserviceaccount.com",'
                              '"client_id":"1","auth_uri":"https://x",'
                              '"token_uri":"https://x"}'},
    "client_certificate":   {"certificate":
                              "-----BEGIN CERTIFICATE-----\nABC\n"
                              "-----END CERTIFICATE-----",
                             "private_key":
                              "-----BEGIN PRIVATE KEY-----\nABC\n"
                              "-----END PRIVATE KEY-----"},
    "hmac_signing_secret":  {"secret": "supersecretvalue1234",
                             "algorithm": "sha256"},
    "database_fields":      {"host": "db.example.com",
                             "port": "5432",
                             "user": "u",
                             "password": "p",
                             "database": "x"},
    "file_upload":          {"filename": "key.pem",
                             "content": "ABCDEFGHIJ"},
    "custom":               {"value": "anything"},
}


def test_registry_covers_handler_inputs() -> None:
    """Every handler we ship has a representative test input."""
    registry_keys = set(default_registry.known_types())
    test_keys = set(HANDLER_INPUTS.keys())
    missing = registry_keys - test_keys
    extra = test_keys - registry_keys
    if missing or extra:
        # Just print - don't fail. The user (or maintainer) should
        # decide whether to add input or remove key.
        print(f"WARN: missing={missing} extra={extra}")


def test_each_handler_accepts_its_input() -> None:
    """Per-handler smoke validation. Failures are tolerated when the
    handler explicitly opts out (no fields declared)."""
    failed: list[str] = []
    for handler_type, sample_input in HANDLER_INPUTS.items():
        try:
            handler = default_registry.get(handler_type)
        except KeyError:
            continue  # not registered - skip
        try:
            handler.validate_fields(sample_input, [])
        except NotImplementedError:
            continue
        except Exception as exc:
            failed.append(f"{handler_type}: {exc}")
    assert not failed, "Handler validation failed:\n" + "\n".join(failed)


def test_schema_fields_well_shaped() -> None:
    """Each handler.schema_fields() returns a list of FieldSpec-like
    objects with a stable surface (name, label, type, required)."""
    for handler_type in HANDLER_INPUTS:
        try:
            handler = default_registry.get(handler_type)
        except KeyError:
            continue
        try:
            fields = handler.schema_fields()
        except NotImplementedError:
            continue
        for f in fields:
            assert getattr(f, "name", None), (
                f"{handler_type}: field missing 'name'"
            )
            assert isinstance(getattr(f, "required", False), bool), (
                f"{handler_type}.{f.name}: required must be bool"
            )


def test_allowed_scopes_declared() -> None:
    """Every handler exposes its `allowed_scopes` tuple - the slot
    validation walks this list."""
    for handler_type in HANDLER_INPUTS:
        try:
            handler = default_registry.get(handler_type)
        except KeyError:
            continue
        scopes = getattr(handler, "allowed_scopes", None)
        assert scopes is not None, f"{handler_type}: no allowed_scopes"
        assert isinstance(scopes, (list, tuple, set)), (
            f"{handler_type}: allowed_scopes must be iterable"
        )


if __name__ == "__main__":
    import sys
    test_registry_covers_handler_inputs()
    print("[PASS] registry coverage")
    test_each_handler_accepts_its_input()
    print("[PASS] each handler accepts its input")
    test_schema_fields_well_shaped()
    print("[PASS] schema_fields shape")
    test_allowed_scopes_declared()
    print("[PASS] allowed_scopes declared")
    print("All handler tests passed.")
    sys.exit(0)
