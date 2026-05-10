"""Requests — HTTP request templates with {{VAR}} substitution against a bound env.

Stub routes (501).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import AuthContext, require_auth
from api.requests.schemas import (
    CreateRequestRequest,
    RequestListResponse,
    RequestResponse,
)

router = APIRouter(prefix="/v1/requests")


@router.post("", response_model=RequestResponse, status_code=201)
async def create_request(
    body: CreateRequestRequest,
    auth: AuthContext = Depends(require_auth),
) -> RequestResponse:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")


@router.get("", response_model=RequestListResponse)
async def list_requests(
    auth: AuthContext = Depends(require_auth),
) -> RequestListResponse:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")


@router.get("/{request_id}", response_model=RequestResponse)
async def get_request(
    request_id: UUID,
    auth: AuthContext = Depends(require_auth),
) -> RequestResponse:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")
