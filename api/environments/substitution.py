"""Substitution-map loader extracted from the route module.

The engine imports from here (NOT ``api.environments.routes``) so it
never drags any ``fastapi.*`` symbols transitively. Public surface:

- ``load_substitution_map(db, environment_id)`` — merged
  ``{**variables, **decrypted_secrets}`` map.
- ``load_substitution_map_with_secret_keys(db, environment_id)`` —
  same merged map PLUS the set of keys whose VALUES came from the
  secrets half. The engine needs this set to know which substituted
  values to redact out of persisted evidence.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from api.shared.secrets import decrypt


def _reveal_secret_map(stored: dict[str, str]) -> dict[str, str]:
    return {k: decrypt(v) for k, v in stored.items()}


def _coerce_jsonb(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    return value


async def load_substitution_map(db, environment_id: UUID) -> dict[str, str]:
    """Return ``{**variables, **decrypted_secrets}`` for the env id.

    Used by route handlers that only need the merged map.
    """
    merged, _ = await load_substitution_map_with_secret_keys(db, environment_id)
    return merged


async def load_substitution_map_with_secret_keys(
    db,
    environment_id: UUID,
) -> tuple[dict[str, str], set[str]]:
    """Return ``(merged_map, secret_keys)``.

    ``secret_keys`` is the subset of keys whose VALUES came from the
    decrypted-secrets half — used by the engine's evidence redaction
    pass (variable values are not redacted, only secret values).
    """
    row = await db.fetchrow(
        "SELECT variables, secrets FROM environments WHERE id = $1",
        environment_id,
    )
    if row is None:
        return {}, set()
    variables = _coerce_jsonb(row["variables"])
    secrets_stored = _coerce_jsonb(row["secrets"])
    revealed = _reveal_secret_map(secrets_stored)
    merged: dict[str, str] = {**variables, **revealed}
    return merged, set(revealed.keys())
