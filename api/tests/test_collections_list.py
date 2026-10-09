"""GET /v1/collections: keyset paging, name and owner filters, items in one
query per page (specs/collections-list-paging.md §6).

Two tiers, as in test_fixtures_import.py:
- validation and auth: no DB. An exploding connection proves each 422/403 is
  decided before any query;
- listing: real Postgres via ``db_pool``, skipped cleanly without one.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient

from api.collections.routes import _decode_cursor, _encode_cursor
from api.dependencies import AuthContext, require_auth, require_user
from api.main import app
from api.schemas import CAPABILITIES
from api.shared.db import get_db
from api.tests._db import db_pool, operator_test_client  # noqa: F401

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


# ---------- tier 1: validation and auth, no DB ----------


class _ExplodingConn:
    async def fetch(self, *a: Any, **k: Any):
        raise AssertionError("DB touched on a rejected list call")

    async def fetchrow(self, *a: Any, **k: Any):
        raise AssertionError("DB touched on a rejected list call")


@contextlib.contextmanager
def _client_with(auth: AuthContext, *, operator: bool = True):
    async def _db():
        yield auth.db

    def _forbidden():
        raise HTTPException(status_code=403, detail="Operator-only endpoint")

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_auth] = lambda: auth
    app.dependency_overrides[require_user] = (lambda: auth) if operator else _forbidden
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


BAD_CURSORS = [
    "not base64 !",  # outside the alphabet
    _b64(b"not json"),
    _b64(b"[1, 2]"),  # JSON, not an object
    _b64(json.dumps({"c": T0.isoformat()}).encode()),  # no "i"
    _b64(json.dumps({"i": str(uuid.uuid4())}).encode()),  # no "c"
    _b64(json.dumps({"c": "yesterday", "i": str(uuid.uuid4())}).encode()),
    _b64(json.dumps({"c": "2026-10-01T12:00:00", "i": str(uuid.uuid4())}).encode()),  # no offset
    _b64(json.dumps({"c": T0.isoformat(), "i": "not-a-uuid"}).encode()),
    _b64(json.dumps({"c": 5, "i": str(uuid.uuid4())}).encode()),
    _b64(json.dumps({"c": T0.isoformat(), "i": 5}).encode()),  # "i" not a string
    _b64(
        json.dumps({"c": "0001-01-01T00:00:00+14:00", "i": str(uuid.uuid4())}).encode()
    ),  # no UTC form
    _b64(json.dumps({"c": "9999-12-31T23:59:59-14:00", "i": str(uuid.uuid4())}).encode()),
]


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=501",
        "&".join(f"name=n{i}" for i in range(51)),
        "name=",
        "name=" + "x" * 201,
        "name=ok&name=" + "x" * 201,
        "owner=someone",
        "name=a%00b",
        *[f"cursor={c}" for c in BAD_CURSORS],
    ],
)
def test_rejected_before_the_db(query: str) -> None:
    auth = AuthContext(
        actor_kind="user",
        actor_id="op-1",
        db=_ExplodingConn(),  # type: ignore[arg-type]
        email="support@pagehub.io",
        is_admin=True,
    )
    with _client_with(auth) as client:
        r = client.get(f"/v1/collections?{query}")
    assert r.status_code == 422, (query, r.text)


def test_bad_cursor_says_so() -> None:
    auth = AuthContext(
        actor_kind="user",
        actor_id="op-1",
        db=_ExplodingConn(),  # type: ignore[arg-type]
        email="support@pagehub.io",
        is_admin=True,
    )
    with _client_with(auth) as client:
        r = client.get(f"/v1/collections?cursor={BAD_CURSORS[1]}")
    assert r.status_code == 422 and r.json() == {"detail": "invalid cursor"}


def test_harness_key_is_refused() -> None:
    auth = AuthContext(actor_kind="harness_key", actor_id="harness-A", db=_ExplodingConn())  # type: ignore[arg-type]
    with _client_with(auth, operator=False) as client:
        assert client.get("/v1/collections?owner=me").status_code == 403


def test_cursor_round_trips() -> None:
    rid = uuid.uuid4()
    at = T0 + timedelta(microseconds=123456)
    assert _decode_cursor(_encode_cursor(at, rid)) == (at, rid)
    assert "=" not in _encode_cursor(at, rid)


def test_health_advertises_the_filters() -> None:
    assert "collections_list_filters" in CAPABILITIES


# ---------- tier 2: real Postgres ----------


def _seed(
    url: str, rows: list[tuple[str, str, datetime]], items: dict[str, int] | None = None
) -> dict[str, str]:
    """Insert collections (owner, name, created_at) and, per name, that many
    items. Returns name+owner -> collection id."""

    async def go() -> dict[str, str]:
        conn = await asyncpg.connect(url)
        try:
            ids: dict[str, str] = {}
            for owner, name, created_at in rows:
                cid = await conn.fetchval(
                    "INSERT INTO collections (owner_user_id, name, created_at, updated_at) "
                    "VALUES ($1, $2, $3, $3) RETURNING id",
                    owner,
                    name,
                    created_at,
                )
                ids[f"{owner}/{name}"] = str(cid)
                for pos in range((items or {}).get(name, 0)):
                    rid = await conn.fetchval(
                        "INSERT INTO requests (owner_user_id, name, method, url) "
                        "VALUES ($1, $2, 'GET', 'http://x') RETURNING id",
                        owner,
                        f"{owner}-{name}-r{pos}",
                    )
                    await conn.execute(
                        "INSERT INTO collection_items (collection_id, request_id, position) VALUES ($1, $2, $3)",
                        cid,
                        rid,
                        pos,
                    )
            return ids
        finally:
            await conn.close()

    return asyncio.run(go())


def _names(body: dict) -> list[str]:
    return [c["name"] for c in body["items"]]


def test_no_parameters_lists_newest_first_with_items(db_pool) -> None:  # noqa: F811
    _seed(
        db_pool,
        [
            ("op-A", "a", T0),
            ("op-A", "b", T0 + timedelta(minutes=1)),
            ("op-B", "c", T0 + timedelta(minutes=2)),
        ],
        items={"a": 0, "b": 1, "c": 3},
    )
    with operator_test_client(db_pool, actor_id="op-A") as client:
        body = client.get("/v1/collections").json()
    assert _names(body) == ["c", "b", "a"]
    assert body["next_cursor"] is None
    by_name = {c["name"]: c for c in body["items"]}
    assert [i["position"] for i in by_name["c"]["items"]] == [0, 1, 2]
    assert all(i["collection_id"] == by_name["c"]["id"] for i in by_name["c"]["items"])
    assert len(by_name["b"]["items"]) == 1 and by_name["a"]["items"] == []


def test_paging_returns_every_row_once_in_order(db_pool) -> None:  # noqa: F811
    # Seven rows; "t1" and "t2" share one created_at, so the id tiebreak decides.
    rows = [("op-A", f"r{i}", T0 + timedelta(minutes=i)) for i in range(5)]
    rows += [("op-A", "t1", T0 + timedelta(minutes=9)), ("op-A", "t2", T0 + timedelta(minutes=9))]
    ids = _seed(db_pool, rows)

    async def expected_order() -> list[str]:
        conn = await asyncpg.connect(db_pool)
        try:
            got = await conn.fetch("SELECT name FROM collections ORDER BY created_at DESC, id DESC")
            return [g["name"] for g in got]
        finally:
            await conn.close()

    want = asyncio.run(expected_order())
    seen: list[str] = []
    pages = 0
    cursor = None
    with operator_test_client(db_pool) as client:
        while True:
            q = "limit=3" + (f"&cursor={cursor}" if cursor else "")
            body = client.get(f"/v1/collections?{q}").json()
            pages += 1
            seen += _names(body)
            cursor = body["next_cursor"]
            if cursor is None:
                break
    assert seen == want and len(seen) == len(ids) == 7
    assert pages == 3
    assert set(want[:2]) == {"t1", "t2"}


def test_exact_page_boundary_ends_with_null_cursor(db_pool) -> None:  # noqa: F811
    _seed(db_pool, [("op-A", f"r{i}", T0 + timedelta(minutes=i)) for i in range(3)])
    with operator_test_client(db_pool) as client:
        body = client.get("/v1/collections?limit=3").json()
    assert len(body["items"]) == 3 and body["next_cursor"] is None


def test_name_filter_matches_exactly_across_owners(db_pool) -> None:  # noqa: F811
    _seed(
        db_pool,
        [
            ("op-A", "a", T0),
            ("op-B", "a", T0 + timedelta(minutes=1)),
            ("op-A", "b", T0 + timedelta(minutes=2)),
            ("op-A", "ab", T0 + timedelta(minutes=3)),
            ("op-A", "c", T0 + timedelta(minutes=4)),
        ],
    )
    with operator_test_client(db_pool) as client:
        body = client.get("/v1/collections?name=a&name=b").json()
    assert _names(body) == ["b", "a", "a"]


def test_owner_me_lists_only_the_callers_rows(db_pool) -> None:  # noqa: F811
    ids = _seed(
        db_pool, [("op-A", "a", T0), ("op-B", "a", T0 + timedelta(minutes=1)), ("op-B", "z", T0)]
    )
    with operator_test_client(db_pool, actor_id="op-A") as client:
        body = client.get("/v1/collections?owner=me").json()
    assert [c["id"] for c in body["items"]] == [ids["op-A/a"]]


def test_name_and_owner_together(db_pool) -> None:  # noqa: F811
    ids = _seed(
        db_pool,
        [
            ("op-A", "a", T0),
            ("op-B", "a", T0 + timedelta(minutes=1)),
            ("op-A", "b", T0),
            ("op-A", "c", T0),
        ],
    )
    with operator_test_client(db_pool, actor_id="op-B") as client:
        body = client.get("/v1/collections?owner=me&name=a&name=b").json()
    assert [c["id"] for c in body["items"]] == [ids["op-B/a"]]


def test_two_queries_per_page(db_pool) -> None:  # noqa: F811
    _seed(
        db_pool,
        [("op-A", n, T0 + timedelta(minutes=i)) for i, n in enumerate("abc")],
        items={"a": 2, "b": 0, "c": 1},
    )
    calls: list[str] = []

    class _Counting:
        def __init__(self, conn: asyncpg.Connection) -> None:
            self._conn = conn

        async def fetch(self, *a: Any, **k: Any):
            calls.append(a[0])
            return await self._conn.fetch(*a, **k)

    async def _db():
        conn = await asyncpg.connect(db_pool)
        try:
            yield _Counting(conn)
        finally:
            await conn.close()

    async def _operator(db=Depends(get_db)) -> AuthContext:
        return AuthContext(
            actor_kind="user", actor_id="op-A", db=db, email="support@pagehub.io", is_admin=True
        )

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_auth] = _operator
    app.dependency_overrides[require_user] = _operator
    try:
        with TestClient(app) as client:
            body = client.get("/v1/collections").json()
    finally:
        app.dependency_overrides.clear()
    assert len(body["items"]) == 3
    assert len(calls) == 2, calls


def test_tied_rows_straddling_a_page_boundary(db_pool) -> None:  # noqa: F811
    # One fixture import gives all its collections the same created_at, so ties
    # are the normal case. With limit=2 every page boundary falls inside a tie:
    # without the id in the cursor comparison, rows would be skipped or repeated.
    _seed(db_pool, [("op-A", f"t{i}", T0) for i in range(5)])
    seen: list[str] = []
    cursor = None
    with operator_test_client(db_pool) as client:
        for _ in range(10):
            q = "limit=2" + (f"&cursor={cursor}" if cursor else "")
            body = client.get(f"/v1/collections?{q}").json()
            seen += _names(body)
            cursor = body["next_cursor"]
            if cursor is None:
                break
    assert sorted(seen) == [f"t{i}" for i in range(5)] and len(seen) == 5
