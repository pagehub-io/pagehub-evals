"""Requests — HTTP request templates with {{VAR}} substitution.

Authoring is operator-only. URL/headers/body all support {{VAR}}
placeholders that the run engine fills in from the bound environment.
"""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import AuthContext, require_user
from api.requests.schemas import (
    CreateRequestRequest,
    RequestListResponse,
    RequestResponse,
)
from api.shared.events import record_event

router = APIRouter(prefix="/v1/requests")


def _row_to_response(row) -> RequestResponse:
    headers = row["headers"] or {}
    if isinstance(headers, str):
        headers = json.loads(headers)
    body = row["body"]
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            pass
    return RequestResponse(
        id=row["id"],
        name=row["name"],
        method=row["method"],
        url=row["url"],
        headers=headers,
        body=body,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.post("", response_model=RequestResponse, status_code=201)
async def create_request(
    body: CreateRequestRequest,
    auth: AuthContext = Depends(require_user),
) -> RequestResponse:
    row = await auth.db.fetchrow(
        """
        INSERT INTO requests (owner_user_id, name, method, url, headers, body)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
        RETURNING id, name, method, url, headers, body, created_at, updated_at
        """,
        auth.actor_id,
        body.name,
        body.method,
        body.url,
        json.dumps(body.headers),
        json.dumps(body.body) if body.body is not None else None,
    )
    await record_event(
        auth.db,
        actor_kind=auth.actor_kind,
        actor_id=auth.actor_id,
        kind="request.created",
        target_kind="request",
        target_id=row["id"],
        payload={"name": row["name"]},
    )
    return _row_to_response(row)


@router.get("", response_model=RequestListResponse)
async def list_requests(
    auth: AuthContext = Depends(require_user),
) -> RequestListResponse:
    rows = await auth.db.fetch(
        """
        SELECT id, name, method, url, headers, body, created_at, updated_at
        FROM requests
        ORDER BY created_at DESC
        LIMIT 500
        """
    )
    return RequestListResponse(items=[_row_to_response(r) for r in rows])


@router.get("/{request_id}", response_model=RequestResponse)
async def get_request(
    request_id: UUID,
    auth: AuthContext = Depends(require_user),
) -> RequestResponse:
    row = await auth.db.fetchrow(
        """
        SELECT id, name, method, url, headers, body, created_at, updated_at
        FROM requests
        WHERE id = $1
        """,
        request_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Request not found")
    return _row_to_response(row)
