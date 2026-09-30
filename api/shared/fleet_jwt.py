"""pagehub-auth's public key set, and the legacy-accept signal.

Step B of pagehub-auth's asymmetric access tokens
(pagehub-auth ``specs/asymmetric-access-tokens.md`` §3.2): tokens signed with
pagehub-auth's Ed25519 keys are verified against ``PAGEHUB_AUTH_JWKS``; during
the overlap, tokens under the legacy fleet HS256 ``kid`` are still accepted,
and every such accept is logged and counted so step D can prove there are none
left.

The structural rules mirror pagehub-infra's ``ci/validate_jwks.py`` (§3.3), the
check deploy-app runs before it delivers the set.
"""

from __future__ import annotations

import base64
import json
import logging
import re

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from prometheus_client import Counter

logger = logging.getLogger(__name__)

# pagehub-auth's committed dev key (kid ed-dev-1). Its private half is public
# by design, so outside development it's refused under ANY kid.
DEV_KID_PREFIX = "ed-dev-"
DEV_PUBLIC_X = "6ICMnghhr2ZPpbROB4UEHFBVyhMr5wnpysH-0Tqciow"

# 32 bytes, unpadded base64url (RFC 8037): exactly 43 characters. A regex,
# because b64decode silently skips characters outside the alphabet.
_ED25519_X = re.compile(r"[A-Za-z0-9_-]{43}")

LEGACY_ACCEPT_EVENT = "fleet_jwt_legacy_accept"

legacy_accepts_total = Counter(
    "fleet_jwt_legacy_accepts_total",
    "Tokens accepted on the legacy fleet HS256 path (asymmetric-access-tokens §3.2).",
)


def parse_jwks(raw: str, *, allow_dev: bool) -> dict[str, Ed25519PublicKey]:
    """``PAGEHUB_AUTH_JWKS`` → ``{kid: public key}``, or ``ValueError`` listing
    every problem. Dev material (an ``ed-dev-*`` kid, or the dev public key
    under any kid) is allowed only when ``allow_dev``."""
    try:
        doc = json.loads(raw)
    except ValueError as e:
        raise ValueError([f"unparseable JSON: {e}"]) from None
    if not isinstance(doc, dict) or not isinstance(doc.get("keys"), list):
        raise ValueError(["not a JWKS object with a 'keys' list"])
    if not doc["keys"]:
        raise ValueError(["the key set is empty"])
    problems: list[str] = []
    keys: dict[str, Ed25519PublicKey] = {}
    for i, key in enumerate(doc["keys"]):
        if not isinstance(key, dict):
            problems.append(f"key {i} is not an object")
            continue
        kid = key.get("kid")
        where = f"key {i} ({kid!r})"
        if not isinstance(kid, str) or not kid:
            problems.append(f"{where} has no kid")
        elif kid.startswith(DEV_KID_PREFIX) and not allow_dev:
            problems.append(f"{where} is a dev kid")
        elif kid in keys:
            problems.append(f"{where} repeats a kid")
        for name, want in (("kty", "OKP"), ("crv", "Ed25519"), ("alg", "EdDSA")):
            if key.get(name) != want:
                problems.append(f"{where} has {name}={key.get(name)!r}, want {want!r}")
        if "d" in key:
            problems.append(f"{where} carries private material ('d')")
        x = key.get("x")
        if not isinstance(x, str) or not x:
            problems.append(f"{where} has no public key ('x')")
        elif x == DEV_PUBLIC_X and not allow_dev:
            problems.append(f"{where} is the committed dev key")
        elif not _ED25519_X.fullmatch(x):
            problems.append(f"{where} has an 'x' that isn't a 32-byte base64url key")
        elif isinstance(kid, str) and kid and kid not in keys:
            keys[kid] = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(x + "="))
    if problems:
        raise ValueError(problems)
    return keys


def record_legacy_accept(*, app: str, stage: str, kid: str, claims: dict) -> None:
    """Count one legacy accept and log it for New Relic (step D's gate reads the
    log). WARNING, the root default, so the agent forwards it with no per-app
    logging config. Never the token, never an email."""
    legacy_accepts_total.inc()
    payload = {
        "app": app,
        "stage": stage,
        "kid": kid,
        "iss": claims.get("iss"),
        "app_slug": claims.get("app_slug"),
    }
    logger.warning("%s %s", LEGACY_ACCEPT_EVENT, json.dumps(payload, sort_keys=True))
