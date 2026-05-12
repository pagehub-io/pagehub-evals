"""Fixtures — declarative JSON bundles imported as collections / requests /
evaluations / environments.

``POST /v1/fixtures/import`` is *declarative desired-state*: importing a
bundle **replaces** each named request's evaluation set and each named
collection's item list with exactly what the bundle declares — rows added
out-of-band via the resource APIs since the last import are discarded. (It
does NOT delete a request / collection / environment the bundle doesn't
mention; only *children of mentioned parents* get the replace treatment.)
The whole import runs in one transaction — all-or-nothing. Re-importing the
same bundle reports ``created: 0`` for every kind — that's the idempotency
signal.

Authoring is operator-only (``require_user``). Errors ride FastAPI's
default ``{"detail": ...}`` envelope — no dedicated error model.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from api.dependencies import AuthContext, require_user
from api.fixtures.engine import FixtureImportError, import_bundle
from api.fixtures.schemas import (
    VOLATILE_FIELDS,
    FixtureBundle,
    FixtureImportResponse,
)
from api.shared.events import record_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/fixtures")

# The raw request body is bounded *before* JSON parsing by
# api.fixtures._body_limit.FixtureBodyLimitMiddleware (Starlette has no
# default body limit; the post-parse caps below — _MAX_REQUESTS etc. on
# FixtureBundle — are a second line, not the size guard).


def _collect_volatile_warnings(raw: object) -> list[str]:
    """Best-effort scan of the raw bundle dict for stray server-assigned fields."""
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return warnings
    for section in ("environments", "requests", "collections"):
        items = raw.get(section)
        if not isinstance(items, list):
            continue
        for idx, obj in enumerate(items):
            if not isinstance(obj, dict):
                continue
            for field in VOLATILE_FIELDS:
                if field in obj:
                    warnings.append(f"{section}[{idx}]: ignored unexpected field {field!r}")
            if section == "requests" and isinstance(obj.get("evaluations"), list):
                for ev_idx, ev in enumerate(obj["evaluations"]):
                    if not isinstance(ev, dict):
                        continue
                    for field in VOLATILE_FIELDS:
                        if field in ev:
                            warnings.append(
                                f"{section}[{idx}].evaluations[{ev_idx}]: "
                                f"ignored unexpected field {field!r}"
                            )
    return warnings


async def _raw_bundle_dict(request: Request) -> dict:
    """The raw parsed JSON body (cached by Starlette) — used only for the
    volatile-field warning scan; the typed ``FixtureBundle`` is the contract."""
    try:
        raw = await request.body()
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


@router.post("/import", response_model=FixtureImportResponse)
async def import_fixture(
    body: FixtureBundle,
    request: Request,
    auth: AuthContext = Depends(require_user),
) -> FixtureImportResponse:
    volatile_warnings = _collect_volatile_warnings(await _raw_bundle_dict(request))

    eval_total = sum(len(r.evaluations) for r in body.requests)
    logger.info(
        "fixture.import.start environments=%d requests=%d collections=%d evaluations=%d",
        len(body.environments),
        len(body.requests),
        len(body.collections),
        eval_total,
    )

    async with auth.db.transaction():
        try:
            result = await import_bundle(auth.db, auth.actor_id, body)
        except FixtureImportError as e:
            logger.warning("fixture.import.failed detail=%r", e.detail)
            raise HTTPException(status_code=e.status_code, detail=e.detail) from e

        result.warnings = [*volatile_warnings, *result.warnings]

        await record_event(
            auth.db,
            actor_kind=auth.actor_kind,
            actor_id=auth.actor_id,
            kind="fixtures.imported",
            target_kind="fixtures",
            target_id=None,
            payload={
                "environments": result.environments.model_dump(),
                "requests": result.requests.model_dump(),
                "evaluations": result.evaluations.model_dump(),
                "collections": result.collections.model_dump(),
                "warning_count": len(result.warnings),
            },
        )

    logger.info(
        "fixture.import.ok environments=%s requests=%s evaluations=%s collections=%s warnings=%d",
        result.environments.model_dump(),
        result.requests.model_dump(),
        result.evaluations.model_dump(),
        result.collections.model_dump(),
        len(result.warnings),
    )
    return result
