"""Fixture import / export logic.

FastAPI-free, like ``api/runs/engine.py`` — the route layer owns the
transaction, HTTP status mapping, and ``record_event``. This module takes
an ``asyncpg.Connection`` (already inside a transaction, opened by the
route) plus an owner id and a validated ``FixtureBundle``, and runs the
whole import; and the inverse, ``build_export(conn, collection_id)``.

Import is *declarative desired-state* for children: a request's
``evaluations[]`` and a collection's ``items[]`` are delete-all-then-
reinsert (rows present in the DB but absent from the fixture are dropped).
Top-level resources (environments / requests / collections) are *upserted*,
never deleted — the bundle never removes a resource it doesn't mention.

Secrets never traverse the fixture boundary: ``FixtureEnvironment.secrets``
is keys-only with ``""`` values (parse-time enforced), and on import the
engine preserves the target environment's stored ciphertext for a known key
and writes ``encrypt("")`` as an unset placeholder for a new key — ``decrypt``
is never on this path. Export emits ``environments: []`` always and never
queries ``environments``.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from api.fixtures.schemas import (
    FIXTURE_VERSION,
    FixtureBundle,
    FixtureCounts,
    FixtureImportResponse,
)
from api.shared.secrets import encrypt


class FixtureImportError(Exception):
    """Engine-level signal for a validation failure surfaced during import.

    Not a Pydantic model — the route maps this to ``HTTPException(422, detail)``
    (or whatever ``status_code`` it carries) and the transaction rolls back.
    """

    def __init__(self, detail: Any, *, status_code: int = 422) -> None:
        super().__init__(str(detail))
        self.detail = detail
        self.status_code = status_code


def _coerce_jsonb_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _coerce_jsonb_any(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


async def import_bundle(
    conn,
    actor_id: str,
    bundle: FixtureBundle,
) -> FixtureImportResponse:
    """Apply ``bundle`` for ``actor_id``. Caller wraps in one transaction."""
    counts = {
        "environments": {"created": 0, "updated": 0},
        "requests": {"created": 0, "updated": 0},
        "evaluations": {"created": 0, "updated": 0},
        "collections": {"created": 0, "updated": 0},
    }
    warnings: list[str] = []

    if not (bundle.environments or bundle.requests or bundle.collections):
        warnings.append("fixture contained no resources")

    # 1. ENVIRONMENTS — read-then-merge for secrets.
    for env in bundle.environments:
        stored_raw = await conn.fetchval(
            "SELECT secrets FROM environments WHERE owner_user_id = $1 AND name = $2",
            actor_id,
            env.name,
        )
        merged = _coerce_jsonb_dict(stored_raw)
        for key in env.secrets:  # every value is "" (parse-time enforced)
            if key not in merged:
                merged[key] = encrypt("")
            # else: keep the stored ciphertext untouched
        row = await conn.fetchrow(
            """
            INSERT INTO environments (owner_user_id, name, variables, secrets)
            VALUES ($1, $2, $3::jsonb, $4::jsonb)
            ON CONFLICT (owner_user_id, name) DO UPDATE
              SET variables = EXCLUDED.variables, secrets = EXCLUDED.secrets, updated_at = now()
            RETURNING (xmax = 0) AS inserted
            """,
            actor_id,
            env.name,
            json.dumps(env.variables),
            json.dumps(merged),
        )
        counts["environments"]["created" if row["inserted"] else "updated"] += 1

    # 2. REQUESTS (+ their evaluations, delete-all-then-reinsert).
    name_to_request_id: dict[str, UUID] = {}
    for req in bundle.requests:
        row = await conn.fetchrow(
            """
            INSERT INTO requests (owner_user_id, name, method, url, headers, body, capture)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb)
            ON CONFLICT (owner_user_id, name) DO UPDATE
              SET method = EXCLUDED.method, url = EXCLUDED.url, headers = EXCLUDED.headers,
                  body = EXCLUDED.body, capture = EXCLUDED.capture, updated_at = now()
            RETURNING id, (xmax = 0) AS inserted
            """,
            actor_id,
            req.name,
            req.method,
            req.url,
            json.dumps(req.headers),
            json.dumps(req.body) if req.body is not None else None,
            json.dumps(req.capture),
        )
        rid = row["id"]
        name_to_request_id[req.name] = rid
        bucket = "created" if row["inserted"] else "updated"
        counts["requests"][bucket] += 1

        await conn.execute("DELETE FROM evaluations WHERE request_id = $1", rid)
        for ev in req.evaluations:
            await conn.execute(
                "INSERT INTO evaluations (request_id, name, kind, config) VALUES ($1, $2, $3, $4::jsonb)",
                rid,
                ev.name,
                ev.kind.value,
                json.dumps(ev.config),
            )
        # The eval set counts under the parent request's bucket.
        counts["evaluations"][bucket] += len(req.evaluations)

    # 3. COLLECTIONS (+ their items, delete-all-then-reinsert, bundle-only resolution).
    for col in bundle.collections:
        row = await conn.fetchrow(
            """
            INSERT INTO collections (owner_user_id, name, description)
            VALUES ($1, $2, $3)
            ON CONFLICT (owner_user_id, name) DO UPDATE
              SET description = EXCLUDED.description, updated_at = now()
            RETURNING id, (xmax = 0) AS inserted
            """,
            actor_id,
            col.name,
            col.description,
        )
        cid = row["id"]
        counts["collections"]["created" if row["inserted"] else "updated"] += 1

        await conn.execute("DELETE FROM collection_items WHERE collection_id = $1", cid)
        for position, item_name in enumerate(col.items):
            rid = name_to_request_id.get(item_name)
            if rid is None:
                # Parse-time validation should have caught this; defensive.
                raise FixtureImportError(
                    {
                        "error": "unresolved_request_ref",
                        "collection": col.name,
                        "item_index": position,
                        "request": item_name,
                    }
                )
            await conn.execute(
                "INSERT INTO collection_items (collection_id, request_id, position) VALUES ($1, $2, $3)",
                cid,
                rid,
                position,
            )

    return FixtureImportResponse(
        environments=FixtureCounts(**counts["environments"]),
        requests=FixtureCounts(**counts["requests"]),
        evaluations=FixtureCounts(**counts["evaluations"]),
        collections=FixtureCounts(**counts["collections"]),
        warnings=warnings,
    )


# ---------- export ----------


async def build_export(conn, collection_id: UUID) -> FixtureBundle:
    """Project a collection into a ``FixtureBundle``. Returns ``None``-free.

    Raises ``FixtureImportError(404, ...)`` if the collection doesn't exist —
    the route maps that to ``HTTPException(404, ...)``. (Reusing the engine
    error type keeps the route thin.)
    """
    col = await conn.fetchrow(
        "SELECT id, name, description FROM collections WHERE id = $1",
        collection_id,
    )
    if col is None:
        raise FixtureImportError({"error": "collection_not_found"}, status_code=404)

    items = await conn.fetch(
        """
        SELECT ci.position, r.id AS rid, r.name, r.method, r.url, r.headers, r.body, r.capture
        FROM collection_items ci
        JOIN requests r ON r.id = ci.request_id
        WHERE ci.collection_id = $1
        ORDER BY ci.position ASC
        """,
        collection_id,
    )

    seen: set[UUID] = set()
    ordered_rids: list[UUID] = []
    requests_out: list[dict[str, Any]] = []
    request_index: dict[UUID, dict[str, Any]] = {}
    for it in items:
        if it["rid"] in seen:
            continue
        seen.add(it["rid"])
        ordered_rids.append(it["rid"])
        entry = {
            "name": it["name"],
            "method": it["method"],
            "url": it["url"],
            "headers": _coerce_jsonb_dict(it["headers"]),
            "body": _coerce_jsonb_any(it["body"]),
            "capture": _coerce_jsonb_dict(it["capture"]),
            "evaluations": [],
        }
        requests_out.append(entry)
        request_index[it["rid"]] = entry

    if ordered_rids:
        evals = await conn.fetch(
            """
            SELECT request_id, name, kind, config
            FROM evaluations
            WHERE request_id = ANY($1::uuid[])
            ORDER BY request_id, created_at ASC, name ASC
            """,
            ordered_rids,
        )
        for ev in evals:
            request_index[ev["request_id"]]["evaluations"].append(
                {
                    "name": ev["name"],
                    "kind": ev["kind"],
                    "config": _coerce_jsonb_dict(ev["config"]),
                }
            )

    return FixtureBundle.model_validate(
        {
            "version": FIXTURE_VERSION,
            "environments": [],
            "requests": requests_out,
            "collections": [
                {
                    "name": col["name"],
                    "description": col["description"],
                    "items": [it["name"] for it in items],
                }
            ],
        }
    )
