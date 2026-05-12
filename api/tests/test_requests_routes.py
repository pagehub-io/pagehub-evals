"""Route tests for /v1/requests.

The duplicate-name → 409 path needs the real ``UNIQUE (owner_user_id, name)``
constraint (added by this slice's schema change), so it runs against the CI
Postgres service; it skips cleanly when DATABASE_URL is unreachable.
"""

from __future__ import annotations

from api.tests._db import db_pool, operator_test_client  # noqa: F401


def test_create_request_duplicate_name_409(db_pool) -> None:  # noqa: F811
    body = {"name": "dup-request-name", "method": "GET", "url": "http://example.test/x"}
    with operator_test_client(db_pool) as client:
        first = client.post("/v1/requests", json=body)
        assert first.status_code == 201
        second = client.post("/v1/requests", json=body)
        assert second.status_code == 409
        assert "dup-request-name" in second.json()["detail"]


def test_create_request_distinct_names_ok(db_pool) -> None:  # noqa: F811
    with operator_test_client(db_pool) as client:
        a = client.post(
            "/v1/requests", json={"name": "req-a", "method": "GET", "url": "http://example.test/a"}
        )
        b = client.post(
            "/v1/requests", json={"name": "req-b", "method": "GET", "url": "http://example.test/b"}
        )
        assert a.status_code == 201
        assert b.status_code == 201
