"""Password hashing, token hashing, and application-level secret encryption
(spec 27, ADR 0002).

* Owner passwords: Argon2id (``argon2-cffi``).
* Manual agent tokens / session tokens: SHA-256 of a high-entropy random
  value; only the hash is stored, the raw value is shown once.
* ``SecretBox``: AES-256-GCM keyed by a master key supplied outside
  PostgreSQL (``MONTAUK_MASTER_KEY`` env var or a mounted file). No secrets
  are actually stored encrypted in this increment, but connector/LLM
  credentials in a later increment go through here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_ph = PasswordHasher()

ENV_MASTER_KEY = "MONTAUK_MASTER_KEY"


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        _ph.verify(stored_hash, password)
        return True
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def new_token(prefix: str = "") -> str:
    """A fresh high-entropy token. ``prefix`` (e.g. ``"mtk_"``) is included in
    the returned string and in its recorded prefix."""
    return f"{prefix}{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str, length: int = 12) -> str:
    return token[:length]


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


class MasterKeyMissing(RuntimeError):
    pass


def _load_master_key() -> bytes:
    raw = os.environ.get(ENV_MASTER_KEY)
    if raw and os.path.isfile(raw):
        raw = open(raw, encoding="utf-8").read().strip()
    if not raw:
        raise MasterKeyMissing(
            f"{ENV_MASTER_KEY} is not set; generate one with "
            '`python -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`'
        )
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as exc:  # noqa: BLE001
        raise MasterKeyMissing(f"{ENV_MASTER_KEY} is not valid base64") from exc
    if len(key) != 32:
        raise MasterKeyMissing(f"{ENV_MASTER_KEY} must decode to 32 bytes, got {len(key)}")
    return key


class SecretBox:
    """AES-256-GCM envelope. Ciphertext is stored as ``v1:<b64 nonce>:<b64 ct>``
    so the key-derivation version is explicit for future rotation."""

    def __init__(self, key: bytes | None = None):
        self._key = key or _load_master_key()
        self._aead = AESGCM(self._key)

    def encrypt(self, plaintext: str, *, associated_data: bytes | None = None) -> str:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), associated_data)
        return "v1:" + base64.b64encode(nonce).decode() + ":" + base64.b64encode(ct).decode()

    def decrypt(self, token: str, *, associated_data: bytes | None = None) -> str:
        version, nonce_b64, ct_b64 = token.split(":", 2)
        if version != "v1":
            raise ValueError(f"unknown secret envelope version {version!r}")
        nonce = base64.b64decode(nonce_b64)
        ct = base64.b64decode(ct_b64)
        return self._aead.decrypt(nonce, ct, associated_data).decode("utf-8")
