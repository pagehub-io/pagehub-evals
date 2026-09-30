"""Ed25519 keys for the fleet-JWT tests (asymmetric-access-tokens §3.2).

``TEST_KEY`` is generated per test run; only its public half goes into the
``PAGEHUB_AUTH_JWKS`` the suite boots with. ``DEV_*`` is pagehub-auth's
committed dev key (``python/src/pagehub_auth/dev_keys.py``), public by design:
pagehub-evals must accept it only when ``ENVIRONMENT == "development"``.
"""

from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def public_x(key: Ed25519PrivateKey) -> str:
    return b64url(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def jwk(kid: str, key: Ed25519PrivateKey) -> dict:
    return {"alg": "EdDSA", "crv": "Ed25519", "kid": kid, "kty": "OKP", "use": "sig", "x": public_x(key)}


def jwks(*keys: dict) -> str:
    return json.dumps({"keys": list(keys)}, separators=(",", ":"))


TEST_KID = "ed-test-1"
TEST_KEY = Ed25519PrivateKey.generate()
OTHER_KEY = Ed25519PrivateKey.generate()
TEST_JWKS = jwks(jwk(TEST_KID, TEST_KEY))

DEV_KID = "ed-dev-1"
_DEV_SEED = "kGOn7AQRvZuGFrk8uNfk-rWeWp6zzAVtCVgj4YGiKBc"
DEV_KEY = Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(_DEV_SEED + "="))
DEV_PUBLIC_X = "6ICMnghhr2ZPpbROB4UEHFBVyhMr5wnpysH-0Tqciow"
# Byte-identical to pagehub-auth's DEV_JWKS (what docker-compose.yml sets).
DEV_JWKS = (
    '{"keys":[{"alg":"EdDSA","crv":"Ed25519","kid":"ed-dev-1"'
    ',"kty":"OKP","use":"sig","x":"6ICMnghhr2ZPpbROB4UEHFBVyhMr5wnpysH-0Tqciow"}]}'
)
