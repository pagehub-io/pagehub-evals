from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class EvaluationKind(str, Enum):
    STATUS_EQ = "status_eq"
    JSON_PATH_EQ = "json_path_eq"
    JSON_PATH_PRESENT = "json_path_present"
    HEADER_PRESENT = "header_present"
    BODY_CONTAINS = "body_contains"
    LATENCY_LTE_MS = "latency_lte_ms"
    # Twin-zero-traffic: the canonical "ground-truth" assertion. With
    # X-Twin-{Dep}-Base-Url set on the run, asserts the real {Dep} saw
    # zero traffic attributable to this request.
    TWIN_TRAFFIC_ZERO = "twin_traffic_zero"


class CreateEvaluationRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    kind: EvaluationKind
    config: dict[str, Any] = Field(default_factory=dict)


class EvaluationResponse(BaseModel):
    id: UUID
    request_id: UUID
    name: str
    kind: EvaluationKind
    config: dict[str, Any]
    created_at: datetime


class EvaluationListResponse(BaseModel):
    items: list[EvaluationResponse]
