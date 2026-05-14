"""Tests for GET /v1/fixtures/{name} — byte-identical fixture fetch.

The endpoint serves the raw bytes of ``fixtures/<name>.json`` so a caller
(pagehub-benchmarks, injecting a grader fixture into a build prompt) sees the
same bytes as what's checked into git — no parse-and-reformat round-trip.

Coverage:
- 200 on a known file, body byte-identical to disk, ``Content-Length`` matches.
- 404 on a missing fixture name (otherwise-valid pattern, no file).
- 422 on names that fail the path-param pattern (uppercase, dot, slash, the
  leading-dash and traversal cases) — these never reach the handler.
- 403 for a harness-key actor (operator-only, like /v1/fixtures/import).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.dependencies import AuthContext, require_auth, require_user
from api.main import app
from api.shared.db import get_db

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"
EXAMPLE_FIXTURE = FIXTURES_DIR / "example.json"


class _ExplodingConn:
    """Any DB access is a test failure — this endpoint must not touch the DB."""

    async def fetchrow(self, *a: Any, **k: Any):  # noqa: ANN001
        raise AssertionError("DB touched on GET /v1/fixtures/{name}")

    async def fetchval(self, *a: Any, **k: Any):
        raise AssertionError("DB touched on GET /v1/fixtures/{name}")

    async def execute(self, *a: Any, **k: Any):
        raise AssertionError("DB touched on GET /v1/fixtures/{name}")

    def transaction(self):
        raise AssertionError("DB transaction opened on GET /v1/fixtures/{name}")


@pytest.fixture
def operator_client():
    exploding = _ExplodingConn()
    auth = AuthContext(actor_kind="user", actor_id="op-1", db=exploding, email="support@pagehub.io", is_admin=True)  # type: ignore[arg-type]

    async def _db():
        yield exploding

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_auth] = lambda: auth
    app.dependency_overrides[require_user] = lambda: auth
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def harness_client():
    auth = AuthContext(actor_kind="harness_key", actor_id="harness-A", db=_ExplodingConn())  # type: ignore[arg-type]

    def _operator_only():
        raise HTTPException(status_code=403, detail="Operator-only endpoint")

    async def _db():
        yield auth.db

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_auth] = lambda: auth
    app.dependency_overrides[require_user] = _operator_only
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def test_get_returns_bytes_identical_to_disk(operator_client):
    r = operator_client.get("/v1/fixtures/example")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    expected = EXAMPLE_FIXTURE.read_bytes()
    assert r.content == expected
    # Content-Length is set explicitly so HEAD-style probes / large-buffer
    # callers (pagehub-benchmarks) can preallocate.
    assert int(r.headers["content-length"]) == len(expected)


def test_get_known_eval_chess_fixture(operator_client):
    # Two real fixtures live in this repo — make sure the endpoint reaches
    # them too, not just `example`.
    r = operator_client.get("/v1/fixtures/eval-chess-frontend")
    assert r.status_code == 200
    expected = (FIXTURES_DIR / "eval-chess-frontend.json").read_bytes()
    assert r.content == expected


def test_get_missing_fixture_is_404(operator_client):
    r = operator_client.get("/v1/fixtures/no-such-fixture")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


@pytest.mark.parametrize(
    "name",
    [
        "..",
        "../etc/passwd",
        "-leading-dash",
        "UPPERCASE",
        "with.dot",
        "with_underscore",
        "trailing-",  # actually allowed by the pattern (only leading char is restricted)
    ],
)
def test_get_pattern_rejects_bad_names(operator_client, name):
    if name == "trailing-":
        # Sanity: a trailing dash IS pattern-legal, so this one returns 404
        # (no file with that stem), not 422.
        r = operator_client.get(f"/v1/fixtures/{name}")
        assert r.status_code == 404
        return
    r = operator_client.get(f"/v1/fixtures/{name}")
    # Anything that fails the path-param pattern is rejected by FastAPI's
    # validator before the handler runs (422), and traversal-shaped paths
    # (containing slashes) don't match the route at all (404 from Starlette).
    assert r.status_code in (404, 422), (name, r.status_code, r.text)


def test_get_requires_operator_auth(harness_client):
    # A harness-key actor hits the require_user gate -> 403, never reaches
    # the file read (the _ExplodingConn would assert if it did).
    r = harness_client.get("/v1/fixtures/example")
    assert r.status_code == 403


def test_get_with_no_auth_is_401():
    # require_auth's signature depends on get_db, which FastAPI resolves
    # before the auth check runs. In env=test the asyncpg pool is unbooted,
    # so we stub get_db to a no-op — but leave require_auth alone, so it
    # raises "Missing Authorization or X-Harness-Key header" -> 401.
    exploding = _ExplodingConn()

    async def _db():
        yield exploding

    app.dependency_overrides[get_db] = _db
    try:
        with TestClient(app) as c:
            r = c.get("/v1/fixtures/example")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 401
