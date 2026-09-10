"""DB-backed: ``timeout_ms`` through fixture import and export, the export
omitting the key when unset, and the round-trip identity over every
checked-in bundle plus a timeout-bearing one."""

from __future__ import annotations

import glob
import json

import pytest

from api.fixtures.schemas import normalize_for_roundtrip
from api.tests._db import db_pool, operator_test_client  # noqa: F401

TIMEOUT_BUNDLE = {
    "version": 1,
    "environments": [],
    "requests": [
        {"name": "t-slow", "method": "GET", "url": "http://t.test/slow", "timeout_ms": 15000,
         "evaluations": [{"name": "ok", "kind": "json_path_exists", "config": {"path": "$.ok"}}]},
        {"name": "t-default", "method": "GET", "url": "http://t.test/fast", "body": None,
         "evaluations": [{"name": "ok", "kind": "json_path_cmp", "config": {"path": "$.n", "op": "gte", "expected": 1}}]},
    ],
    "collections": [{"name": "t-coll", "items": ["t-slow", "t-default"]}],
}


def _import_and_export(client, bundle: dict) -> dict:
    r = client.post("/v1/fixtures/import", json=bundle)
    assert r.status_code == 200, r.text
    cols = client.get("/v1/collections").json()["items"]
    by_name = {c["name"]: c["id"] for c in cols}
    out = {}
    for c in bundle["collections"]:
        e = client.get(f"/v1/collections/{by_name[c['name']]}/export")
        assert e.status_code == 200, e.text
        out[c["name"]] = e.json()
    return out


def test_timeout_ms_round_trips_and_absent_key_stays_absent(db_pool) -> None:  # noqa: F811
    with operator_test_client(db_pool) as client:
        exports = _import_and_export(client, TIMEOUT_BUNDLE)
        reqs = {r["name"]: r for r in exports["t-coll"]["requests"]}
        assert reqs["t-slow"]["timeout_ms"] == 15000
        assert "timeout_ms" not in reqs["t-default"]
        listed = {r["name"]: r for r in client.get("/v1/requests").json()["items"]}
        assert listed["t-slow"]["timeout_ms"] == 15000 and listed["t-default"]["timeout_ms"] is None
        # removing the key resets the row
        bundle2 = json.loads(json.dumps(TIMEOUT_BUNDLE))
        del bundle2["requests"][0]["timeout_ms"]
        exports2 = _import_and_export(client, bundle2)
        assert "timeout_ms" not in {r["name"]: r for r in exports2["t-coll"]["requests"]}["t-slow"]


def test_create_request_route_returns_timeout_ms(db_pool) -> None:  # noqa: F811
    with operator_test_client(db_pool) as client:
        r = client.post("/v1/requests", json={"name": "with-t", "method": "GET", "url": "http://t.test", "timeout_ms": 250})
        assert r.status_code == 201, r.text
        assert r.json()["timeout_ms"] == 250
        g = client.get(f"/v1/requests/{r.json()['id']}")
        assert g.json()["timeout_ms"] == 250
        bad = client.post("/v1/requests", json={"name": "bad-t", "method": "GET", "url": "http://t.test", "timeout_ms": 5})
        assert bad.status_code == 422


def test_environment_with_run_id_is_422_on_import(db_pool) -> None:  # noqa: F811
    with operator_test_client(db_pool) as client:
        r = client.post("/v1/fixtures/import", json={"version": 1, "environments": [{"name": "e", "variables": {"RUN_ID": "x"}}]})
        assert r.status_code == 422
        r = client.post("/v1/environments", json={"name": "e2", "secrets": {"RUN_ID": "v"}})
        assert r.status_code == 422


@pytest.mark.parametrize("path", sorted(glob.glob("fixtures/*.json")) + ["<timeout-bundle>"])
def test_export_import_export_identity(db_pool, path: str) -> None:  # noqa: F811
    bundle = TIMEOUT_BUNDLE if path == "<timeout-bundle>" else json.load(open(path))
    with operator_test_client(db_pool) as client:
        first = _import_and_export(client, bundle)
        for name, export_a in first.items():
            re_import = client.post("/v1/fixtures/import", json=export_a)
            assert re_import.status_code == 200, re_import.text
            cols = {c["name"]: c["id"] for c in client.get("/v1/collections").json()["items"]}
            export_b = client.get(f"/v1/collections/{cols[name]}/export").json()
            assert normalize_for_roundtrip(export_a) == normalize_for_roundtrip(export_b)
            assert json.dumps(export_a, sort_keys=True) == json.dumps(export_b, sort_keys=True)
            if path != "<timeout-bundle>":
                assert all("timeout_ms" not in r for r in export_a["requests"])
