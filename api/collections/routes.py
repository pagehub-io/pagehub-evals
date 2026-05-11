"""Collections — ordered groups of requests.

Authoring is operator-only. Position uniqueness is enforced by a
DB unique constraint; duplicate inserts return 409.
"""

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from api.collections.schemas import (
    AddCollectionItemRequest,
    CollectionItemResponse,
    CollectionListResponse,
    CollectionResponse,
    CreateCollectionRequest,
)
from api.dependencies import AuthContext, require_user
from api.shared.events import record_event

router = APIRouter(prefix="/v1/collections")


async def _load_items(db, collection_id: UUID) -> list[CollectionItemResponse]:
    rows = await db.fetch(
        """
        SELECT id, collection_id, request_id, position
        FROM collection_items
        WHERE collection_id = $1
        ORDER BY position ASC
        """,
        collection_id,
    )
    return [CollectionItemResponse(**dict(r)) for r in rows]


async def _row_to_response(db, row) -> CollectionResponse:
    items = await _load_items(db, row["id"])
    return CollectionResponse(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        items=items,
    )


@router.post("", response_model=CollectionResponse, status_code=201)
async def create_collection(
    body: CreateCollectionRequest,
    auth: AuthContext = Depends(require_user),
) -> CollectionResponse:
    row = await auth.db.fetchrow(
        """
        INSERT INTO collections (owner_user_id, name, description)
        VALUES ($1, $2, $3)
        RETURNING id, name, description, created_at, updated_at
        """,
        auth.actor_id,
        body.name,
        body.description,
    )
    await record_event(
        auth.db,
        actor_kind=auth.actor_kind,
        actor_id=auth.actor_id,
        kind="collection.created",
        target_kind="collection",
        target_id=row["id"],
        payload={"name": row["name"]},
    )
    return await _row_to_response(auth.db, row)


@router.get("", response_model=CollectionListResponse)
async def list_collections(
    auth: AuthContext = Depends(require_user),
) -> CollectionListResponse:
    rows = await auth.db.fetch(
        """
        SELECT id, name, description, created_at, updated_at
        FROM collections
        ORDER BY created_at DESC
        LIMIT 500
        """
    )
    return CollectionListResponse(
        items=[await _row_to_response(auth.db, r) for r in rows]
    )


@router.get("/{collection_id}", response_model=CollectionResponse)
async def get_collection(
    collection_id: UUID,
    auth: AuthContext = Depends(require_user),
) -> CollectionResponse:
    row = await auth.db.fetchrow(
        """
        SELECT id, name, description, created_at, updated_at
        FROM collections
        WHERE id = $1
        """,
        collection_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    return await _row_to_response(auth.db, row)


@router.post(
    "/{collection_id}/items",
    response_model=CollectionItemResponse,
    status_code=201,
)
async def add_item(
    collection_id: UUID,
    body: AddCollectionItemRequest,
    auth: AuthContext = Depends(require_user),
) -> CollectionItemResponse:
    # Verify both parents exist before insert (gives 404 instead of FK violation).
    cexists = await auth.db.fetchrow("SELECT 1 FROM collections WHERE id = $1", collection_id)
    if cexists is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    rexists = await auth.db.fetchrow("SELECT 1 FROM requests WHERE id = $1", body.request_id)
    if rexists is None:
        raise HTTPException(status_code=404, detail="Request not found")

    try:
        row = await auth.db.fetchrow(
            """
            INSERT INTO collection_items (collection_id, request_id, position)
            VALUES ($1, $2, $3)
            RETURNING id, collection_id, request_id, position
            """,
            collection_id,
            body.request_id,
            body.position,
        )
    except asyncpg.UniqueViolationError as e:
        raise HTTPException(
            status_code=409,
            detail=f"Position {body.position} already taken in this collection",
        ) from e

    await record_event(
        auth.db,
        actor_kind=auth.actor_kind,
        actor_id=auth.actor_id,
        kind="collection_item.added",
        target_kind="collection",
        target_id=collection_id,
        payload={"request_id": str(body.request_id), "position": body.position},
    )
    return CollectionItemResponse(**dict(row))


@router.delete("/{collection_id}/items/{item_id}", status_code=204)
async def remove_item(
    collection_id: UUID,
    item_id: UUID,
    auth: AuthContext = Depends(require_user),
) -> None:
    row = await auth.db.fetchrow(
        """
        DELETE FROM collection_items
        WHERE id = $1 AND collection_id = $2
        RETURNING id
        """,
        item_id,
        collection_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Collection item not found")
    await record_event(
        auth.db,
        actor_kind=auth.actor_kind,
        actor_id=auth.actor_id,
        kind="collection_item.removed",
        target_kind="collection",
        target_id=collection_id,
        payload={"item_id": str(item_id)},
    )
