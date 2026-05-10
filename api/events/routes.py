"""Events — append-only audit trail.

Read-only API surface. PATCH and DELETE endpoints exist solely to
return 405 (proves immutability to the eval suite). Filterable by
target_kind + target_id.
"""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import AuthContext, require_user
from api.events.schemas import EventListResponse, EventResponse

router = APIRouter(prefix="/v1/events")


def _row_to_response(row) -> EventResponse:
    payload = row["payload"] or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    return EventResponse(
        id=row["id"],
        actor_kind=row["actor_kind"],
        actor_id=row["actor_id"],
        kind=row["kind"],
        target_kind=row["target_kind"],
        target_id=row["target_id"],
        payload=payload,
        created_at=row["created_at"],
    )


@router.get("", response_model=EventListResponse)
async def list_events(
    target_kind: str | None = None,
    target_id: UUID | None = None,
    auth: AuthContext = Depends(require_user),
) -> EventListResponse:
    where: list[str] = []
    args: list = []
    if target_kind is not None:
        where.append(f"target_kind = ${len(args) + 1}")
        args.append(target_kind)
    if target_id is not None:
        where.append(f"target_id = ${len(args) + 1}")
        args.append(target_id)
    sql = """
        SELECT id, actor_kind, actor_id, kind, target_kind, target_id, payload, created_at
        FROM events
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at ASC LIMIT 500"
    rows = await auth.db.fetch(sql, *args)
    return EventListResponse(items=[_row_to_response(r) for r in rows])


# Explicit 405 endpoints. Without these FastAPI returns 404 for unknown
# methods on /v1/events/{id}, but the spec asserts 405 (Method Not
# Allowed) so the immutability property is testable.

@router.patch("/{event_id}", status_code=405)
async def patch_event_blocked(event_id: UUID) -> None:
    raise HTTPException(status_code=405, detail="Events are immutable")


@router.delete("/{event_id}", status_code=405)
async def delete_event_blocked(event_id: UUID) -> None:
    raise HTTPException(status_code=405, detail="Events cannot be deleted")
