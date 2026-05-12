"""Fixture bundle data shapes — the file format = ``FixtureBundle``.

Pure data + validation. No DB, no FastAPI. Parse-time validation catches
(and 422s, via FastAPI body validation) every structural error before the
import engine runs: unknown version, bad eval kind/config, non-empty secret
value, object-form collection items, oversize bundle, a collection item
naming a request not in the bundle, duplicate names within the bundle.

The Architect's contract: errors ride FastAPI's default ``{"detail": ...}``
envelope; there is NO ``FixtureImportError`` model. Pydantic's own
``ValidationError`` already emits the standard ``{"detail": [{loc, msg}]}``
list; richer 422s the engine signals are raised by the route as
``HTTPException(422, detail=<dict or str>)``.

Volatile / server-assigned fields (``id``, ``created_at``, ``updated_at``,
``owner_user_id``, ``request_id``, ``collection_id``, ``position``) do not
appear in these models at all — they cannot leak into an export, and
``model_config`` is left at Pydantic's default ``extra='ignore'`` so a
stray one on a hand-authored / export-tweaked object is silently dropped
(the route adds a ``warnings[]`` entry for the obvious ones).
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from api.evaluations.schemas import _CONFIG_VALIDATORS, EvaluationKind
from api.requests.schemas import _validate_capture_dict
from api.runs._constants import COLLECTION_ITEM_CAP

FIXTURE_VERSION = 1

# Bundle size caps — parse-time rejections, no DB churn. The collection-item
# cap tracks the run engine's hard cap so a fixture can't declare a
# collection the engine would refuse to execute.
_MAX_REQUESTS = 200
_MAX_COLLECTIONS = 50
_MAX_ENVIRONMENTS = 50
_MAX_EVALUATIONS_PER_REQUEST = 20
_MAX_COLLECTION_ITEMS = COLLECTION_ITEM_CAP  # leaf constant in api/runs/_constants.py

# Server-assigned fields that are silently stripped (Pydantic ignores them)
# but worth a warning when an export-tweak-reimport loop leaves them on an
# object. The route inspects the raw bundle dict for these.
VOLATILE_FIELDS = (
    "id",
    "created_at",
    "updated_at",
    "owner_user_id",
    "request_id",
    "collection_id",
    "position",
)


class FixtureEvaluation(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    kind: EvaluationKind
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_config_shape(self) -> FixtureEvaluation:
        validator = _CONFIG_VALIDATORS[self.kind]
        validator(**self.config)  # ValidationError → 422
        return self


class FixtureRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    method: str = Field(..., pattern="^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$")
    url: str = Field(..., min_length=1, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    capture: dict[str, str] = Field(default_factory=dict)
    evaluations: list[FixtureEvaluation] = Field(
        default_factory=list, max_length=_MAX_EVALUATIONS_PER_REQUEST
    )

    @field_validator("capture")
    @classmethod
    def _check_capture(cls, v: dict[str, str]) -> dict[str, str]:
        return _validate_capture_dict(v)


class FixtureEnvironment(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    variables: dict[str, str] = Field(default_factory=dict)
    # Keys only. Every value MUST be "" — a non-empty value is a hard 422 at
    # parse time. A git-tracked fixture must never carry decrypted secret
    # material; the operator fills values in later via PATCH /v1/environments.
    secrets: dict[str, str] = Field(default_factory=dict)

    @field_validator("secrets")
    @classmethod
    def _secrets_keys_only(cls, v: dict[str, str]) -> dict[str, str]:
        for key, value in v.items():
            if value != "":
                raise ValueError(
                    "fixtures must not contain secret values; use empty placeholders "
                    f"(secret key {key!r} has a non-empty value)"
                )
        return v


class FixtureCollection(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    # ARRAY of request *names* (strings). Array index == position. An item
    # given as an object is a plain type error → 422.
    items: list[str] = Field(default_factory=list, max_length=_MAX_COLLECTION_ITEMS)


class FixtureBundle(BaseModel):
    # An unknown TOP-LEVEL key is structural → 422. (Unknown fields *inside*
    # a resource stay forward-compat: those models keep extra='ignore'.)
    model_config = ConfigDict(extra="forbid")

    version: int
    environments: list[FixtureEnvironment] = Field(
        default_factory=list, max_length=_MAX_ENVIRONMENTS
    )
    requests: list[FixtureRequest] = Field(
        default_factory=list, max_length=_MAX_REQUESTS
    )
    collections: list[FixtureCollection] = Field(
        default_factory=list, max_length=_MAX_COLLECTIONS
    )

    @field_validator("version")
    @classmethod
    def _version_supported(cls, v: int) -> int:
        if v != FIXTURE_VERSION:
            raise ValueError(
                f"unsupported fixture version {v}; supported: [{FIXTURE_VERSION}]"
            )
        return v

    @model_validator(mode="after")
    def _validate_internal_consistency(self) -> FixtureBundle:
        # Duplicate names within a resource type → reject before any DB write.
        for kind, names in (
            ("environment", [e.name for e in self.environments]),
            ("request", [r.name for r in self.requests]),
            ("collection", [c.name for c in self.collections]),
        ):
            seen: set[str] = set()
            for name in names:
                if name in seen:
                    raise ValueError(f"duplicate {kind} name in bundle: {name!r}")
                seen.add(name)
        # Bundle-only request-name resolution: every collection item name
        # MUST appear in this bundle's requests[].
        request_names = {r.name for r in self.requests}
        for col in self.collections:
            for idx, item_name in enumerate(col.items):
                if item_name not in request_names:
                    raise ValueError(
                        f"collection {col.name!r} item {idx} references request "
                        f"{item_name!r} which is not in this bundle's requests[]"
                    )
        return self


# ---------- import response ----------


class FixtureCounts(BaseModel):
    created: int = 0
    updated: int = 0


class FixtureImportResponse(BaseModel):
    environments: FixtureCounts = Field(default_factory=FixtureCounts)
    requests: FixtureCounts = Field(default_factory=FixtureCounts)
    evaluations: FixtureCounts = Field(default_factory=FixtureCounts)
    collections: FixtureCounts = Field(default_factory=FixtureCounts)
    warnings: list[str] = Field(default_factory=list)


# ---------- round-trip normalization ----------


def normalize_for_roundtrip(bundle: dict) -> dict:
    """Canonical form for the ``export → import → export`` identity check.

    Single source of truth, used by both the pytest round-trip test and the
    platform eval seed. Steps (per the Architect's Contracts section):

    1. Drop server-assigned fields wherever they appear (defensive — they
       shouldn't be in an export, but a hand-authored input might leak one).
    2. Drop ``environments`` from the comparison entirely (export emits ``[]``;
       import of a collection-only bundle ignores absent envs — symmetric).
    3. Leave ``headers`` / request ``body`` / evaluation ``config`` /
       environment ``variables`` as-is (dicts compare order-insensitively).
    4. Sort ``requests`` by name; sort ``collections`` by name; sort each
       request's ``evaluations`` by name; keep collection ``items`` in array
       order (order is meaningful).
    5. Carry ``version`` through.
    """

    def _strip(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: _strip(v) for k, v in obj.items() if k not in VOLATILE_FIELDS}
        if isinstance(obj, list):
            return [_strip(v) for v in obj]
        return obj

    src = copy.deepcopy(bundle)
    out: dict[str, Any] = {"version": src.get("version")}

    requests = [_strip(r) for r in src.get("requests", [])]
    for r in requests:
        if isinstance(r, dict) and isinstance(r.get("evaluations"), list):
            r["evaluations"] = sorted(
                r["evaluations"],
                key=lambda e: e.get("name", "") if isinstance(e, dict) else "",
            )
    out["requests"] = sorted(
        requests, key=lambda r: r.get("name", "") if isinstance(r, dict) else ""
    )

    collections = [_strip(c) for c in src.get("collections", [])]
    out["collections"] = sorted(
        collections, key=lambda c: c.get("name", "") if isinstance(c, dict) else ""
    )

    return out
