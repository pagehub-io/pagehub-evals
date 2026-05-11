"""Evaluations — assertions attached to requests.

Slice 1 kinds: status_eq, json_path_eq, header_present, body_contains.
Per-kind config is strictly validated at create-time (unknown kind or
malformed config → 422).
"""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import AuthContext, require_user
from api.evaluations.schemas import (
    CreateEvaluationRequest,
    EvaluationKind,
    EvaluationListResponse,
    EvaluationResponse,
)
from api.shared.events import record_event

router = APIRouter(prefix="/v1/requests/{request_id}/evaluations")


def _row_to_response(row) -> EvaluationResponse:
    config = row["config"] or {}
    if isinstance(config, str):
        config = json.loads(config)
    return EvaluationResponse(
        id=row["id"],
        request_id=row["request_id"],
        name=row["name"],
        kind=EvaluationKind(row["kind"]),
        config=config,
        created_at=row["created_at"],
    )


async def _request_exists(db, request_id: UUID) -> bool:
    row = await db.fetchrow("SELECT 1 FROM requests WHERE id = $1", request_id)
    return row is not None


@router.post("", response_model=EvaluationResponse, status_code=201)
async def create_evaluation(
    request_id: UUID,
    body: CreateEvaluationRequest,
    auth: AuthContext = Depends(require_user),
) -> EvaluationResponse:
    if not await _request_exists(auth.db, request_id):
        raise HTTPException(status_code=404, detail="Request not found")
    row = await auth.db.fetchrow(
        """
        INSERT INTO evaluations (request_id, name, kind, config)
        VALUES ($1, $2, $3, $4::jsonb)
        RETURNING id, request_id, name, kind, config, created_at
        """,
        request_id,
        body.name,
        body.kind.value,
        json.dumps(body.config),
    )
    await record_event(
        auth.db,
        actor_kind=auth.actor_kind,
        actor_id=auth.actor_id,
        kind="evaluation.created",
        target_kind="evaluation",
        target_id=row["id"],
        payload={"name": row["name"], "kind": row["kind"], "request_id": str(request_id)},
    )
    return _row_to_response(row)


@router.get("", response_model=EvaluationListResponse)
async def list_evaluations(
    request_id: UUID,
    auth: AuthContext = Depends(require_user),
) -> EvaluationListResponse:
    if not await _request_exists(auth.db, request_id):
        raise HTTPException(status_code=404, detail="Request not found")
    rows = await auth.db.fetch(
        """
        SELECT id, request_id, name, kind, config, created_at
        FROM evaluations
        WHERE request_id = $1
        ORDER BY created_at ASC
        LIMIT 500
        """,
        request_id,
    )
    return EvaluationListResponse(items=[_row_to_response(r) for r in rows])
