from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from api.shared.jsonpath import check_reserved_names


class CreateEnvironmentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    variables: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)

    @field_validator("variables", "secrets")
    @classmethod
    def _no_reserved(cls, v: dict[str, str]) -> dict[str, str]:
        check_reserved_names(v, what="environment")
        return v


class UpdateEnvironmentRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    variables: dict[str, str] | None = None
    secrets: dict[str, str] | None = None

    @field_validator("variables", "secrets")
    @classmethod
    def _no_reserved(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        check_reserved_names(v, what="environment")
        return v


class EnvironmentResponse(BaseModel):
    id: UUID
    name: str
    variables: dict[str, str]
    # Either {"key": "********"} (default) or plaintext (?reveal_secrets=true).
    secrets: dict[str, str]
    created_at: datetime
    updated_at: datetime


class EnvironmentListResponse(BaseModel):
    items: list[EnvironmentResponse]
