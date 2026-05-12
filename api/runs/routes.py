"""Runs — verdict-bearing executions.

POST is allowed for any authenticated actor (operator JWT OR
harness key). Background execution kicks off immediately and the
response returns 202 + run_id.

PATCH is rejected with 403 — verdict and harness_claim are
write-once / engine-only.
"""

import json
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from api.config import get_settings
from api.dependencies import AuthContext, require_auth, require_user
from api.runs.engine import execute_run
from api.runs.schemas import (
    CreateRunRequest,
    RunEvidence,
    RunListResponse,
    RunResponse,
    RunStatus,
    RunVerdict,
)
from api.shared.events import record_event

router = APIRouter(prefix="/v1/runs")

_COLLECTION_ITEM_CAP = 50


def _gate() -> None:
    if not get_settings().runs_enabled:
        raise HTTPException(status_code=503, detail="Runs feature flag disabled")


def _row_to_response(row) -> RunResponse:
    # Decode-to-dict-first, then fallback, then schema-validate. No
    # try/except — engine-written rows that fail validation are a real
    # bug and must surface as 500, not silently empty the evidence.
    raw_evidence = row["evidence"]
    if isinstance(raw_evidence, str):
        raw_evidence = json.loads(raw_evidence)
    if not raw_evidence:
        raw_evidence = {"requests": []}
    evidence = RunEvidence.model_validate(raw_evidence)
    return RunResponse(
        id=row["id"],
        created_by_kind=row["created_by_kind"],
        created_by_id=row["created_by_id"],
        collection_id=row["collection_id"],
        environment_id=row["environment_id"],
        harness_id=row["harness_id"],
        harness_claim=row["harness_claim"],
        status=RunStatus(row["status"]),
        verdict=RunVerdict(row["verdict"]) if row["verdict"] else None,
        evidence=evidence,
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        created_at=row["created_at"],
    )


@router.post("", response_model=RunResponse, status_code=202)
async def create_run(
    body: CreateRunRequest,
    background: BackgroundTasks,
    auth: AuthContext = Depends(require_auth),
) -> RunResponse:
    _gate()
    if body.collection_id is not None:
        item_count = await auth.db.fetchval(
            "SELECT count(*) FROM collection_items WHERE collection_id = $1",
            body.collection_id,
        )
        if item_count is not None and item_count > _COLLECTION_ITEM_CAP:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"collection exceeds max items={_COLLECTION_ITEM_CAP} "
                    f"(got {item_count})"
                ),
            )
    row = await auth.db.fetchrow(
        """
        INSERT INTO runs (
            created_by_kind, created_by_id,
            collection_id, environment_id,
            harness_id, harness_claim,
            status
        )
        VALUES ($1, $2, $3, $4, $5, $6, 'pending')
        RETURNING id, created_by_kind, created_by_id, collection_id, environment_id,
                  harness_id, harness_claim, status, verdict, evidence,
                  started_at, finished_at, created_at
        """,
        auth.actor_kind,
        auth.actor_id,
        body.collection_id,
        body.environment_id,
        body.harness_id,
        body.harness_claim,
    )
    await record_event(
        auth.db,
        actor_kind=auth.actor_kind,
        actor_id=auth.actor_id,
        kind="run.created",
        target_kind="run",
        target_id=row["id"],
        payload={
            "harness_id": body.harness_id,
            "collection_id": str(body.collection_id) if body.collection_id else None,
        },
    )
    background.add_task(execute_run, row["id"])
    return _row_to_response(row)


@router.get("", response_model=RunListResponse)
async def list_runs(
    auth: AuthContext = Depends(require_user),
) -> RunListResponse:
    _gate()
    rows = await auth.db.fetch(
        """
        SELECT id, created_by_kind, created_by_id, collection_id, environment_id,
               harness_id, harness_claim, status, verdict, evidence,
               started_at, finished_at, created_at
        FROM runs
        ORDER BY created_at DESC
        LIMIT 500
        """
    )
    return RunListResponse(items=[_row_to_response(r) for r in rows])


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: UUID,
    auth: AuthContext = Depends(require_auth),
) -> RunResponse:
    _gate()
    row = await auth.db.fetchrow(
        """
        SELECT id, created_by_kind, created_by_id, collection_id, environment_id,
               harness_id, harness_claim, status, verdict, evidence,
               started_at, finished_at, created_at
        FROM runs
        WHERE id = $1
        """,
        run_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    # A harness key can read its own runs; operators can read any run.
    if auth.actor_kind == "harness_key" and row["created_by_id"] != auth.actor_id:
        raise HTTPException(
            status_code=403,
            detail="Harness keys can only read their own runs",
        )
    return _row_to_response(row)


# Verdict immutability: PATCH on a run rejects every field for every
# caller. The eval suite exercises this from operator JWT and from
# harness keys; both must hit 403.
@router.patch("/{run_id}", status_code=403)
async def patch_run_blocked(
    run_id: UUID,
    auth: AuthContext = Depends(require_auth),
) -> None:
    _gate()
    raise HTTPException(
        status_code=403,
        detail="Run fields (verdict, harness_claim, status) are engine-only and cannot be patched",
    )
