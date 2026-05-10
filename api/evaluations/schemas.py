from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class EvaluationKind(str, Enum):
    STATUS_EQ = "status_eq"
    JSON_PATH_EQ = "json_path_eq"
    HEADER_PRESENT = "header_present"
    BODY_CONTAINS = "body_contains"


# Per-kind config schemas. Used to validate `config` strictly at write
# time (per spec: unknown shape → 422, not silently stored and no-op'd
# at run time).

class StatusEqConfig(BaseModel):
    expected: int = Field(..., ge=100, le=599)


class JsonPathEqConfig(BaseModel):
    path: str = Field(..., min_length=1)
    expected: Any


class HeaderPresentConfig(BaseModel):
    header: str = Field(..., min_length=1)


class BodyContainsConfig(BaseModel):
    needle: str = Field(..., min_length=1)


_CONFIG_VALIDATORS: dict[EvaluationKind, type[BaseModel]] = {
    EvaluationKind.STATUS_EQ: StatusEqConfig,
    EvaluationKind.JSON_PATH_EQ: JsonPathEqConfig,
    EvaluationKind.HEADER_PRESENT: HeaderPresentConfig,
    EvaluationKind.BODY_CONTAINS: BodyContainsConfig,
}


class CreateEvaluationRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    kind: EvaluationKind
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_config_shape(self) -> "CreateEvaluationRequest":
        validator = _CONFIG_VALIDATORS[self.kind]
        # Will raise ValidationError → 422 from FastAPI.
        validator(**self.config)
        return self


class EvaluationResponse(BaseModel):
    id: UUID
    request_id: UUID
    name: str
    kind: EvaluationKind
    config: dict[str, Any]
    created_at: datetime


class EvaluationListResponse(BaseModel):
    items: list[EvaluationResponse]
