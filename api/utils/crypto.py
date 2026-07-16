"""Symmetric encryption for channel credentials stored at rest.

Messaging channel credentials (Meta system-user tokens, app secrets) are
long-lived and highly sensitive, so unlike the legacy telephony credential
rows they are encrypted before being written to the database.

The Fernet key is taken from MESSAGING_ENCRYPTION_KEY when set (any string —
it is hashed to 32 bytes), otherwise derived from OSS_JWT_SECRET so OSS
deployments work with zero extra configuration. Rotating the key requires
re-saving stored configurations.
"""

from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

_ENCRYPTED_PREFIX = "enc:v1:"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    secret = os.environ.get("MESSAGING_ENCRYPTION_KEY") or os.environ.get(
        "OSS_JWT_SECRET"
    )
    if not secret:
        raise RuntimeError(
            "No encryption key available: set MESSAGING_ENCRYPTION_KEY "
            "(or OSS_JWT_SECRET) to store messaging credentials."
        )
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_value(plaintext: str) -> str:
    """Encrypt a credential value for storage. Idempotent on already-encrypted input."""
    if plaintext.startswith(_ENCRYPTED_PREFIX):
        return plaintext
    token = _fernet().encrypt(plaintext.encode()).decode()
    return f"{_ENCRYPTED_PREFIX}{token}"


def decrypt_value(stored: str) -> str:
    """Decrypt a stored credential value. Plaintext legacy values pass through."""
    if not stored.startswith(_ENCRYPTED_PREFIX):
        return stored
    token = stored[len(_ENCRYPTED_PREFIX) :]
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise ValueError(
            "Failed to decrypt credential: wrong or rotated encryption key."
        ) from e


def encrypt_credentials(credentials: dict) -> dict:
    """Encrypt every string value of a credentials dict."""
    return {
        k: encrypt_value(v) if isinstance(v, str) and v else v
        for k, v in credentials.items()
    }


def decrypt_credentials(credentials: dict) -> dict:
    """Decrypt every string value of a credentials dict."""
    return {
        k: decrypt_value(v) if isinstance(v, str) and v else v
        for k, v in credentials.items()
    }
