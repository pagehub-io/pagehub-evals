import re
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, StrictInt, field_validator

from api.shared.jsonpath import check_reserved_names, is_valid_json_path

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

_CAPTURE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TIMEOUT_MS_MIN = 100
TIMEOUT_MS_MAX = 60000
_MAX_CAPTURE_ENTRIES = 32
_MAX_CAPTURE_KEY_LEN = 64


def _validate_capture_dict(v: dict[str, str]) -> dict[str, str]:
    if len(v) > _MAX_CAPTURE_ENTRIES:
        raise ValueError(f"at most {_MAX_CAPTURE_ENTRIES} captures per request")
    for k, val in v.items():
        if not isinstance(k, str) or len(k) > _MAX_CAPTURE_KEY_LEN:
            raise ValueError(f"capture key too long (>{_MAX_CAPTURE_KEY_LEN}): {k!r}")
        if not _CAPTURE_KEY_RE.fullmatch(k):
            raise ValueError(
                f"capture key must match ^[A-Za-z_][A-Za-z0-9_]*$: {k!r}"
            )
        if not is_valid_json_path(val):
            raise ValueError(
                "capture value must match the JSONPath-lite grammar "
                f"($.field, [int], [?(@.key=='value')]): {val!r}"
            )
    check_reserved_names(v, what="capture")
    return v


class CreateRequestRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    method: str = Field(..., pattern="^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$")
    url: str = Field(..., min_length=1, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    capture: dict[str, str] = Field(default_factory=dict)
    # None means the engine default. Bounded so a bundle cannot make every
    # request fail instantly by accident, nor hold a run open indefinitely.
    timeout_ms: StrictInt | None = Field(default=None, ge=TIMEOUT_MS_MIN, le=TIMEOUT_MS_MAX)

    @field_validator("capture")
    @classmethod
    def _check_capture(cls, v: dict[str, str]) -> dict[str, str]:
        return _validate_capture_dict(v)


class UpdateRequestRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    method: str | None = Field(
        default=None, pattern="^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$"
    )
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    headers: dict[str, str] | None = None
    body: Any = None
    capture: dict[str, str] | None = None

    @field_validator("capture")
    @classmethod
    def _check_capture(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        return _validate_capture_dict(v)


class RequestResponse(BaseModel):
    id: UUID
    name: str
    method: str
    url: str
    headers: dict[str, str]
    body: Any
    capture: dict[str, str] = Field(default_factory=dict)
    timeout_ms: int | None = None
    created_at: datetime
    updated_at: datetime


class RequestListResponse(BaseModel):
    items: list[RequestResponse]
