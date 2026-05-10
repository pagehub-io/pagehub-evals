"""Shared FastAPI dependencies for authentication.

Verifies the pagehub-auth-issued access token locally with the shared
HS256 signing key. The `app_slug` claim is asserted against
`pagehub_evals_app_slug` so a token minted for a different app cannot
reach this app's data — mirrors the app-prayers pattern.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import asyncpg
import jwt
from fastapi import Depends, Header, HTTPException

from api.config import get_settings
from api.shared.db import get_db

logger = logging.getLogger(__name__)


@dataclass
class AuthContext:
    user_id: str
    pagehub_user_id: str
    token: str
    db: asyncpg.Connection
    email: str
    is_admin: bool = False
    raw_user_meta_data: dict = field(default_factory=dict)


def _strip_bearer(authorization: str) -> str:
    return authorization.replace("Bearer ", "").strip()


def _verify_jwt(token: str) -> dict:
    settings = get_settings()
    if not settings.jwt_signing_keys:
        raise HTTPException(status_code=500, detail="JWT signing keys not configured")

    last_err: Optional[Exception] = None
    for kid, secret in settings.jwt_signing_keys:
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


async def require_auth(
    authorization: Optional[str] = Header(None),
    db: asyncpg.Connection = Depends(get_db),
) -> AuthContext:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    settings = get_settings()
    token = _strip_bearer(authorization)
    claims = _verify_jwt(token)

    app_slug = claims.get("app_slug")
    if app_slug != settings.app_slug:
        raise HTTPException(status_code=403, detail="Token issued for a different app")

    pagehub_user_id = str(claims.get("sub", ""))
    if not pagehub_user_id:
        raise HTTPException(status_code=401, detail="Token missing sub claim")

    email = (claims.get("email") or "").lower()
    is_admin = bool(email and email in settings.admin_emails)

    return AuthContext(
        user_id=pagehub_user_id,
        pagehub_user_id=pagehub_user_id,
        token=token,
        db=db,
        email=email,
        is_admin=is_admin,
        raw_user_meta_data=claims,
    )


async def require_admin(auth: AuthContext = Depends(require_auth)) -> AuthContext:
    if not auth.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return auth
