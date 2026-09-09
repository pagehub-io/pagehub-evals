"""Top-level response models for routes that live in ``api/main.py``."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# What this build of the engine supports beyond what OpenAPI shapes reveal.
# Consumers migrating suites probe this list rather than inferring from
# schema shapes (the builtin, the capture grammar and the retry policy are
# not visible in OpenAPI at all). A runs gate (RUNS_ENABLED=false) still
# answers 503 regardless of what is listed here.
CAPABILITIES: tuple[str, ...] = (
    "kinds:json_path_exists",
    "kinds:json_path_not_exists",
    "kinds:json_path_contains",
    "kinds:json_path_cmp",
    "request_timeout_ms",
    "run_id_builtin",
    "config_substitution",
    "filter_capture",
    "transient_retry",
    "run_budget",
)


class HealthResponse(BaseModel):
    status: str
    version: str
    env: str
    git_sha: str
    boot: dict[str, Any] | None = None
    capabilities: list[str] = Field(default_factory=lambda: list(CAPABILITIES))
