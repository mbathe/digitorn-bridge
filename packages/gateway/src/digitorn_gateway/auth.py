"""Stateless JWT validation for the digitorn gateway.

The gateway is a SERVICE, not a client of `auth.digitorn.ai`. It never
authenticates upstream and never asks the auth service to authorise a
request. The only thing it pulls from auth - and only at boot + on a
slow background timer - is the public JWKS used to verify token
signatures locally. Once cached, every incoming request is verified
without any network I/O. This is the standard OIDC pattern used by
Auth0, AWS Cognito, Clerk, Firebase Auth, etc.

Hot-path cost per request (cached JWKS, hot CPython):

    Authorization header parse  : ~1 µs
    JWT decode + RSA verify     : ~300-800 µs (cryptography handles it)
    iss / sub / type checks     : ~5 µs
    -------------------------------------
    total                       : < 1 ms

Background tasks owned by `main.lifespan`:

    JWKS initial fetch  : at boot, retried once on failure
    JWKS periodic refresh: every `auth_jwks_refresh_seconds` (15 min default)

Token expiration is enforced by PyJWT itself (`exp` claim). When a
client sends an expired token we return 401 - the user re-authenticates
upstream and retries. We don't try to refresh tokens at the gateway.

Refresh tokens are explicitly REJECTED (`type: refresh` in the claims):
they are meant to be exchanged at the auth service for a new access
token, never used directly to call resource servers like this one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Header, HTTPException, status

logger = logging.getLogger(__name__)


# ── Tunables ───────────────────────────────────────────────────────


# Small leeway absorbs benign clock skew between the auth service and
# the gateway (NTP drift, container boot delay). 5 s is enough for
# any healthy infrastructure; bigger windows weaken the `exp` check.
_CLOCK_SKEW_LEEWAY_S = 5

# HTTP timeout for the JWKS fetch. The endpoint is supposed to serve a
# small static-ish JSON; if it can't answer in 10 s, something is wrong
# upstream and we'd rather log + skip the refresh than block forever.
_JWKS_FETCH_TIMEOUT_S = 10.0


# ── Principal ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GatewayPrincipal:
    """Immutable view of an authenticated caller. The route handlers
    use it to gate quota, logging, and provider routing.
    """

    user_id: str
    email: str | None
    roles: tuple[str, ...]
    token_type: str
    expires_at: int
    jti: str | None
    raw_claims: dict[str, Any]


# ── JWKS cache ─────────────────────────────────────────────────────


@dataclass(slots=True)
class _CachedKey:
    """A JWKS entry with the heavy `RSAPublicKey` parsed once and reused.

    Building the cryptography public key from a JWK dict costs ~100-500 µs
    per call (BIG INTEGER decode + key construction). Doing that on every
    request would dominate the verify cost; instead we parse it once at
    fetch time and hand the precomputed object to PyJWT directly.
    """

    public_key: Any  # cryptography.hazmat.primitives.asymmetric.rsa.RSAPublicKey
    raw_jwk: dict[str, Any]


class _JwksCache:
    """In-process JWKS cache with single-flight refresh.

    Threading: the gateway runs on a single asyncio loop; the only
    "thread" interleaving comes from concurrent coroutines. The
    `_refresh_lock` ensures that a thundering herd of requests (e.g.
    all hitting verify() right after a key rotation invalidates the
    current set) collapses to ONE network call instead of N.

    Failure mode: when a refresh raises (network blip, auth.digitorn.ai
    rolling deploy), the OLD cache is preserved. Tokens that match a
    previously-known kid still verify; only new-kid tokens fail until
    the next refresh succeeds. Combined with the periodic background
    refresh in `main.lifespan`, this gives a transparent recovery from
    transient outages.
    """

    def __init__(self, jwks_url: str) -> None:
        self._jwks_url = jwks_url
        self._keys: dict[str, _CachedKey] = {}
        self._fetched_at: float = 0.0
        self._refresh_lock = asyncio.Lock()

    async def fetch(self) -> int:
        """Re-pull the JWKS from the auth service. Returns the new key
        count. Single-flight: concurrent callers wait for the same
        in-flight fetch instead of fanning out N requests upstream.
        Preserves the old cache on failure.
        """
        async with self._refresh_lock:
            try:
                async with httpx.AsyncClient(timeout=_JWKS_FETCH_TIMEOUT_S) as client:
                    resp = await client.get(self._jwks_url)
                    resp.raise_for_status()
                    data = resp.json()
            except Exception as exc:
                # Keep the old cache intact - clients with already-known
                # kids continue to verify successfully. New kids will
                # fail the next refresh attempt.
                logger.warning(
                    "jwks_fetch_failed url=%s err=%s (old cache preserved, "
                    "size=%d)",
                    self._jwks_url, exc, len(self._keys),
                )
                raise

            new_keys: dict[str, _CachedKey] = {}
            for raw in data.get("keys") or []:
                kid = raw.get("kid")
                if not kid:
                    # JWKS entries without a kid are unusable for kid-based
                    # lookup. Skip + log so an operator notices.
                    logger.warning("jwks_entry_missing_kid skipped raw=%s", raw)
                    continue
                try:
                    pub = _jwk_to_public_key(raw)
                except Exception as exc:
                    logger.warning(
                        "jwks_entry_invalid kid=%s err=%s", kid, exc,
                    )
                    continue
                new_keys[kid] = _CachedKey(public_key=pub, raw_jwk=raw)

            if not new_keys:
                # An empty payload is suspicious - more likely a misconfig
                # than a real "no keys" state. Keep the old cache.
                logger.warning(
                    "jwks_response_empty url=%s (old cache preserved, size=%d)",
                    self._jwks_url, len(self._keys),
                )
                return len(self._keys)

            self._keys = new_keys
            self._fetched_at = time.monotonic()
            logger.info(
                "jwks_refreshed url=%s keys=%d kids=%s",
                self._jwks_url, len(new_keys), sorted(new_keys.keys()),
            )
            return len(new_keys)

    def find(self, kid: str | None) -> _CachedKey | None:
        if kid is None:
            # Tokens MUST carry a kid header. Accepting kid-less tokens
            # would let an attacker exploit a cache that happens to
            # have only one key. Refuse explicitly.
            return None
        return self._keys.get(kid)

    @property
    def empty(self) -> bool:
        return not self._keys

    @property
    def keys_count(self) -> int:
        return len(self._keys)


_jwks: _JwksCache | None = None


def init_jwks(jwks_url: str) -> _JwksCache:
    """Construct the process-wide JWKS cache. Called once from
    `main.lifespan` at boot. Subsequent calls replace the previous
    instance (useful for tests).
    """
    global _jwks
    _jwks = _JwksCache(jwks_url)
    return _jwks


def get_jwks() -> _JwksCache | None:
    return _jwks


# ── Verification ───────────────────────────────────────────────────


def _jwk_to_public_key(jwk: dict[str, Any]) -> Any:
    """Convert a JWK dict into a cryptography RSA public key object.

    Done once per fetch (cached) - this is the expensive part of the
    verify path. PyJWT's RSAAlgorithm.from_jwk handles all the JWK
    parameter unpacking (n, e, kty=RSA) and BigInteger decoding.
    """
    from jwt.algorithms import RSAAlgorithm
    return RSAAlgorithm.from_jwk(json.dumps(jwk))


async def _verify_token(
    token: str,
    *,
    expected_issuer: str,
) -> dict[str, Any]:
    """Verify a JWT against the cached JWKS.

    Returns the decoded claims dict on success; raises HTTPException
    with the appropriate 401/503 status on any failure.
    """
    import jwt as pyjwt

    cache = get_jwks()
    if cache is None:
        # The lifespan hook didn't run - serious misconfiguration. Fail
        # closed; never accept a token without a JWKS source.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="jwks_not_initialised",
        )

    if cache.empty:
        # First request after boot when the initial fetch failed. Try
        # once more synchronously; if it still fails, return 503 so the
        # client retries instead of being held indefinitely.
        try:
            await cache.fetch()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="jwks_unavailable",
            ) from exc

    # Parse the unverified header to pick the right key.
    try:
        header = pyjwt.get_unverified_header(token)
    except Exception as exc:
        raise HTTPException(401, detail=f"invalid_token: bad_header: {exc}") from exc

    alg = header.get("alg")
    if alg != "RS256":
        # Refuse anything but RS256. `none` is an obvious attack; HS256
        # would mean the gateway shares a symmetric secret with the
        # auth service, which it intentionally does not.
        raise HTTPException(
            401,
            detail=f"invalid_token: unsupported_alg {alg!r} (RS256 only)",
        )

    kid = header.get("kid")
    cached = cache.find(kid)
    if cached is None:
        # Unknown kid: maybe a fresh rotation. Force a refresh once
        # (single-flight inside the cache) and retry the lookup.
        try:
            await cache.fetch()
        except Exception:
            # Refresh failed but the old cache is preserved by `fetch`;
            # the lookup below will fail naturally and return 401.
            pass
        cached = cache.find(kid)
        if cached is None:
            raise HTTPException(
                401,
                detail=f"invalid_token: unknown_kid {kid!r}",
            )

    # Verify signature + standard claims (`exp`, `nbf`, `iat`).
    try:
        claims = pyjwt.decode(
            token,
            cached.public_key,
            algorithms=["RS256"],
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": False,  # iat sanity is implied by signing
                "verify_aud": False,  # digitorn-auth doesn't issue aud
                "require": ["exp", "iat", "sub"],
            },
            leeway=_CLOCK_SKEW_LEEWAY_S,
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise HTTPException(401, detail="invalid_token: expired") from exc
    except pyjwt.ImmatureSignatureError as exc:
        raise HTTPException(401, detail="invalid_token: not_yet_valid") from exc
    except pyjwt.MissingRequiredClaimError as exc:
        raise HTTPException(401, detail=f"invalid_token: missing_claim: {exc}") from exc
    except pyjwt.InvalidSignatureError as exc:
        raise HTTPException(401, detail="invalid_token: bad_signature") from exc
    except pyjwt.InvalidTokenError as exc:
        raise HTTPException(401, detail=f"invalid_token: {exc}") from exc
    except Exception as exc:
        # Defensive: catch anything PyJWT might raise outside the typed
        # exception family rather than leaking a 500.
        raise HTTPException(401, detail=f"invalid_token: {type(exc).__name__}") from exc

    # Issuer check. The token must come from the configured auth
    # service; cross-tenant token replay is refused here.
    iss = claims.get("iss")
    if iss != expected_issuer:
        raise HTTPException(
            401,
            detail=(
                f"invalid_token: issuer_mismatch "
                f"(got {iss!r}, expected {expected_issuer!r})"
            ),
        )

    # Refresh tokens are NEVER accepted as bearer credentials on the
    # data plane. They are meant to be exchanged at the auth service
    # for a new access token. Accepting them here would broaden the
    # blast radius of a refresh-token leak well beyond its intent.
    token_type = claims.get("type", "access")
    if token_type != "access":
        raise HTTPException(
            401,
            detail=f"invalid_token: refresh_token_not_allowed (type={token_type!r})",
        )

    return claims


# ── FastAPI dependency ─────────────────────────────────────────────


async def require_principal(
    authorization: str | None = Header(default=None),
) -> GatewayPrincipal:
    """FastAPI dependency. Pulls the JWT from `Authorization: Bearer
    <jwt>`, verifies it offline against the cached JWKS, and returns
    a `GatewayPrincipal` the route handlers can use to gate quota,
    logging, and provider routing.

    Failure modes:
      * Missing / malformed header   -> 401 missing_or_malformed_authorization_header
      * Empty bearer                 -> 401 empty_bearer_token
      * Bad token shape              -> 401 invalid_token: bad_header
      * Unknown alg                  -> 401 invalid_token: unsupported_alg
      * Unknown kid (after refresh)  -> 401 invalid_token: unknown_kid
      * Bad signature                -> 401 invalid_token: bad_signature
      * Expired                      -> 401 invalid_token: expired
      * Issuer mismatch              -> 401 invalid_token: issuer_mismatch
      * Refresh token presented      -> 401 invalid_token: refresh_token_not_allowed
      * JWKS unreachable at boot+1   -> 503 jwks_unavailable
    """
    from digitorn_gateway.config import get_settings

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            401, detail="missing_or_malformed_authorization_header",
        )
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(401, detail="empty_bearer_token")

    settings = get_settings()
    claims = await _verify_token(
        token, expected_issuer=settings.auth_issuer,
    )

    user_id = claims.get("sub")
    if not user_id:
        # `require=["sub"]` above already enforces presence; this is a
        # defensive empty-string check.
        raise HTTPException(401, detail="invalid_token: empty_sub")

    return GatewayPrincipal(
        user_id=str(user_id),
        email=claims.get("email"),
        roles=tuple(claims.get("roles") or []),
        token_type=str(claims.get("type", "access")),
        expires_at=int(claims.get("exp", 0)),
        jti=claims.get("jti"),
        raw_claims=claims,
    )
