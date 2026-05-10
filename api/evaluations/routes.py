"""Evaluations — response assertions per request.

Stub routes (501).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import AuthContext, require_auth
from api.evaluations.schemas import (
    CreateEvaluationRequest,
    EvaluationListResponse,
    EvaluationResponse,
)

router = APIRouter(prefix="/v1/requests/{request_id}/evaluations")


@router.post("", response_model=EvaluationResponse, status_code=201)
async def create_evaluation(
    request_id: UUID,
    body: CreateEvaluationRequest,
    auth: AuthContext = Depends(require_auth),
) -> EvaluationResponse:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")


@router.get("", response_model=EvaluationListResponse)
async def list_evaluations(
    request_id: UUID,
    auth: AuthContext = Depends(require_auth),
) -> EvaluationListResponse:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")
