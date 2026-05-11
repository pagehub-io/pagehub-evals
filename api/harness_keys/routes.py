"""Harness-key lifecycle: mint, list, revoke.

Authoring is operator-only (require_user). The minted secret is
returned exactly once at creation (HarnessKeyMintedResponse) and
never re-rendered (HarnessKeyResponse omits the secret).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import AuthContext, require_user
from api.harness_keys.schemas import (
    CreateHarnessKeyRequest,
    HarnessKeyListResponse,
    HarnessKeyMintedResponse,
    HarnessKeyResponse,
)
from api.shared.events import record_event
from api.shared.secret_hash import (
    generate_secret,
    hash_secret,
    visible_prefix,
)

router = APIRouter(prefix="/v1/harness-keys")


@router.post("", response_model=HarnessKeyMintedResponse, status_code=201)
async def create_harness_key(
    body: CreateHarnessKeyRequest,
    auth: AuthContext = Depends(require_user),
) -> HarnessKeyMintedResponse:
    secret = generate_secret()
    row = await auth.db.fetchrow(
        """
        INSERT INTO harness_keys (name, secret_hash, secret_prefix, created_by)
        VALUES ($1, $2, $3, $4)
        RETURNING id, name, secret_prefix, created_at
        """,
        body.name,
        hash_secret(secret),
        visible_prefix(secret),
        auth.actor_id,
    )
    await record_event(
        auth.db,
        actor_kind=auth.actor_kind,
        actor_id=auth.actor_id,
        kind="harness_key.created",
        target_kind="harness_key",
        target_id=row["id"],
        payload={"name": row["name"]},
    )
    return HarnessKeyMintedResponse(
        id=row["id"],
        name=row["name"],
        secret=secret,
        secret_prefix=row["secret_prefix"],
        created_at=row["created_at"],
    )


@router.get("", response_model=HarnessKeyListResponse)
async def list_harness_keys(
    auth: AuthContext = Depends(require_user),
) -> HarnessKeyListResponse:
    rows = await auth.db.fetch(
        """
        SELECT id, name, secret_prefix, created_at, revoked_at
        FROM harness_keys
        ORDER BY created_at DESC
        LIMIT 500
        """
    )
    return HarnessKeyListResponse(
        items=[HarnessKeyResponse(**dict(r)) for r in rows]
    )


@router.get("/{key_id}", response_model=HarnessKeyResponse)
async def get_harness_key(
    key_id: UUID,
    auth: AuthContext = Depends(require_user),
) -> HarnessKeyResponse:
    row = await auth.db.fetchrow(
        """
        SELECT id, name, secret_prefix, created_at, revoked_at
        FROM harness_keys
        WHERE id = $1
        """,
        key_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Harness key not found")
    return HarnessKeyResponse(**dict(row))


@router.delete("/{key_id}", status_code=204)
async def revoke_harness_key(
    key_id: UUID,
    auth: AuthContext = Depends(require_user),
) -> None:
    row = await auth.db.fetchrow(
        """
        UPDATE harness_keys
        SET revoked_at = now()
        WHERE id = $1 AND revoked_at IS NULL
        RETURNING id
        """,
        key_id,
    )
    if row is None:
        # Already revoked, or not found — both 404 to keep surface tight.
        raise HTTPException(status_code=404, detail="Harness key not found or already revoked")
    await record_event(
        auth.db,
        actor_kind=auth.actor_kind,
        actor_id=auth.actor_id,
        kind="harness_key.revoked",
        target_kind="harness_key",
        target_id=key_id,
        payload={},
    )
