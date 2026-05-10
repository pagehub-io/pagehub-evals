"""Environments — named config contexts with variables and Fernet-encrypted secrets.

Stub routes (501) — schema + contract only. Implementation lands in
the JTBD pivot.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import AuthContext, require_auth
from api.environments.schemas import (
    CreateEnvironmentRequest,
    EnvironmentListResponse,
    EnvironmentResponse,
    UpdateEnvironmentRequest,
)

router = APIRouter(prefix="/v1/environments")


@router.post("", response_model=EnvironmentResponse, status_code=201)
async def create_environment(
    body: CreateEnvironmentRequest,
    auth: AuthContext = Depends(require_auth),
) -> EnvironmentResponse:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")


@router.get("", response_model=EnvironmentListResponse)
async def list_environments(
    auth: AuthContext = Depends(require_auth),
) -> EnvironmentListResponse:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")


@router.get("/{environment_id}", response_model=EnvironmentResponse)
async def get_environment(
    environment_id: UUID,
    reveal_secrets: bool = False,
    auth: AuthContext = Depends(require_auth),
) -> EnvironmentResponse:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")


@router.patch("/{environment_id}", response_model=EnvironmentResponse)
async def update_environment(
    environment_id: UUID,
    body: UpdateEnvironmentRequest,
    auth: AuthContext = Depends(require_auth),
) -> EnvironmentResponse:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")


@router.delete("/{environment_id}", status_code=204)
async def delete_environment(
    environment_id: UUID,
    auth: AuthContext = Depends(require_auth),
) -> None:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")
