"""Tests for POST /v1/fixtures/import.

Two tiers:
- parse-time / auth 422 & 403 cases — no DB touched; a sentinel fake conn
  raises if the handler ever reaches the DB (proves "nothing written");
- transaction / upsert / rollback / owner-scoping — real Postgres via the
  CI service; skip cleanly when DATABASE_URL is unreachable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.dependencies import AuthContext, require_auth, require_user
from api.main import app
from api.shared.db import get_db
from api.tests._db import db_pool  # noqa: F401

CHESS_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "chess.json"


def _load_chess_bundle() -> dict:
    return json.loads(CHESS_FIXTURE.read_text())


# ---------- tier 1: parse-time / auth — no DB ----------


class _ExplodingConn:
    """Any DB access is a test failure — these cases must not reach the DB."""

    async def fetchrow(self, *a: Any, **k: Any):  # noqa: ANN001
        raise AssertionError("DB touched on a parse-time-rejected import")

    async def fetchval(self, *a: Any, **k: Any):
        raise AssertionError("DB touched on a parse-time-rejected import")

    async def execute(self, *a: Any, **k: Any):
        raise AssertionError("DB touched on a parse-time-rejected import")

    def transaction(self):
        raise AssertionError("DB transaction opened on a parse-time-rejected import")


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


def test_bad_eval_kind_rejected_422(operator_client) -> None:
    bundle = {
        "version": 1,
        "requests": [
            {"name": "r", "method": "GET", "url": "http://x", "evaluations": [
                {"name": "e", "kind": "no_such_kind", "config": {}}
            ]},
        ],
    }
    r = operator_client.post("/v1/fixtures/import", json=bundle)
    assert r.status_code == 422


def test_oversize_body_rejected_413_before_parse(operator_client) -> None:
    # > 1 MiB of raw bytes — the body-limit middleware must 413 before the
    # body is parsed (so the _ExplodingConn never trips, and an otherwise
    # well-shaped bundle doesn't even get a chance to be validated).
    from api.fixtures._body_limit import MAX_BUNDLE_BYTES

    huge = json.dumps({"version": 1, "_pad": "x" * (MAX_BUNDLE_BYTES + 1024)})
    r = operator_client.post(
        "/v1/fixtures/import",
        content=huge,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413
    assert "max size" in r.json()["detail"]


def test_bad_eval_config_rejected_422(operator_client) -> None:
    bundle = {
        "version": 1,
        "requests": [
            {"name": "r", "method": "GET", "url": "http://x", "evaluations": [
                {"name": "e", "kind": "status_eq", "config": {}}  # missing `expected`
            ]},
        ],
    }
    r = operator_client.post("/v1/fixtures/import", json=bundle)
    assert r.status_code == 422


def test_non_empty_secret_value_rejected_422(operator_client) -> None:
    bundle = {
        "version": 1,
        "environments": [{"name": "e", "variables": {}, "secrets": {"K": "actual-value"}}],
    }
    r = operator_client.post("/v1/fixtures/import", json=bundle)
    assert r.status_code == 422


def test_unresolved_request_name_in_collection_rejected_422(operator_client) -> None:
    bundle = {
        "version": 1,
        "requests": [{"name": "r1", "method": "GET", "url": "http://x"}],
        "collections": [{"name": "c", "items": ["r1", "does-not-exist"]}],
    }
    r = operator_client.post("/v1/fixtures/import", json=bundle)
    assert r.status_code == 422


def test_duplicate_request_name_in_bundle_rejected_422(operator_client) -> None:
    bundle = {
        "version": 1,
        "requests": [
            {"name": "dup", "method": "GET", "url": "http://x"},
            {"name": "dup", "method": "POST", "url": "http://y"},
        ],
    }
    r = operator_client.post("/v1/fixtures/import", json=bundle)
    assert r.status_code == 422


def test_collection_items_object_form_rejected_422(operator_client) -> None:
    bundle = {
        "version": 1,
        "requests": [{"name": "r1", "method": "GET", "url": "http://x"}],
        "collections": [{"name": "c", "items": [{"request": "r1", "position": 0}]}],
    }
    r = operator_client.post("/v1/fixtures/import", json=bundle)
    assert r.status_code == 422


def test_collection_too_large_rejected_422(operator_client) -> None:
    from api.fixtures.schemas import _MAX_COLLECTION_ITEMS

    names = [f"r{i}" for i in range(_MAX_COLLECTION_ITEMS + 1)]
    bundle = {
        "version": 1,
        "requests": [{"name": n, "method": "GET", "url": "http://x"} for n in names],
        "collections": [{"name": "c", "items": names}],
    }
    r = operator_client.post("/v1/fixtures/import", json=bundle)
    assert r.status_code == 422


def test_too_many_requests_rejected_422(operator_client) -> None:
    from api.fixtures.schemas import _MAX_REQUESTS

    bundle = {
        "version": 1,
        "requests": [
            {"name": f"r{i}", "method": "GET", "url": "http://x"}
            for i in range(_MAX_REQUESTS + 1)
        ],
    }
    r = operator_client.post("/v1/fixtures/import", json=bundle)
    assert r.status_code == 422


def test_too_many_evaluations_rejected_422(operator_client) -> None:
    from api.fixtures.schemas import _MAX_EVALUATIONS_PER_REQUEST

    bundle = {
        "version": 1,
        "requests": [
            {
                "name": "r",
                "method": "GET",
                "url": "http://x",
                "evaluations": [
                    {"name": f"e{i}", "kind": "status_eq", "config": {"expected": 200}}
                    for i in range(_MAX_EVALUATIONS_PER_REQUEST + 1)
                ],
            }
        ],
    }
    r = operator_client.post("/v1/fixtures/import", json=bundle)
    assert r.status_code == 422


def test_unsupported_version_rejected_422(operator_client) -> None:
    r = operator_client.post("/v1/fixtures/import", json={"version": 2})
    assert r.status_code == 422


def test_missing_version_rejected_422(operator_client) -> None:
    r = operator_client.post("/v1/fixtures/import", json={"requests": []})
    assert r.status_code == 422


def test_unknown_top_level_key_rejected_422(operator_client) -> None:
    r = operator_client.post("/v1/fixtures/import", json={"version": 1, "bogus": []})
    assert r.status_code == 422


def test_import_requires_operator_harness_403(harness_client) -> None:
    r = harness_client.post("/v1/fixtures/import", json={"version": 1})
    assert r.status_code == 403


# ---------- tier 2: real Postgres ----------

from api.tests._db import operator_test_client  # noqa: E402


def _import(client, bundle: dict):
    return client.post("/v1/fixtures/import", json=bundle)


def test_import_happy_path_chess(db_pool) -> None:  # noqa: F811
    bundle = _load_chess_bundle()
    with operator_test_client(db_pool) as client:
        r = _import(client, bundle)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["environments"] == {"created": 1, "updated": 0}
        assert body["requests"]["created"] == len(bundle["requests"])
        assert body["requests"]["updated"] == 0
        n_evals = sum(len(req["evaluations"]) for req in bundle["requests"])
        assert body["evaluations"] == {"created": n_evals, "updated": 0}
        assert body["collections"] == {"created": 2, "updated": 0}


def test_reimport_is_idempotent(db_pool) -> None:  # noqa: F811
    bundle = _load_chess_bundle()
    with operator_test_client(db_pool) as client:
        assert _import(client, bundle).status_code == 200
        r2 = _import(client, bundle)
        assert r2.status_code == 200
        body = r2.json()
        for kind in ("environments", "requests", "evaluations", "collections"):
            assert body[kind]["created"] == 0, (kind, body[kind])


def test_empty_bundle_is_noop(db_pool) -> None:  # noqa: F811
    with operator_test_client(db_pool) as client:
        r = _import(client, {"version": 1})
        assert r.status_code == 200
        body = r.json()
        for kind in ("environments", "requests", "evaluations", "collections"):
            assert body[kind] == {"created": 0, "updated": 0}
        assert body["warnings"] == ["fixture contained no resources"]


def test_volatile_fields_stripped_with_warning(db_pool) -> None:  # noqa: F811
    bundle = {
        "version": 1,
        "requests": [
            {"name": "r", "method": "GET", "url": "http://x", "id": "ignore-me", "created_at": "2020-01-01"}
        ],
    }
    with operator_test_client(db_pool) as client:
        r = _import(client, bundle)
        assert r.status_code == 200
        warnings = r.json()["warnings"]
        assert any("id" in w for w in warnings)
        assert any("created_at" in w for w in warnings)


def test_all_or_nothing_rollback(db_pool) -> None:  # noqa: F811
    bundle = {
        "version": 1,
        "requests": [
            {"name": "good-req-1", "method": "GET", "url": "http://x"},
            {
                "name": "bad-req-2",
                "method": "GET",
                "url": "http://y",
                "evaluations": [{"name": "e", "kind": "status_eq", "config": {}}],  # bad config
            },
        ],
    }
    with operator_test_client(db_pool) as client:
        r = _import(client, bundle)
        assert r.status_code == 422
        listing = client.get("/v1/requests").json()["items"]
        assert all(req["name"] not in ("good-req-1", "bad-req-2") for req in listing)


def test_owner_scoping(db_pool) -> None:  # noqa: F811
    coll_bundle = {
        "version": 1,
        "requests": [{"name": "shared-r", "method": "GET", "url": "http://x"}],
        "collections": [{"name": "C", "items": ["shared-r"]}],
    }
    with operator_test_client(db_pool, actor_id="owner-A") as client_a:
        a1 = _import(client_a, coll_bundle)
        assert a1.status_code == 200
        assert a1.json()["collections"]["created"] == 1
    # Owner B imports the same names — they upsert into B's OWN rows (UNIQUE is
    # per (owner_user_id, name)), so B also sees `created: 1`, never an update
    # of A's row. (The data from A persists — db_pool truncates once per test.)
    with operator_test_client(db_pool, actor_id="owner-B") as client_b:
        b1 = _import(client_b, coll_bundle)
        assert b1.status_code == 200
        assert b1.json()["collections"]["created"] == 1
        assert b1.json()["requests"]["created"] == 1
        # Two distinct collections named C now exist (one per owner).
        names = [c["name"] for c in client_b.get("/v1/collections").json()["items"]]
        assert names.count("C") == 2


def test_empty_secret_value_creates_unset_placeholder(db_pool) -> None:  # noqa: F811
    bundle = {
        "version": 1,
        "environments": [
            {"name": "env-with-key", "variables": {"V": "x"}, "secrets": {"API_KEY": ""}}
        ],
    }
    with operator_test_client(db_pool) as client:
        r = client.post("/v1/fixtures/import", json=bundle)
        assert r.status_code == 200
        assert r.json()["environments"] == {"created": 1, "updated": 0}
        envs = client.get("/v1/environments").json()["items"]
        env = next(e for e in envs if e["name"] == "env-with-key")
        assert "API_KEY" in env["secrets"]


def test_secrets_merge_preserves_existing_ciphertext(db_pool) -> None:  # noqa: F811
    with operator_test_client(db_pool) as client:
        created = client.post(
            "/v1/environments",
            json={"name": "env-merge", "variables": {}, "secrets": {"TOKEN": "real-value"}},
        )
        assert created.status_code == 201
        env_id = created.json()["id"]
        bundle = {
            "version": 1,
            "environments": [
                {"name": "env-merge", "variables": {"V": "1"}, "secrets": {"TOKEN": "", "NEW_KEY": ""}}
            ],
        }
        assert client.post("/v1/fixtures/import", json=bundle).status_code == 200
        revealed = client.get(f"/v1/environments/{env_id}?reveal_secrets=true").json()
        assert revealed["secrets"]["TOKEN"] == "real-value"
        assert revealed["secrets"]["NEW_KEY"] == ""


def test_replace_semantics_drops_stale_evals(db_pool) -> None:  # noqa: F811
    b1 = {
        "version": 1,
        "requests": [
            {"name": "r", "method": "GET", "url": "http://x", "evaluations": [
                {"name": "e1", "kind": "status_eq", "config": {"expected": 200}},
                {"name": "e2", "kind": "status_eq", "config": {"expected": 201}},
            ]}
        ],
    }
    b2 = {
        "version": 1,
        "requests": [
            {"name": "r", "method": "GET", "url": "http://x", "evaluations": [
                {"name": "e1", "kind": "status_eq", "config": {"expected": 200}},
            ]}
        ],
    }
    with operator_test_client(db_pool) as client:
        assert _import(client, b1).status_code == 200
        assert _import(client, b2).status_code == 200
        rid = next(req["id"] for req in client.get("/v1/requests").json()["items"] if req["name"] == "r")
        evals = client.get(f"/v1/requests/{rid}/evaluations").json()["items"]
        assert [e["name"] for e in evals] == ["e1"]


def test_export_then_import_unrelated_owner(db_pool) -> None:  # noqa: F811
    bundle = {
        "version": 1,
        "requests": [{"name": "exp-r", "method": "GET", "url": "http://x"}],
        "collections": [{"name": "exp-c", "items": ["exp-r"]}],
    }
    with operator_test_client(db_pool, actor_id="owner-A") as client_a:
        assert _import(client_a, bundle).status_code == 200
        cid = next(c["id"] for c in client_a.get("/v1/collections").json()["items"] if c["name"] == "exp-c")
        exported = client_a.get(f"/v1/collections/{cid}/export")
        assert exported.status_code == 200
        ex = exported.json()
        assert ex["environments"] == []
    with operator_test_client(db_pool, actor_id="owner-B") as client_b:
        r = client_b.post("/v1/fixtures/import", json=ex)
        assert r.status_code == 200
        assert r.json()["collections"]["created"] == 1
        # B's collection's item resolves by name.
        cid_b = next(c["id"] for c in client_b.get("/v1/collections").json()["items"]
                     if c["name"] == "exp-c" and c["id"] != cid)
        coll = client_b.get(f"/v1/collections/{cid_b}").json()
        assert len(coll["items"]) == 1
