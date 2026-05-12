"""Shared helpers for tests that need a real Postgres.

CI runs ``postgres:17`` for the ``backend-tests`` job and points
``DATABASE_URL`` at it (``.github/workflows/ci.yml``; conftest preserves an
externally-set ``DATABASE_URL``). Locally a developer may not have Postgres
up — these helpers connect lazily and **skip** the test cleanly (never
error) when the DB is unreachable.

Loop discipline: ``TestClient`` runs the ASGI app inside its own
anyio blocking-portal event loop, distinct from pytest-asyncio's. asyncpg
connections are bound to the loop that created them, so a connection (or
pool) made in the pytest loop cannot be used by a route handler running in
the TestClient loop. The ``operator_test_client`` helper therefore opens a
**fresh ``asyncpg.connect()`` per request** (created inside the handler →
the TestClient loop). The ``db_pool`` fixture's own connection is only used
for schema-apply + truncate, in the pytest loop, before the client starts.

Usage::

    import pytest
    from api.tests._db import db_pool, operator_test_client  # noqa: F401

    def test_x(db_pool):
        with operator_test_client(db_pool) as client:
            ...
"""

from __future__ import annotations

import contextlib
import os

import asyncpg
import pytest
import pytest_asyncio
from fastapi import Depends

from api.dependencies import AuthContext, require_auth, require_user
from api.main import app
from api.shared.db import get_db
from api.shared.schema import _SCHEMA_PATH

_TABLES_TO_TRUNCATE = (
    "events",
    "collection_items",
    "evaluations",
    "collections",
    "requests",
    "environments",
    "runs",
    "harness_keys",
)


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "")


def _is_real_url(url: str) -> bool:
    # The conftest default ('postgresql://test:test@localhost/test') is bogus.
    return bool(url) and "localhost/test" not in url and "@localhost/test" not in url


async def _can_connect(url: str) -> bool:
    try:
        conn = await asyncpg.connect(url, timeout=3)
    except Exception:  # noqa: BLE001
        return False
    await conn.close()
    return True


@pytest_asyncio.fixture
async def db_pool():
    """Yields the ``DATABASE_URL`` (a str) after applying schema + truncating.

    Named ``db_pool`` for historical parity; it is *not* an asyncpg pool —
    handing one across event loops is exactly the bug we avoid. Tests use
    ``operator_test_client(db_pool)`` which opens its own connections.
    """
    url = _database_url()
    if not _is_real_url(url) or not await _can_connect(url):
        pytest.skip("no reachable Postgres (DATABASE_URL) — DB-backed test skipped")
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(_SCHEMA_PATH.read_text())
        await conn.execute(
            "TRUNCATE " + ", ".join(_TABLES_TO_TRUNCATE) + " RESTART IDENTITY CASCADE"
        )
    finally:
        await conn.close()
    yield url


async def truncate_all(url: str) -> None:
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(
            "TRUNCATE " + ", ".join(_TABLES_TO_TRUNCATE) + " RESTART IDENTITY CASCADE"
        )
    finally:
        await conn.close()


@contextlib.contextmanager
def operator_test_client(database_url: str, *, actor_id: str = "test-operator"):
    """A ``TestClient`` whose ``require_user`` / ``require_auth`` resolve to a
    fresh operator ``AuthContext`` with a per-request DB connection — exactly
    how a route sees auth + DB in prod, just without the JWT round-trip and
    without a pool (see loop-discipline note above).

    Yields the ``TestClient``. Clears ``app.dependency_overrides`` on exit.
    """
    from fastapi.testclient import TestClient

    async def _db():
        conn = await asyncpg.connect(database_url)
        try:
            yield conn
        finally:
            await conn.close()

    async def _operator(db: asyncpg.Connection = Depends(get_db)) -> AuthContext:
        return AuthContext(
            actor_kind="user",
            actor_id=actor_id,
            db=db,
            email="support@pagehub.io",
            is_admin=True,
        )

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_auth] = _operator
    app.dependency_overrides[require_user] = _operator
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
