"""Route-level tests for /v1/runs using a fake asyncpg connection.

These tests stub out the DB dependency and the engine entry point so we
can exercise the HTTP shape (status codes, schema validation, harness
auth clamp, 50-item cap) without needing a live Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from api.dependencies import AuthContext, require_auth, require_user
from api.main import app
from api.shared.db import get_db

# ---------- in-memory fake asyncpg connection ----------


class FakeConn:
    """Minimal asyncpg-like connection for route-level tests.

    Tests load it with canned responses keyed by a coarse SQL signature
    (substring match), and route handlers exercise the responses via
    ``fetchrow`` / ``fetch`` / ``fetchval`` / ``execute``.
    """

    def __init__(self) -> None:
        self.runs: dict[UUID, dict[str, Any]] = {}
        self.collection_items_count: dict[UUID, int] = {}
        self.events: list[dict[str, Any]] = []

    # asyncpg API surface used by routes
    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "INSERT INTO runs" in sql:
            run_id = uuid4()
            row = {
                "id": run_id,
                "created_by_kind": args[0],
                "created_by_id": args[1],
                "collection_id": args[2],
                "environment_id": args[3],
                "harness_id": args[4],
                "harness_claim": args[5],
                "status": "pending",
                "verdict": None,
                "evidence": {"requests": []},
                "started_at": None,
                "finished_at": None,
                "created_at": datetime.now(tz=UTC),
            }
            self.runs[run_id] = row
            return row
        if "FROM runs" in sql and "WHERE id" in sql:
            return self.runs.get(args[0])
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM runs" in sql:
            return list(self.runs.values())
        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "count(*)" in sql and "collection_items" in sql:
            return self.collection_items_count.get(args[0], 0)
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        if "INSERT INTO events" in sql:
            self.events.append({"sql": sql, "args": args})
        return "OK"


# ---------- fixtures ----------


@pytest.fixture
def fake_conn() -> FakeConn:
    return FakeConn()


@pytest.fixture
def operator_auth(fake_conn: FakeConn) -> AuthContext:
    return AuthContext(
        actor_kind="user",
        actor_id="user-op-1",
        db=fake_conn,  # type: ignore[arg-type]
        email="support@pagehub.io",
        is_admin=True,
    )


@pytest.fixture
def harness_auth(fake_conn: FakeConn) -> AuthContext:
    return AuthContext(
        actor_kind="harness_key",
        actor_id="harness-A",
        db=fake_conn,  # type: ignore[arg-type]
    )


@pytest.fixture
def client_with_operator(operator_auth: AuthContext, fake_conn: FakeConn):
    async def _db():
        yield fake_conn

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_auth] = lambda: operator_auth
    app.dependency_overrides[require_user] = lambda: operator_auth
    try:
        # Use AsyncMock'd background tasks: the engine is invoked
        # via BackgroundTasks.add_task; we don't want it to actually fire
        # against a non-existent DB pool. Replace at import site.
        from api.runs import routes as runs_routes
        original = runs_routes.execute_run
        runs_routes.execute_run = AsyncMock(return_value=None)
        try:
            with TestClient(app) as c:
                yield c
        finally:
            runs_routes.execute_run = original
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client_with_harness(harness_auth: AuthContext, operator_auth: AuthContext, fake_conn: FakeConn):
    async def _db():
        yield fake_conn

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_auth] = lambda: harness_auth
    # require_user rejects harness keys at the dep layer; we mimic that
    # by raising the same 403 the real dep raises.
    from fastapi import HTTPException

    def _operator_only():
        raise HTTPException(status_code=403, detail="Operator-only endpoint")

    app.dependency_overrides[require_user] = _operator_only
    try:
        from api.runs import routes as runs_routes
        original = runs_routes.execute_run
        runs_routes.execute_run = AsyncMock(return_value=None)
        try:
            with TestClient(app) as c:
                yield c
        finally:
            runs_routes.execute_run = original
    finally:
        app.dependency_overrides.clear()


# ---------- tests ----------


def test_post_run_returns_202(client_with_operator, fake_conn: FakeConn) -> None:
    r = client_with_operator.post("/v1/runs", json={"harness_claim": "I fixed X"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending"
    assert body["verdict"] is None
    # engine_error is null pre-execution; the field is part of the
    # contract so we read it explicitly rather than asserting on dict
    # equality (Pydantic emits it as null until the engine sets it).
    assert body["evidence"]["requests"] == []
    assert body["evidence"].get("engine_error") is None
    assert body["harness_claim"] == "I fixed X"
    assert len(fake_conn.runs) == 1


def test_post_run_with_verdict_in_body_is_422(client_with_operator) -> None:
    r = client_with_operator.post(
        "/v1/runs",
        json={"harness_claim": "I did it", "verdict": "passed"},
    )
    assert r.status_code == 422


def test_post_run_with_status_in_body_is_422(client_with_operator) -> None:
    r = client_with_operator.post("/v1/runs", json={"status": "passed"})
    assert r.status_code == 422


def test_post_run_with_evidence_in_body_is_422(client_with_operator) -> None:
    r = client_with_operator.post("/v1/runs", json={"evidence": {"requests": []}})
    assert r.status_code == 422


def test_patch_run_always_403_for_operator(client_with_operator, fake_conn: FakeConn) -> None:
    create = client_with_operator.post("/v1/runs", json={"harness_claim": "x"})
    run_id = create.json()["id"]
    r = client_with_operator.patch(f"/v1/runs/{run_id}", json={"verdict": "passed"})
    assert r.status_code == 403


def test_patch_run_always_403_for_harness(client_with_harness, fake_conn: FakeConn) -> None:
    # Pre-populate a run owned by this harness so the route handler sees it.
    run_id = uuid4()
    fake_conn.runs[run_id] = {
        "id": run_id,
        "created_by_kind": "harness_key",
        "created_by_id": "harness-A",
        "collection_id": None,
        "environment_id": None,
        "harness_id": None,
        "harness_claim": None,
        "status": "pending",
        "verdict": None,
        "evidence": {"requests": []},
        "started_at": None,
        "finished_at": None,
        "created_at": datetime.now(tz=UTC),
    }
    r = client_with_harness.patch(f"/v1/runs/{run_id}", json={})
    assert r.status_code == 403


def test_get_run_as_harness_self_succeeds(client_with_harness, fake_conn: FakeConn) -> None:
    run_id = uuid4()
    fake_conn.runs[run_id] = {
        "id": run_id,
        "created_by_kind": "harness_key",
        "created_by_id": "harness-A",
        "collection_id": None,
        "environment_id": None,
        "harness_id": None,
        "harness_claim": "ours",
        "status": "passed",
        "verdict": "passed",
        "evidence": {"requests": []},
        "started_at": datetime.now(tz=UTC),
        "finished_at": datetime.now(tz=UTC),
        "created_at": datetime.now(tz=UTC),
    }
    r = client_with_harness.get(f"/v1/runs/{run_id}")
    assert r.status_code == 200
    assert r.json()["harness_claim"] == "ours"


def test_get_run_as_harness_other_key_returns_403(client_with_harness, fake_conn: FakeConn) -> None:
    run_id = uuid4()
    fake_conn.runs[run_id] = {
        "id": run_id,
        "created_by_kind": "harness_key",
        "created_by_id": "harness-B-other",
        "collection_id": None,
        "environment_id": None,
        "harness_id": None,
        "harness_claim": "theirs",
        "status": "passed",
        "verdict": "passed",
        "evidence": {"requests": []},
        "started_at": datetime.now(tz=UTC),
        "finished_at": datetime.now(tz=UTC),
        "created_at": datetime.now(tz=UTC),
    }
    r = client_with_harness.get(f"/v1/runs/{run_id}")
    assert r.status_code == 403


def test_list_runs_harness_blocked_by_require_user(client_with_harness) -> None:
    r = client_with_harness.get("/v1/runs")
    assert r.status_code == 403


def test_post_run_collection_over_cap_is_422(client_with_operator, fake_conn: FakeConn) -> None:
    coll_id = uuid4()
    fake_conn.collection_items_count[coll_id] = 51
    r = client_with_operator.post(
        "/v1/runs",
        json={"collection_id": str(coll_id), "harness_claim": "I did it"},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "max items=50" in detail
    assert "51" in detail


def test_post_run_collection_at_cap_is_accepted(client_with_operator, fake_conn: FakeConn) -> None:
    coll_id = uuid4()
    fake_conn.collection_items_count[coll_id] = 50
    r = client_with_operator.post(
        "/v1/runs",
        json={"collection_id": str(coll_id), "harness_claim": "I did it"},
    )
    assert r.status_code == 202


def test_post_run_with_null_collection_skips_cap_check(client_with_operator) -> None:
    r = client_with_operator.post("/v1/runs", json={"harness_claim": "I did it"})
    assert r.status_code == 202
