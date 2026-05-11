from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


class CreateRequestRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    method: str = Field(..., pattern="^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$")
    url: str = Field(..., min_length=1, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None


class RequestResponse(BaseModel):
    id: UUID
    name: str
    method: str
    url: str
    headers: dict[str, str]
    body: Any
    created_at: datetime
    updated_at: datetime


class RequestListResponse(BaseModel):
    items: list[RequestResponse]
