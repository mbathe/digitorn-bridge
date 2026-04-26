"""Comprehensive error-classifier coverage test.

Exercises every error category the daemon can surface. Proves that the
classifier maps them to stable (``category``, ``code``, ``retry``)
triples — the contract the Flutter client switches on to render the
right UI (banner, toast, dialog, picker, etc.).

Categories enumerated here:

    billing            — API-level billing (insufficient funds, 402)
    quota              — daemon rate/token/request limits (429-like,
                         structured)
    auth               — bad key, expired token, 401, 403
    rate_limit         — provider 429 / overloaded_error
    context_overflow   — token-limit exceeded for the model
    timeout            — execution timeout (tool, LLM, approval)
    network            — connect/DNS/SSL/stream interrupted
    provider           — provider-side 5xx, model not found,
                         malformed-response
    content_filter     — moderation / safety rejection
    approval_required  — human-in-the-loop gate
    credential_required
    credential_auth_required
    validation         — Pydantic / IML / bad params
    bad_request        — 400, malformed payload
    cancelled          — user abort (CancelledError)
    storage            — DB / disk full / integrity
    tool_error         — action execution / MCP server crash
    concurrency        — session lock, double-turn
    permission         — RBAC denied
    security           — intent verification / injection scan
    internal           — fallback only — SHOULD never be the match
                         for a known symptom

This test is PURE — it doesn't need the daemon. It constructs
exceptions and feeds them directly into ``_classify_error``.

Run: py -3.12 tools/test_error_classifier_coverage.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages"))
sys.stdout.reconfigure(encoding="utf-8")

from digitorn.core.api.apps_v2._errors import _classify_error

results: list[tuple[str, bool, str]] = []


def assert_classification(
    case: str, exc: Exception, *,
    category: str, code: str | None = None,
    retry: bool | None = None,
) -> None:
    got = _classify_error(exc)
    ok_cat = got.get("category") == category
    ok_code = code is None or got.get("code") == code
    ok_retry = retry is None or got.get("retry") == retry
    ok = ok_cat and ok_code and ok_retry
    detail = (
        f"expected category={category!r}" +
        (f" code={code!r}" if code else "") +
        (f" retry={retry}" if retry is not None else "") +
        f" → got category={got.get('category')!r} "
        f"code={got.get('code')!r} retry={got.get('retry')}"
    )
    tag = "[PASS]" if ok else "[FAIL]"
    print(f"{tag} {case}")
    if not ok:
        print(f"       {detail}")
    results.append((case, ok, detail))


# ── 1. Billing (API provider insufficient funds) ──────────────────────
assert_classification(
    "OpenAI insufficient_quota",
    RuntimeError(
        "Error code: 429 - {'error': {'message': 'You exceeded your "
        "current quota', 'type': 'insufficient_quota'}}"
    ),
    category="billing", code="insufficient_balance", retry=False,
)
assert_classification(
    "DeepSeek insufficient balance",
    RuntimeError(
        "Error code: 402 - {'error': {'message': 'Insufficient Balance'}}"
    ),
    category="billing", code="insufficient_balance", retry=False,
)
assert_classification(
    "Anthropic credit balance",
    RuntimeError(
        "Your credit balance is too low to access the Claude API. "
        "Please go to Plans & Billing to upgrade or purchase credits."
    ),
    category="billing", code="insufficient_balance", retry=False,
)

# ── 2. Auth ───────────────────────────────────────────────────────────
assert_classification(
    "OpenAI 401 invalid_api_key",
    RuntimeError(
        "Error code: 401 - {'error': {'message': 'Incorrect API key "
        "provided', 'type': 'invalid_api_key'}}"
    ),
    category="auth", code="auth_error", retry=False,
)
assert_classification(
    "403 Forbidden",
    RuntimeError("Error code: 403 - Forbidden"),
    category="auth", code="auth_error", retry=False,
)
assert_classification(
    "token expired",
    RuntimeError("Your token has expired. Please sign in again."),
    category="auth", code="auth_error", retry=False,
)

# ── 3. Rate limit (API provider) ──────────────────────────────────────
assert_classification(
    "OpenAI 429 rate_limit_exceeded",
    RuntimeError(
        "Error code: 429 - {'error': {'message': 'Rate limit reached', "
        "'type': 'rate_limit_exceeded'}}"
    ),
    category="rate_limit", code="rate_limited", retry=True,
)
assert_classification(
    "Anthropic overloaded_error",
    RuntimeError(
        "Error code: 529 - {'error': {'type': 'overloaded_error', "
        "'message': 'Overloaded'}}"
    ),
    category="rate_limit", code="rate_limited", retry=True,
)

# ── 4. Context overflow ───────────────────────────────────────────────
assert_classification(
    "OpenAI context_length_exceeded",
    RuntimeError(
        "This model's maximum context length is 128000 tokens. Your "
        "messages resulted in 150000 tokens. Please reduce the length."
    ),
    category="provider", code="context_overflow", retry=False,
)

# ── 5. Model not found ────────────────────────────────────────────────
assert_classification(
    "OpenAI 404 not_found_error",
    RuntimeError(
        "Error code: 404 - {'error': {'message': 'The model does not "
        "exist', 'type': 'not_found_error'}}"
    ),
    category="provider", code="model_not_found", retry=False,
)

# ── 6. Network ────────────────────────────────────────────────────────
class FakeConnectTimeout(Exception):
    pass
FakeConnectTimeout.__name__ = "ConnectTimeout"

assert_classification(
    "connect timeout",
    FakeConnectTimeout("connection timed out"),
    category="network",
)
assert_classification(
    "DNS failure",
    ConnectionError("Could not resolve host api.example.com"),
    category="network",
)
assert_classification(
    "SSL handshake",
    RuntimeError("SSL handshake failed"),
    category="network",
)

# ── 7. Provider 5xx ───────────────────────────────────────────────────
assert_classification(
    "provider 502",
    RuntimeError("Error code: 502 - Bad Gateway"),
    category="provider", code="provider_error", retry=True,
)

# ── 8. Session busy ──────────────────────────────────────────────────
assert_classification(
    "session lock timeout",
    RuntimeError("session lock timeout after 30s"),
    category="concurrency", code="session_busy", retry=False,
)

# ── 9. Daemon quota exceeded (structured) — PROVIDER-AGNOSTIC ────────
# The daemon's own rate limiter (core/quota.py) raises
# QuotaExceededError with structured scope/metric/limit/reset_at. The
# client needs ``category=quota`` distinct from ``billing`` so it can
# show "You've hit your 1000-req/day plan limit" rather than
# "Top up your API key".
try:
    from digitorn.core.quota import (
        QuotaExceededError as _QE, CounterState as _CS,
    )
    import time as _t
    _state = _CS(
        metric="requests",
        window="5h",
        current=1000, limit=1000,
        reset_at=_t.time() + 3600,
        over=True,
        scope="user:abc",
    )
    assert_classification(
        "daemon QuotaExceededError",
        _QE(_state),
        category="quota", code="quota_exceeded", retry=True,
    )
except Exception as exc:
    print(f"[SKIP] daemon QuotaExceededError — couldn't construct: {exc}")
    results.append(("daemon QuotaExceededError", False, f"{exc}"))

# ── 10. Approval required ────────────────────────────────────────────
try:
    from digitorn.modules.exceptions import ApprovalRequiredError as _AR
    assert_classification(
        "approval required (human-in-the-loop gate)",
        _AR(action_id="filesystem.rm", plan_id="plan-42"),
        category="approval", code="approval_required", retry=False,
    )
except Exception as exc:
    results.append(("ApprovalRequiredError construct", False, f"{exc}"))

# ── 11. Timeout (distinct from network) ──────────────────────────────
import asyncio
assert_classification(
    "asyncio.TimeoutError (LLM slow)",
    asyncio.TimeoutError("Tool call exceeded 30s budget"),
    category="timeout", code="timeout_error", retry=True,
)
try:
    from digitorn.modules.exceptions import ExecutionTimeoutError as _ET
    assert_classification(
        "ExecutionTimeoutError",
        _ET(action_id="shell.bash", timeout_seconds=30),
        category="timeout", code="timeout_error", retry=True,
    )
except Exception as exc:
    results.append(("ExecutionTimeoutError", False, f"{exc}"))

# ── 12. Cancelled (user abort) ───────────────────────────────────────
assert_classification(
    "asyncio.CancelledError (user abort)",
    asyncio.CancelledError("turn aborted by user"),
    category="cancelled", code="cancelled", retry=False,
)

# ── 13. Validation ────────────────────────────────────────────────────
try:
    from pydantic import ValidationError, BaseModel
    class _M(BaseModel):
        x: int
    try:
        _M(x="not-an-int")
    except ValidationError as exc:
        assert_classification(
            "Pydantic ValidationError",
            exc,
            category="validation", code="validation_error", retry=False,
        )
except Exception as exc:
    results.append(("Pydantic ValidationError", False, f"{exc}"))

try:
    from digitorn.modules.exceptions import IMLValidationError as _IML
    assert_classification(
        "IMLValidationError (tool params)",
        _IML("params.foo missing required field"),
        category="validation", code="validation_error", retry=False,
    )
except Exception as exc:
    results.append(("IMLValidationError", False, f"{exc}"))

# ── 14. Bad request (400) ────────────────────────────────────────────
assert_classification(
    "OpenAI 400 invalid_request_error",
    RuntimeError(
        "Error code: 400 - {'error': {'message': 'Messages array is "
        "empty', 'type': 'invalid_request_error'}}"
    ),
    category="provider", code="bad_request", retry=False,
)

# ── 15. Content filter ───────────────────────────────────────────────
assert_classification(
    "OpenAI content_filter",
    RuntimeError(
        "Error code: 400 - {'error': {'message': 'Your request was "
        "rejected by the safety system', 'code': 'content_filter'}}"
    ),
    category="content_filter", code="content_policy_violation", retry=False,
)
assert_classification(
    "Anthropic safety",
    RuntimeError(
        "stop_reason='safety': output rejected by the moderation layer"
    ),
    category="content_filter", code="content_policy_violation", retry=False,
)

# ── 16. Permission denied (RBAC) ─────────────────────────────────────
try:
    from digitorn.modules.exceptions import PermissionDeniedError as _PDE
    assert_classification(
        "PermissionDeniedError (RBAC)",
        _PDE(action="rm", module="filesystem", profile="restricted"),
        category="security", code="permission_denied", retry=False,
    )
except Exception as exc:
    results.append(("PermissionDeniedError", False, f"{exc}"))

# ── 17. Credential flow (picker dialog) — already handled ────────────
try:
    from digitorn.core.credentials.store import CredentialMissing
    try:
        raise CredentialMissing(
            provider="openai", field="api_key",
            app_id="demo-app", user_id="u123",
        )
    except CredentialMissing as exc:
        r = _classify_error(exc)
        results.append((
            "CredentialMissing (picker dialog)",
            r.get("code") == "credential_required",
            f"code={r.get('code')}",
        ))
        print(f"[{'PASS' if r.get('code') == 'credential_required' else 'FAIL'}] "
              f"CredentialMissing → code={r.get('code')!r}")
except Exception as exc:
    results.append(("CredentialMissing", False, f"{exc}"))

# ── 18. Storage (DB / disk) ──────────────────────────────────────────
assert_classification(
    "sqlite disk full",
    RuntimeError("database or disk is full"),
    category="storage", code="storage_error", retry=False,
)
assert_classification(
    "sqlite locked",
    RuntimeError("database is locked"),
    category="storage", code="storage_locked", retry=True,
)

# ── 19. Tool / Worker error ──────────────────────────────────────────
try:
    from digitorn.modules.exceptions import (
        ActionExecutionError as _AEE,
        WorkerCrashedError as _WCE,
    )
    assert_classification(
        "ActionExecutionError (tool raised)",
        _AEE(
            module_id="filesystem", action="write",
            cause=OSError("ENOSPC: no space left on device"),
        ),
        category="tool", code="tool_error", retry=True,
    )
    assert_classification(
        "WorkerCrashedError (sidecar died)",
        _WCE(module_id="preview", exit_code=137),
        category="tool", code="worker_crashed", retry=True,
    )
except Exception as exc:
    results.append(("ActionExecutionError/WorkerCrashed", False, f"{exc}"))


# ── 20. Fallback must NOT match well-known symptoms ──────────────────
# This ensures we don't silently drop errors into the generic bucket.
_UNEXPECTED_INTERNAL: list[tuple[str, str]] = []
for case_name, ok, detail in results:
    if not ok and "internal" in (detail or ""):
        _UNEXPECTED_INTERNAL.append((case_name, detail))

print()
print("=" * 70)
total = len(results)
passed = sum(1 for _, ok, _ in results if ok)
print(f"ERROR CLASSIFIER COVERAGE: {passed}/{total}")
print("=" * 70)
if passed != total:
    print("\nFailures (each of these = an error the client can't render properly):")
    for n, ok, det in results:
        if not ok:
            print(f"  [FAIL] {n}\n         {det[:300]}")

sys.exit(0 if passed == total else 1)
