from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CreateHarnessKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class HarnessKeyMintedResponse(BaseModel):
    """The ONLY response that includes the plaintext secret. Returned
    once at creation; subsequent reads use ``HarnessKeyResponse``."""
    id: UUID
    name: str
    secret: str
    secret_prefix: str
    created_at: datetime


class HarnessKeyResponse(BaseModel):
    id: UUID
    name: str
    secret_prefix: str
    created_at: datetime
    revoked_at: datetime | None


class HarnessKeyListResponse(BaseModel):
    items: list[HarnessKeyResponse]
