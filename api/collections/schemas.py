from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CreateCollectionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class CollectionItemResponse(BaseModel):
    id: UUID
    collection_id: UUID
    request_id: UUID
    position: int


class CollectionResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    items: list[CollectionItemResponse] = Field(default_factory=list)


class CollectionListResponse(BaseModel):
    items: list[CollectionResponse]


class AddCollectionItemRequest(BaseModel):
    request_id: UUID
    position: int = Field(..., ge=0)
