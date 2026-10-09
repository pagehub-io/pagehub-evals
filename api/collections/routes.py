"""Collections — ordered groups of requests.

Authoring is operator-only. Position uniqueness is enforced by a
DB unique constraint; duplicate inserts return 409.
"""

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from api.collections.schemas import (
    AddCollectionItemRequest,
    CollectionItemResponse,
    CollectionListResponse,
    CollectionResponse,
    CreateCollectionRequest,
)
from api.dependencies import AuthContext, require_user
from api.fixtures.engine import FixtureImportError, build_export
from api.fixtures.schemas import FixtureBundle
from api.shared.events import record_event

router = APIRouter(prefix="/v1/collections")

# The list's filters (specs/collections-list-paging.md §2). A consumer resolves
# its own collections with owner=me + name=, at most _MAX_NAMES per call.
_MAX_NAMES = 50
_MAX_NAME_LEN = 200
_INVALID_CURSOR = "invalid cursor"


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


async def _load_items_for(
    db, collection_ids: list[UUID]
) -> dict[UUID, list[CollectionItemResponse]]:
    """Every listed collection's items in one query, keyed by collection."""
    out: dict[UUID, list[CollectionItemResponse]] = {cid: [] for cid in collection_ids}
    if not collection_ids:
        return out
    rows = await db.fetch(
        """
        SELECT id, collection_id, request_id, position
        FROM collection_items
        WHERE collection_id = ANY($1::uuid[])
        ORDER BY collection_id, position ASC
        """,
        collection_ids,
    )
    for r in rows:
        out[r["collection_id"]].append(CollectionItemResponse(**dict(r)))
    return out


def _encode_cursor(created_at: datetime, row_id: UUID) -> str:
    """Opaque to clients: unpadded base64url of {"c": <ISO 8601>, "i": <uuid>}."""
    raw = json.dumps({"c": created_at.isoformat(), "i": str(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """The (created_at, id) a page continues after. Anything malformed is a 422."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        if not isinstance(data, dict) or not isinstance(data.get("i"), str):
            raise ValueError("cursor shape")
        created_at = datetime.fromisoformat(data["c"])
        row_id = UUID(data["i"])
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp has no offset")
        # A timestamp that parses but can't be expressed in UTC would fail at
        # bind time as a 500; normalising here makes it a 422.
        created_at = created_at.astimezone(UTC)
    except (binascii.Error, UnicodeError, ValueError, KeyError, TypeError, OverflowError) as e:
        raise HTTPException(status_code=422, detail=_INVALID_CURSOR) from e
    return created_at, row_id


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
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    name: Annotated[list[str] | None, Query()] = None,
    owner: Annotated[Literal["me"] | None, Query()] = None,
) -> CollectionListResponse:
    """Newest first (created_at, then id), keyset-paged. With no parameters it
    is today's first 500 rows plus ``next_cursor``. Every input is checked
    before the DB is touched."""
    # A length bound on a repeated Query bounds the list, not each value, so
    # each name is checked here.
    if name is not None:
        if len(name) > _MAX_NAMES:
            raise HTTPException(status_code=422, detail=f"at most {_MAX_NAMES} name values")
        # Postgres text can't hold NUL, so a NUL would otherwise be a 500.
        if any(not n or len(n) > _MAX_NAME_LEN or "\x00" in n for n in name):
            raise HTTPException(
                status_code=422,
                detail=f"each name must be 1-{_MAX_NAME_LEN} characters, without NUL",
            )
    after = _decode_cursor(cursor) if cursor is not None else None

    clauses: list[str] = []
    args: list[object] = []
    if name is not None:
        args.append(name)
        clauses.append(f"name = ANY(${len(args)}::text[])")
    if owner == "me":
        args.append(auth.actor_id)
        clauses.append(f"owner_user_id = ${len(args)}")
    if after is not None:
        args.extend(after)
        clauses.append(f"(created_at, id) < (${len(args) - 1}, ${len(args)})")
    args.append(limit + 1)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await auth.db.fetch(
        f"""
        SELECT id, name, description, created_at, updated_at
        FROM collections
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT ${len(args)}
        """,
        *args,
    )
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = _encode_cursor(rows[-1]["created_at"], rows[-1]["id"])
    items = await _load_items_for(auth.db, [r["id"] for r in rows])
    return CollectionListResponse(
        items=[
            CollectionResponse(
                id=r["id"],
                name=r["name"],
                description=r["description"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                items=items[r["id"]],
            )
            for r in rows
        ],
        next_cursor=next_cursor,
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


@router.get(
    "/{collection_id}/export",
    response_model=FixtureBundle,
    # ``timeout_ms`` is optional and only set when the row carries it;
    # exclude_unset keeps it out of exports of bundles that never had it.
    # (Not exclude_none: ``body: null`` and ``description: null`` are real.)
    response_model_exclude_unset=True,
)
async def export_collection(
    collection_id: UUID,
    auth: AuthContext = Depends(require_user),
) -> FixtureBundle:
    """Project this collection (+ its requests + their evaluations) into a
    fixture bundle. ``environments`` is always ``[]`` (a collection has no
    canonical environment). Served inline as ``application/json``."""
    try:
        return await build_export(auth.db, collection_id)
    except FixtureImportError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e


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
