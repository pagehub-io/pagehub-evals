"""Environments: variables (plaintext) + secrets (Fernet-at-rest).

Authoring is operator-only. Secrets are masked in responses by
default. ``?reveal_secrets=true`` requires admin.
"""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import AuthContext, require_user
from api.environments.schemas import (
    CreateEnvironmentRequest,
    EnvironmentListResponse,
    EnvironmentResponse,
    UpdateEnvironmentRequest,
)
from api.shared.events import record_event
from api.shared.secrets import decrypt, encrypt, mask

router = APIRouter(prefix="/v1/environments")


def _encrypt_secret_map(secrets: dict[str, str]) -> dict[str, str]:
    return {k: encrypt(v) for k, v in secrets.items()}


def _mask_secret_map(stored: dict[str, str]) -> dict[str, str]:
    return {k: mask(v) for k, v in stored.items()}


def _reveal_secret_map(stored: dict[str, str]) -> dict[str, str]:
    return {k: decrypt(v) for k, v in stored.items()}


def _row_to_response(row, *, reveal: bool) -> EnvironmentResponse:
    stored = row["secrets"] or {}
    if isinstance(stored, str):
        stored = json.loads(stored)
    variables = row["variables"] or {}
    if isinstance(variables, str):
        variables = json.loads(variables)
    return EnvironmentResponse(
        id=row["id"],
        name=row["name"],
        variables=variables,
        secrets=_reveal_secret_map(stored) if reveal else _mask_secret_map(stored),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.post("", response_model=EnvironmentResponse, status_code=201)
async def create_environment(
    body: CreateEnvironmentRequest,
    auth: AuthContext = Depends(require_user),
) -> EnvironmentResponse:
    encrypted = _encrypt_secret_map(body.secrets)
    row = await auth.db.fetchrow(
        """
        INSERT INTO environments (owner_user_id, name, variables, secrets)
        VALUES ($1, $2, $3::jsonb, $4::jsonb)
        RETURNING id, name, variables, secrets, created_at, updated_at
        """,
        auth.actor_id,
        body.name,
        json.dumps(body.variables),
        json.dumps(encrypted),
    )
    await record_event(
        auth.db,
        actor_kind=auth.actor_kind,
        actor_id=auth.actor_id,
        kind="environment.created",
        target_kind="environment",
        target_id=row["id"],
        payload={"name": row["name"]},
    )
    # Always mask on the create response — even from admin — to nudge
    # the operator toward not pasting plaintext into other UIs.
    return _row_to_response(row, reveal=False)


@router.get("", response_model=EnvironmentListResponse)
async def list_environments(
    auth: AuthContext = Depends(require_user),
) -> EnvironmentListResponse:
    rows = await auth.db.fetch(
        """
        SELECT id, name, variables, secrets, created_at, updated_at
        FROM environments
        ORDER BY created_at DESC
        LIMIT 500
        """
    )
    return EnvironmentListResponse(
        items=[_row_to_response(r, reveal=False) for r in rows]
    )


@router.get("/{environment_id}", response_model=EnvironmentResponse)
async def get_environment(
    environment_id: UUID,
    reveal_secrets: bool = False,
    auth: AuthContext = Depends(require_user),
) -> EnvironmentResponse:
    if reveal_secrets and not auth.is_admin:
        raise HTTPException(status_code=403, detail="Admin only for ?reveal_secrets=true")
    row = await auth.db.fetchrow(
        """
        SELECT id, name, variables, secrets, created_at, updated_at
        FROM environments
        WHERE id = $1
        """,
        environment_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Environment not found")
    return _row_to_response(row, reveal=reveal_secrets)


@router.patch("/{environment_id}", response_model=EnvironmentResponse)
async def update_environment(
    environment_id: UUID,
    body: UpdateEnvironmentRequest,
    auth: AuthContext = Depends(require_user),
) -> EnvironmentResponse:
    fields: list[str] = []
    values: list = []
    if body.name is not None:
        fields.append(f"name = ${len(values) + 1}")
        values.append(body.name)
    if body.variables is not None:
        fields.append(f"variables = ${len(values) + 1}::jsonb")
        values.append(json.dumps(body.variables))
    if body.secrets is not None:
        fields.append(f"secrets = ${len(values) + 1}::jsonb")
        values.append(json.dumps(_encrypt_secret_map(body.secrets)))
    if not fields:
        raise HTTPException(status_code=400, detail="No updatable fields supplied")
    fields.append("updated_at = now()")
    values.append(environment_id)
    sql = (
        f"UPDATE environments SET {', '.join(fields)} "
        f"WHERE id = ${len(values)} "
        f"RETURNING id, name, variables, secrets, created_at, updated_at"
    )
    row = await auth.db.fetchrow(sql, *values)
    if row is None:
        raise HTTPException(status_code=404, detail="Environment not found")
    await record_event(
        auth.db,
        actor_kind=auth.actor_kind,
        actor_id=auth.actor_id,
        kind="environment.updated",
        target_kind="environment",
        target_id=row["id"],
        payload={"name": row["name"]},
    )
    return _row_to_response(row, reveal=False)


@router.delete("/{environment_id}", status_code=204)
async def delete_environment(
    environment_id: UUID,
    auth: AuthContext = Depends(require_user),
) -> None:
    row = await auth.db.fetchrow(
        "DELETE FROM environments WHERE id = $1 RETURNING id",
        environment_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Environment not found")
    await record_event(
        auth.db,
        actor_kind=auth.actor_kind,
        actor_id=auth.actor_id,
        kind="environment.deleted",
        target_kind="environment",
        target_id=environment_id,
        payload={},
    )


# Helper used by the run engine lives in ``api.environments.substitution``
# (kept FastAPI-free). The re-export above preserves any in-repo callers
# that still import from this module.
