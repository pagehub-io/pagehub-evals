"""Runs — verdict-bearing executions.

Stub routes (501). Ground-truth-gate semantics land in the JTBD pivot.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from api.config import get_settings
from api.dependencies import AuthContext, require_auth
from api.runs.schemas import CreateRunRequest, RunListResponse, RunResponse

router = APIRouter(prefix="/v1/runs")


def _gate_runs() -> None:
    if not get_settings().runs_enabled:
        raise HTTPException(status_code=503, detail="Runs feature flag disabled")


@router.post("", response_model=RunResponse, status_code=201)
async def create_run(
    body: CreateRunRequest,
    auth: AuthContext = Depends(require_auth),
) -> RunResponse:
    _gate_runs()
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")


@router.get("", response_model=RunListResponse)
async def list_runs(
    auth: AuthContext = Depends(require_auth),
) -> RunListResponse:
    _gate_runs()
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: UUID,
    auth: AuthContext = Depends(require_auth),
) -> RunResponse:
    _gate_runs()
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")
