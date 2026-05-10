"""Events — immutable audit trail.

Stub routes (501).
"""

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import AuthContext, require_auth
from api.events.schemas import EventListResponse

router = APIRouter(prefix="/v1/events")


@router.get("", response_model=EventListResponse)
async def list_events(
    auth: AuthContext = Depends(require_auth),
) -> EventListResponse:
    raise HTTPException(status_code=501, detail="Not implemented in scaffold")
