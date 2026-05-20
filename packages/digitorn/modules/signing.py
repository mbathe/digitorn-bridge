"""Module cryptographic signing - Ed25519 key pairs, signing, verification."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from digitorn.modules.log import get_logger
from digitorn.modules.manifest import ModuleSignature

log = get_logger(__name__)

class SigningError(Exception):
    """Raised when signing or verification fails."""

@dataclass
class KeyPair:
    """Ed25519 key pair for module signing."""

    private_key_bytes: bytes
    public_key_bytes: bytes
    fingerprint: str

class ModuleSigner:
    """Signs module packages using Ed25519."""

    @classmethod
    def generate_key_pair(cls) -> KeyPair:
        """Generate a new Ed25519 key pair."""
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
        except ImportError as e:
            raise SigningError(
                "cryptography library required for signing. "
                "Install with: pip install 'digitorn[hub]'"
            ) from e

        private_key = Ed25519PrivateKey.generate()
        private_bytes = private_key.private_bytes_raw()
        public_bytes = private_key.public_key().public_bytes_raw()
        fingerprint = hashlib.sha256(public_bytes).hexdigest()

        return KeyPair(
            private_key_bytes=private_bytes,
            public_key_bytes=public_bytes,
            fingerprint=fingerprint,
        )

    @classmethod
    def save_key_pair(cls, key_pair: KeyPair, path: Path) -> None:
        """Save key pair to files (path.key and path.pub)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_suffix(".key").write_bytes(key_pair.private_key_bytes)
        path.with_suffix(".pub").write_bytes(key_pair.public_key_bytes)

    @classmethod
    def load_private_key(cls, path: Path) -> bytes:
        """Load a private key from a .key file."""
        return path.read_bytes()

    def __init__(self, private_key_bytes: bytes) -> None:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
        except ImportError as e:
            raise SigningError(
                "cryptography library required for signing."
            ) from e

        self._private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
        self._public_bytes = self._private_key.public_key().public_bytes_raw()
        self._fingerprint = hashlib.sha256(self._public_bytes).hexdigest()

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def public_key_bytes(self) -> bytes:
        return self._public_bytes

    def sign_content(self, content_hash: str) -> ModuleSignature:
        """Sign a content hash and return a ModuleSignature."""
        signature = self._private_key.sign(content_hash.encode())
        return ModuleSignature(
            public_key_fingerprint=self._fingerprint,
            signature_hex=signature.hex(),
            signed_hash=content_hash,
            signed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    def sign_module(self, module_dir: Path) -> ModuleSignature:
        """Compute hash and sign a module directory."""
        content_hash = self.compute_module_hash(module_dir)
        return self.sign_content(content_hash)

    @staticmethod
    def compute_module_hash(module_dir: Path) -> str:
        """Compute deterministic SHA-256 of module directory content."""
        hasher = hashlib.sha256()
        for fpath in sorted(module_dir.rglob("*.py")):
            hasher.update(fpath.relative_to(module_dir).as_posix().encode())
            hasher.update(fpath.read_bytes())
        for config_name in ("pyproject.toml", "digitorn-module.toml"):
            config = module_dir / config_name
            if config.exists():
                hasher.update(config_name.encode())
                hasher.update(config.read_bytes())
        return hasher.hexdigest()

class SignatureVerifier:
    """Verifies module signatures against a trust store."""

    def __init__(self, trusted_keys: dict[str, bytes] | None = None) -> None:
        self._trusted: dict[str, bytes] = trusted_keys or {}

    def add_trusted_key(self, fingerprint: str, public_key_bytes: bytes) -> None:
        """Add a public key to the trust store."""
        self._trusted[fingerprint] = public_key_bytes

    def remove_trusted_key(self, fingerprint: str) -> None:
        """Remove a public key from the trust store."""
        self._trusted.pop(fingerprint, None)

    def load_trust_store(self, path: Path) -> int:
        """Load trusted public keys from a directory of .pub files."""
        path = path.expanduser()
        if not path.exists():
            return 0
        loaded = 0
        for pub_file in sorted(path.glob("*.pub")):
            public_bytes = pub_file.read_bytes()
            fingerprint = hashlib.sha256(public_bytes).hexdigest()
            self._trusted[fingerprint] = public_bytes
            loaded += 1
        return loaded

    def verify(self, signature: ModuleSignature, content_hash: str) -> bool:
        """Verify a module signature against the trust store."""
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
            from cryptography.exceptions import InvalidSignature
        except ImportError:
            log.warning("cryptography_not_installed", action="verify")
            return False

        if signature.public_key_fingerprint not in self._trusted:
            log.warning(
                "untrusted_key",
                fingerprint=signature.public_key_fingerprint,
            )
            return False

        if signature.signed_hash != content_hash:
            log.warning(
                "hash_mismatch",
                expected=content_hash,
                got=signature.signed_hash,
            )
            return False

        public_bytes = self._trusted[signature.public_key_fingerprint]
        public_key = Ed25519PublicKey.from_public_bytes(public_bytes)

        try:
            public_key.verify(
                bytes.fromhex(signature.signature_hex),
                content_hash.encode(),
            )
            return True
        except InvalidSignature:
            log.warning("invalid_signature", fingerprint=signature.public_key_fingerprint)
            return False

    @property
    def trusted_count(self) -> int:
        """Number of trusted keys in the store."""
        return len(self._trusted)

    def list_trusted_fingerprints(self) -> list[str]:
        """Return all trusted key fingerprints."""
        return list(self._trusted.keys())
