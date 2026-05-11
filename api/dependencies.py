"""Authentication + authorization dependencies.

Two auth paths land at the same ``AuthContext``:

- **User JWT**: ``Authorization: Bearer <jwt>`` issued by pagehub-auth.
  Verified locally with the shared HS256 signing keys. Slug must
  match ``settings.app_slug``. Sets ``actor_kind="user"``.
- **Harness key**: ``X-Harness-Key: <secret>``. Looked up against the
  ``harness_keys`` table; revoked or unknown keys → 401. Sets
  ``actor_kind="harness_key"``.

Sending **both** headers on the same request → 422 (ambiguous
attribution; per spec ``harness-key-auth``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

import asyncpg
import jwt
from fastapi import Depends, Header, HTTPException

from api.config import get_settings
from api.shared.db import get_db
from api.shared.secret_hash import hash_secret

logger = logging.getLogger(__name__)

ActorKind = Literal["user", "harness_key"]


@dataclass
class AuthContext:
    actor_kind: ActorKind
    actor_id: str
    db: asyncpg.Connection
    # User-only fields (empty when actor_kind == "harness_key").
    email: str = ""
    is_admin: bool = False
    raw_jwt_claims: dict = field(default_factory=dict)


def _strip_bearer(authorization: str) -> str:
    return authorization.replace("Bearer ", "").strip()


def _verify_jwt(token: str) -> dict:
    settings = get_settings()
    last_err: Exception | None = None
    for _kid, secret in settings.jwt_signing_keys:
        try:
            return jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                issuer=settings.pagehub_auth_issuer,
            )
        except jwt.InvalidTokenError as e:
            last_err = e
            continue
    logger.warning("JWT verification failed: %s", last_err)
    raise HTTPException(status_code=401, detail="Invalid token")


async def _resolve_user(authorization: str, db: asyncpg.Connection) -> AuthContext:
    settings = get_settings()
    token = _strip_bearer(authorization)
    claims = _verify_jwt(token)

    if claims.get("app_slug") != settings.app_slug:
        raise HTTPException(status_code=403, detail="Token issued for a different app")

    user_id = str(claims.get("sub", ""))
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub claim")

    email = (claims.get("email") or "").lower()
    is_admin = bool(email and email in settings.admin_emails)

    return AuthContext(
        actor_kind="user",
        actor_id=user_id,
        db=db,
        email=email,
        is_admin=is_admin,
        raw_jwt_claims=claims,
    )


async def _resolve_harness_key(secret: str, db: asyncpg.Connection) -> AuthContext:
    if not secret.strip():
        raise HTTPException(status_code=401, detail="Empty harness key")
    hashed = hash_secret(secret.strip())
    row = await db.fetchrow(
        """
        SELECT id::text AS id, revoked_at
        FROM harness_keys
        WHERE secret_hash = $1
        """,
        hashed,
    )
    if row is None:
        raise HTTPException(status_code=401, detail="Unknown harness key")
    if row["revoked_at"] is not None:
        raise HTTPException(status_code=401, detail="Revoked harness key")
    return AuthContext(actor_kind="harness_key", actor_id=row["id"], db=db)


async def require_auth(
    authorization: str | None = Header(None),
    x_harness_key: str | None = Header(None),
    db: asyncpg.Connection = Depends(get_db),
) -> AuthContext:
    if authorization and x_harness_key:
        raise HTTPException(
            status_code=422,
            detail="Both Authorization and X-Harness-Key were sent — pick one",
        )
    if authorization:
        return await _resolve_user(authorization, db)
    if x_harness_key:
        return await _resolve_harness_key(x_harness_key, db)
    raise HTTPException(
        status_code=401,
        detail="Missing Authorization or X-Harness-Key header",
    )


async def require_user(auth: AuthContext = Depends(require_auth)) -> AuthContext:
    """Endpoints that only operators (humans) can call — never harness keys.

    Used for: minting/revoking harness keys, environment authoring,
    request/collection authoring, secret reveal, run-listing through
    the operator UX.
    """
    if auth.actor_kind != "user":
        raise HTTPException(status_code=403, detail="Operator-only endpoint")
    return auth


async def require_admin(auth: AuthContext = Depends(require_user)) -> AuthContext:
    if not auth.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return auth
