"""Runs — verdict-bearing executions. The JTBD lives here.

Postman lets you run a collection. Pagehub-evals adds:
* `harness_id` and `harness_claim` — what the LLM agent said it had
  done, captured at run-start so the verdict has the agent's claim
  in the same record (and so we can grep for "lying" patterns later).
* `verdict` — the engine writes pass/fail/error; the agent cannot
  override it. This is the "non-overrideable ground truth" piece.
* `evidence` — request/response pairs, twin-traffic counts, timing,
  any twin-zero-traffic assertion outcomes.
"""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


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
    collection_id: UUID | None = None
    environment_id: UUID | None = None
    harness_id: str | None = Field(default=None, max_length=200)
    harness_claim: str | None = Field(default=None, max_length=10000)


class RunResponse(BaseModel):
    id: UUID
    collection_id: UUID | None
    environment_id: UUID | None
    harness_id: str | None
    harness_claim: str | None
    status: RunStatus
    verdict: RunVerdict | None
    evidence: dict[str, Any]
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class RunListResponse(BaseModel):
    items: list[RunResponse]
