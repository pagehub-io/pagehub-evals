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

from api.shared.jsonpath import is_valid_json_path


class EvaluationKind(str, Enum):
    STATUS_EQ = "status_eq"
    JSON_PATH_EQ = "json_path_eq"
    HEADER_PRESENT = "header_present"
    BODY_CONTAINS = "body_contains"
    JSON_PATH_EXISTS = "json_path_exists"
    JSON_PATH_NOT_EXISTS = "json_path_not_exists"
    JSON_PATH_CONTAINS = "json_path_contains"
    JSON_PATH_NOT_CONTAINS = "json_path_not_contains"
    JSON_PATH_CMP = "json_path_cmp"
    JSON_PATH_NEQ = "json_path_neq"


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


# The path-bearing kinds below are strict on purpose: stored config is persisted
# verbatim after validation, so a lax model would store ``"5"`` or ``True``
# for the evaluator to choke on at run time. Paths are validated against
# the tokenizer's real grammar (see api/shared/jsonpath.py); a bare ``$``
# would make ``json_path_exists`` pass on a 502 HTML page.


class _StrictPathConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: StrictStr = Field(..., min_length=1)

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


class _ContainsPathConfig(BaseModel):
    # Like _StrictPathConfig but ALSO admits path=="" (the raw whole-body
    # match: platform ``actual = _extract_path(body, path) if path else
    # (body, True)``). Used ONLY by the two contains kinds — exists/not_exists/
    # cmp keep the strict grammar (a bare/whole-body path there is a footgun).
    # The converter emits only "" (raw) or a "$."-prefixed path (jpath), so a
    # bare "$"/"$." / index-first path is correctly rejected here.
    model_config = ConfigDict(extra="forbid")

    path: StrictStr

    @field_validator("path")
    @classmethod
    def _path_grammar(cls, v: str) -> str:
        if v == "" or is_valid_json_path(v):
            return v
        raise ValueError(
            f"path must be '' (raw whole-body) or match the JSONPath-lite grammar: {v!r}"
        )


class JsonPathContainsConfig(_ContainsPathConfig):
    needle: StrictStr = Field(..., min_length=1)


class JsonPathNotContainsConfig(_ContainsPathConfig):
    needle: StrictStr = Field(..., min_length=1)


class JsonPathCmpConfig(_StrictPathConfig):
    op: Literal["gt", "gte", "lt", "lte"]
    expected: StrictInt | StrictFloat = Field(..., allow_inf_nan=False)


class JsonPathNeqConfig(_StrictPathConfig):
    # Strict path (rejects ""/"$"/"$." — neq is always pathed, and a whole-body
    # neq would fail OPEN: engine whole-body != expected → True, while platform
    # returns not-found→False). expected is Any (incl None) like JsonPathEqConfig,
    # BUT scalar-only: a structured {{VAR}} (e.g. {"id":"{{X}}"}) is not recursed by
    # _render_config, so it stays a literal template — and neq's polarity makes that
    # fail OPEN (observed != {template} → True), a vacuous pass on a leak assertion.
    # eq is fail-closed there so it needs no such guard; neq fails loud at import.
    expected: Any

    @field_validator("expected")
    @classmethod
    def _scalar_expected(cls, v: Any) -> Any:
        if isinstance(v, dict | list):
            raise ValueError(
                "json_path_neq expected must be a scalar (str/number/bool/null); "
                "a structured expected is not variable-substituted and would fail open"
            )
        return v


_CONFIG_VALIDATORS: dict[EvaluationKind, type[BaseModel]] = {
    EvaluationKind.STATUS_EQ: StatusEqConfig,
    EvaluationKind.JSON_PATH_EQ: JsonPathEqConfig,
    EvaluationKind.HEADER_PRESENT: HeaderPresentConfig,
    EvaluationKind.BODY_CONTAINS: BodyContainsConfig,
    EvaluationKind.JSON_PATH_EXISTS: JsonPathExistsConfig,
    EvaluationKind.JSON_PATH_NOT_EXISTS: JsonPathNotExistsConfig,
    EvaluationKind.JSON_PATH_CONTAINS: JsonPathContainsConfig,
    EvaluationKind.JSON_PATH_NOT_CONTAINS: JsonPathNotContainsConfig,
    EvaluationKind.JSON_PATH_CMP: JsonPathCmpConfig,
    EvaluationKind.JSON_PATH_NEQ: JsonPathNeqConfig,
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
