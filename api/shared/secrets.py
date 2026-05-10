"""Fernet-encrypted environment secrets.

Schema stores `{key: ciphertext}` in environments.secrets. Decryption
is gated by `?reveal_secrets=true` in the API layer.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from api.config import get_settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    settings = get_settings()
    if not settings.encryption_key:
        raise RuntimeError("ENCRYPTION_KEY not configured")
    return Fernet(settings.encryption_key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise ValueError("Invalid Fernet ciphertext") from e


def mask(_ciphertext: str) -> str:
    """Default rendering for secrets in API responses."""
    return "********"
