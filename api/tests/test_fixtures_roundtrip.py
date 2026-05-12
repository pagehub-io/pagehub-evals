"""Round-trip invariant: import(chess.json) -> export each collection ->
import -> export -> assert ``normalize_for_roundtrip(a) == normalize_for_roundtrip(b)``.

Drives ``fixtures.engine`` against a real Postgres (the import transaction
logic can't be faked); skips cleanly when DATABASE_URL is unreachable.
"""

from __future__ import annotations

import json
from pathlib import Path

from api.fixtures.schemas import normalize_for_roundtrip
from api.tests._db import db_pool, operator_test_client  # noqa: F401

CHESS_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "chess.json"


def _collection_ids_by_name(client) -> dict[str, str]:
    items = client.get("/v1/collections").json()["items"]
    return {c["name"]: c["id"] for c in items}


def test_chess_fixture_roundtrips(db_pool) -> None:  # noqa: F811
    bundle = json.loads(CHESS_FIXTURE.read_text())
    with operator_test_client(db_pool) as client:
        r = client.post("/v1/fixtures/import", json=bundle)
        assert r.status_code == 200, r.text

        by_name = _collection_ids_by_name(client)
        assert {"chess-legality", "chess-playable-game"} <= set(by_name)

        for coll_name in ("chess-legality", "chess-playable-game"):
            cid = by_name[coll_name]
            export_a = client.get(f"/v1/collections/{cid}/export")
            assert export_a.status_code == 200, export_a.text
            a = export_a.json()
            assert a["environments"] == []
            assert a["collections"][0]["name"] == coll_name

            reimport = client.post("/v1/fixtures/import", json=a)
            assert reimport.status_code == 200, reimport.text
            # The collection id is stable (upsert by name), so re-export the same id.
            export_b = client.get(f"/v1/collections/{cid}/export")
            assert export_b.status_code == 200
            b = export_b.json()

            assert normalize_for_roundtrip(a) == normalize_for_roundtrip(b)
            # Export emits requests in first-referenced order with dense
            # positions, so raw export bytes are stable too (modulo dict order).
            assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_empty_collection_roundtrips(db_pool) -> None:  # noqa: F811
    with operator_test_client(db_pool) as client:
        created = client.post("/v1/collections", json={"name": "empty-coll", "description": "d"})
        assert created.status_code == 201
        cid = created.json()["id"]
        a = client.get(f"/v1/collections/{cid}/export").json()
        assert a["requests"] == []
        assert a["collections"][0]["items"] == []
        client.post("/v1/fixtures/import", json=a)
        b = client.get(f"/v1/collections/{cid}/export").json()
        assert normalize_for_roundtrip(a) == normalize_for_roundtrip(b)


def test_shared_request_appears_once_per_export(db_pool) -> None:  # noqa: F811
    bundle = {
        "version": 1,
        "requests": [{"name": "shared", "method": "GET", "url": "http://x"}],
        "collections": [
            {"name": "c-one", "items": ["shared"]},
            {"name": "c-two", "items": ["shared", "shared"]},
        ],
    }
    with operator_test_client(db_pool) as client:
        assert client.post("/v1/fixtures/import", json=bundle).status_code == 200
        by_name = _collection_ids_by_name(client)
        e1 = client.get(f"/v1/collections/{by_name['c-one']}/export").json()
        e2 = client.get(f"/v1/collections/{by_name['c-two']}/export").json()
        assert [r["name"] for r in e1["requests"]] == ["shared"]
        assert [r["name"] for r in e2["requests"]] == ["shared"]
        assert e2["collections"][0]["items"] == ["shared", "shared"]
