"""Local device authentication for self-hosted daemons."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Default location. Override via `LocalDeviceAuth.load(secrets_path=...)`.
DEFAULT_SECRETS_PATH = Path.home() / ".digitorn" / "daemon-secrets.enc"

# Service / username pair used to look up the encryption key in the
# OS keychain. Kept as constants so tests can monkeypatch them.
_KEYRING_SERVICE = "digitorn-daemon"
_KEYRING_USER = "device-encryption-key"

class LocalDeviceAuthError(Exception):
    """Base class for local device auth errors."""

class NotPaired(LocalDeviceAuthError):
    """No paired device on this host. Run `digitorn install-local` first."""

class SecretsCorrupted(LocalDeviceAuthError):
    """The secrets file exists but cannot be decrypted or parsed."""

class TokenExpired(LocalDeviceAuthError):
    """The device_token's `exp` has passed."""

class InvalidDeviceToken(LocalDeviceAuthError):
    """Token signature mismatch, wrong audience, or wrong scope."""

@dataclass
class LocalDeviceAuth:
    """Encapsulates the daemon's offline identity for a paired user."""

    device_id: str
    user_id: str
    user_email: str
    user_display_name: str
    central_iss: str
    auth_url: str
    device_token: str
    expires_at: int  # epoch seconds
    last_token_jti: str | None
    secrets_path: Path

    @classmethod
    def load(cls, secrets_path: Path | None = None) -> "LocalDeviceAuth":
        """Decrypt the on-disk secrets and validate the device token."""
        path = secrets_path or DEFAULT_SECRETS_PATH
        if not path.exists():
            raise NotPaired(
                f"No daemon-secrets at {path}. Run `digitorn install-local`."
            )
        fernet = _get_or_create_machine_key()
        try:
            blob = fernet.decrypt(path.read_bytes())
            data = json.loads(blob)
        except Exception as exc:  # noqa: BLE001
            raise SecretsCorrupted(f"Cannot decrypt secrets: {exc}") from exc

        token = data.get("device_token", "")
        if not token:
            raise SecretsCorrupted("Secrets file has no device_token")

        # RS256 path uses the cached JWKS (offline); HS256 falls back
        # to the shared `~/.digitorn/jwt.key`.
        claims = _verify_device_token(
            token,
            audience=f"daemon-{data.get('device_id', '')}",
            jwks=data.get("central_jwks") or {"keys": []},
        )

        return cls(
            device_id=claims["device_id"],
            user_id=claims["sub"],
            user_email=claims.get("email", ""),
            user_display_name=claims.get("name", "") or claims.get("email", ""),
            central_iss=data.get("central_iss", ""),
            auth_url=data.get("auth_url", ""),
            device_token=token,
            expires_at=int(claims["exp"]),
            last_token_jti=claims.get("jti"),
            secrets_path=path,
        )

    @classmethod
    def write(
        cls,
        secrets_path: Path,
        device_id: str,
        device_token: str,
        central_iss: str,
        auth_url: str,
        central_jwks: dict | None = None,
    ) -> None:
        """Persist freshly-paired secrets. Called by `digitorn install-local`."""
        path = secrets_path or DEFAULT_SECRETS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        fernet = _get_or_create_machine_key()
        payload = json.dumps({
            "device_id": device_id,
            "device_token": device_token,
            "central_iss": central_iss,
            "auth_url": auth_url,
            "central_jwks": central_jwks or {"keys": []},
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }).encode("utf-8")
        path.write_bytes(fernet.encrypt(payload))
        try:
            path.chmod(0o600)
        except Exception:
            # Windows ignores chmod silently; OS-level ACL is what
            # matters there. The file lives under the user's profile.
            pass

    def update_token(self, new_token: str, new_expires_at: int) -> None:
        """Replace the stored device token (rolling refresh from central)."""
        path = self.secrets_path
        fernet = _get_or_create_machine_key()
        # Re-decrypt so we keep the rest of the payload (auth_url,
        # central_iss, stored_at history) intact.
        try:
            existing = json.loads(fernet.decrypt(path.read_bytes()))
        except Exception:
            existing = {}
        existing.update({
            "device_token": new_token,
            "stored_at": datetime.now(timezone.utc).isoformat(),
        })
        path.write_bytes(fernet.encrypt(json.dumps(existing).encode("utf-8")))
        self.device_token = new_token
        self.expires_at = new_expires_at

    def wipe(self) -> None:
        """Erase the secrets file. Called when central revokes the device."""
        try:
            self.secrets_path.unlink(missing_ok=True)
        finally:
            self.device_token = ""
            self.expires_at = 0

    @property
    def days_until_expiry(self) -> int:
        if self.expires_at <= 0:
            return 0
        now = datetime.now(timezone.utc).timestamp()
        return max(0, int((self.expires_at - now) // 86400))

    @property
    def is_expired(self) -> bool:
        return self.expires_at > 0 and datetime.now(timezone.utc).timestamp() >= self.expires_at

def _get_or_create_machine_key():
    from cryptography.fernet import Fernet

    try:
        import keyring  # type: ignore[import-not-found]
        existing = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
        if existing:
            return Fernet(existing.encode())
        new = Fernet.generate_key()
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, new.decode())
        return Fernet(new)
    except Exception as exc:  # keyring not installed, dbus down, etc.
        logger.debug("keychain unavailable, using fallback file: %s", exc)
        keyfile = Path.home() / ".digitorn" / ".machine-key"
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        if keyfile.exists():
            return Fernet(keyfile.read_bytes())
        new = Fernet.generate_key()
        keyfile.write_bytes(new)
        try:
            keyfile.chmod(0o600)
        except Exception as exc:
            logger.debug("local_device best-effort block failed: %s", exc)
        return Fernet(new)

def _verify_device_token(token: str, *, audience: str, jwks: dict) -> dict:
    import jwt as pyjwt

    # Peek at the algorithm in the unverified header.
    try:
        header = pyjwt.get_unverified_header(token)
    except Exception as exc:
        raise InvalidDeviceToken(f"Cannot read token header: {exc}") from exc
    alg = header.get("alg")

    if alg == "RS256":
        kid = header.get("kid")
        key = _find_jwks_key(jwks, kid)
        if key is None:
            raise InvalidDeviceToken(
                f"No JWKS key matches kid={kid!r}. The daemon's cached "
                f"JWKS is stale - run `digitorn install-local` to re-pair, "
                f"or wait for the next online revalidate (which refreshes "
                f"the JWKS automatically)."
            )
        verify_key = _jwk_to_public_key(key)
        algorithms = ["RS256"]
    elif alg == "HS256":
        secret_path = Path.home() / ".digitorn" / "jwt.key"
        if not secret_path.exists():
            raise InvalidDeviceToken(
                f"No HS256 secret at {secret_path}. Either re-pair against "
                f"an RS256 auth service (recommended), or ensure the "
                f"central's jwt.key is present locally for legacy HS256 mode."
            )
        verify_key = secret_path.read_text().strip()
        algorithms = ["HS256"]
    else:
        raise InvalidDeviceToken(f"Unsupported alg in token header: {alg!r}")

    try:
        claims = pyjwt.decode(
            token,
            verify_key,
            algorithms=algorithms,
            audience=audience,
            options={"verify_aud": True},  # bind-to-this-daemon check
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise TokenExpired(
            "Device token expired. Run `digitorn install-local` to re-pair, "
            "or wait for the next online revalidate cycle."
        ) from exc
    except pyjwt.InvalidAudienceError as exc:
        raise InvalidDeviceToken(
            f"Token audience does not match this daemon "
            f"(expected '{audience}'). The token was issued for a different device."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise InvalidDeviceToken(f"Token validation failed: {exc}") from exc

    if claims.get("scope") != "daemon-pair":
        raise InvalidDeviceToken(
            f"Token scope is '{claims.get('scope')}', expected 'daemon-pair'."
        )
    return claims

def _find_jwks_key(jwks: dict, kid: str | None) -> dict | None:
    keys = (jwks or {}).get("keys", [])
    if not keys:
        return None
    if kid:
        for k in keys:
            if k.get("kid") == kid:
                return k
        return None
    return keys[0]

def _jwk_to_public_key(jwk: dict):
    import base64
    from cryptography.hazmat.primitives.asymmetric.rsa import (
        RSAPublicNumbers, rsa_recover_prime_factors,  # noqa: F401
    )
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

    def _b64dec_int(s: str) -> int:
        # JWK is base64url, no padding. Pad it back before decoding.
        padded = s + "=" * (-len(s) % 4)
        return int.from_bytes(base64.urlsafe_b64decode(padded), "big")

    n = _b64dec_int(jwk["n"])
    e = _b64dec_int(jwk["e"])
    return _rsa.RSAPublicNumbers(e=e, n=n).public_key()
