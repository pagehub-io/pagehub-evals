"""Events writer — append-only audit trail.

Every state-changing handler should call ``record_event`` once the
state change is durable — either after its transaction commits, or
*inside* the handler's own transaction so the audit row commits (and
rolls back) atomically with the change it records. The latter is the
right choice when the event must never exist without the change, or
vice versa (e.g. fixture import). Events are visible via
``GET /v1/events`` and have no mutation endpoint.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg


async def record_event(
    db: asyncpg.Connection,
    *,
    actor_kind: str,
    actor_id: str | None,
    kind: str,
    target_kind: str,
    target_id: UUID | str | None,
    payload: dict[str, Any] | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO events (actor_kind, actor_id, kind, target_kind, target_id, payload)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        """,
        actor_kind,
        actor_id,
        kind,
        target_kind,
        target_id if isinstance(target_id, (UUID, type(None))) else UUID(str(target_id)),
        json.dumps(payload or {}),
    )
