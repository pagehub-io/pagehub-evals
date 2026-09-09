from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from api.shared.jsonpath import JSON_PATH_LITE_PATTERN, is_valid_json_path


class EvaluationKind(str, Enum):
    STATUS_EQ = "status_eq"
    JSON_PATH_EQ = "json_path_eq"
    HEADER_PRESENT = "header_present"
    BODY_CONTAINS = "body_contains"
    JSON_PATH_EXISTS = "json_path_exists"
    JSON_PATH_NOT_EXISTS = "json_path_not_exists"
    JSON_PATH_CONTAINS = "json_path_contains"
    JSON_PATH_CMP = "json_path_cmp"


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


# The four kinds below are strict on purpose: stored config is persisted
# verbatim after validation, so a lax model would store ``"5"`` or ``True``
# for the evaluator to choke on at run time. Paths are validated against
# the tokenizer's real grammar (see api/shared/jsonpath.py); a bare ``$``
# would make ``json_path_exists`` pass on a 502 HTML page.


class _StrictPathConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: StrictStr = Field(..., min_length=1, pattern=JSON_PATH_LITE_PATTERN)

    @field_validator("path")
    @classmethod
    def _path_grammar(cls, v: str) -> str:
        if not is_valid_json_path(v):
            raise ValueError(f"path does not match the JSONPath-lite grammar: {v!r}")
        return v


class JsonPathExistsConfig(_StrictPathConfig):
    pass


class JsonPathNotExistsConfig(_StrictPathConfig):
    pass


class JsonPathContainsConfig(_StrictPathConfig):
    needle: StrictStr = Field(..., min_length=1)


class JsonPathCmpConfig(_StrictPathConfig):
    op: Literal["gt", "gte", "lt", "lte"]
    expected: StrictInt | StrictFloat = Field(..., allow_inf_nan=False)


_CONFIG_VALIDATORS: dict[EvaluationKind, type[BaseModel]] = {
    EvaluationKind.STATUS_EQ: StatusEqConfig,
    EvaluationKind.JSON_PATH_EQ: JsonPathEqConfig,
    EvaluationKind.HEADER_PRESENT: HeaderPresentConfig,
    EvaluationKind.BODY_CONTAINS: BodyContainsConfig,
    EvaluationKind.JSON_PATH_EXISTS: JsonPathExistsConfig,
    EvaluationKind.JSON_PATH_NOT_EXISTS: JsonPathNotExistsConfig,
    EvaluationKind.JSON_PATH_CONTAINS: JsonPathContainsConfig,
    EvaluationKind.JSON_PATH_CMP: JsonPathCmpConfig,
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
