"""Hashing for harness key secrets.

We use SHA-256 with HMAC keyed by ``ENCRYPTION_KEY`` so that:

- Compromising the database alone (without the env key) does not let
  an attacker authenticate; they'd need to brute-force from the hash
  with the right HMAC key.
- Verification is constant-time via ``hmac.compare_digest``.

This is NOT a password-hashing context (no slow function like bcrypt);
the secrets are 32-byte URL-safe random tokens, not human-typeable
passwords, so the threat model is "leak via DB dump" rather than
"online brute-force a weak password." HMAC-SHA256 is appropriate.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from base64 import urlsafe_b64encode

from api.config import get_settings

SECRET_BYTES = 32
SECRET_PREFIX = "phk_"  # pagehub-evals harness key
PREFIX_VISIBLE_CHARS = 10  # phk_ + 6 chars of base64


def generate_secret() -> str:
    raw = secrets.token_bytes(SECRET_BYTES)
    return SECRET_PREFIX + urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def hash_secret(secret: str) -> str:
    settings = get_settings()
    key = settings.encryption_key.encode("utf-8")
    return hmac.new(key, secret.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_secret(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), stored_hash)


def visible_prefix(secret: str) -> str:
    return secret[:PREFIX_VISIBLE_CHARS]
