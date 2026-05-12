"""Run schemas.

The verdict-bearing record. ``verdict`` and ``harness_claim`` are
engine-/create-time-only — no client can mutate them post-creation.
``CreateRunRequest`` is ``extra="forbid"`` so a client that POSTs a
``verdict`` (or any other engine-only field) gets 422 at the HTTP
boundary instead of a silent drop.
"""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class RunVerdict(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class CreateRunRequest(BaseModel):
    # Strict: only the four fields below are accepted. Any extra key
    # (verdict, status, evidence, started_at, finished_at, created_by_*)
    # → 422 at schema-validation. This is the HTTP-layer half of the
    # write-once contract; the route's PATCH-403 is the other half.
    model_config = ConfigDict(extra="forbid")

    collection_id: UUID | None = None
    environment_id: UUID | None = None
    harness_id: str | None = Field(default=None, max_length=200)
    harness_claim: str | None = Field(default=None, max_length=10000)


class RunEvaluationResult(BaseModel):
    id: UUID
    name: str
    kind: str
    passed: bool
    detail: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class RunRequestResult(BaseModel):
    request_id: UUID
    request_name: str
    method: str
    url: str
    response_status: int
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body_excerpt: Any = None
    latency_ms: int
    transport_error: str | None = None
    substitution_missed: list[str] = Field(default_factory=list)
    # KEYS ONLY. Captured *values* often carry live session tokens; the
    # schema enforces "no values persisted" by TYPE (list, not dict).
    captured: list[str] = Field(default_factory=list)
    evaluations: list[RunEvaluationResult] = Field(default_factory=list)
    passed: bool


class RunEvidence(BaseModel):
    requests: list[RunRequestResult] = Field(default_factory=list)
    # Engine-only sentinel — set when ``execute_run`` itself bombed
    # (pre-flight crash, HTTP-loop crash, etc.). Read by the UI as a
    # diagnostic banner. Declared explicitly (not via ``extra="allow"``)
    # so Pydantic preserves it through ``RunEvidence.model_validate``;
    # without this field, ``extra="ignore"`` (Pydantic default) silently
    # strips engine_error on the GET path.
    engine_error: str | None = None


class RunResponse(BaseModel):
    id: UUID
    created_by_kind: str
    created_by_id: str
    collection_id: UUID | None
    environment_id: UUID | None
    harness_id: str | None
    harness_claim: str | None
    status: RunStatus
    verdict: RunVerdict | None
    evidence: RunEvidence
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class RunListResponse(BaseModel):
    items: list[RunResponse]
