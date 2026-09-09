"""``execute_run`` end to end through the fake pool: per-request timeout,
transient retry, per-attempt ceiling, blocked/skipped items, the RUN_ID
builtin, the run budget, and evidence compatibility."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from api.runs import engine as engine_mod
from api.runs.schemas import RunEvidence, RunRequestResult
from api.tests.test_runs_engine_concurrency import (
    _FakeConn,
    _FakePool,
    _initial_read_row,
    _patch_pool,
)


class _Conn(_FakeConn):
    def __init__(self, items: list[dict[str, Any]], evals: list[dict[str, Any]]):
        super().__init__(rows_for_select={"initial_read": _initial_read_row(collection_id=uuid4())}, starting_status="pending")
        self._items = items
        self._evals = evals

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetches.append((sql, args))
        if "FROM collection_items ci" in sql:
            return self._items
        if "FROM evaluations" in sql:
            return self._evals
        return []


def _item(name: str = "r1", url: str = "http://target.test/x", timeout_ms: int | None = None, rid=None):
    return {
        "position": 0,
        "request_id": rid or uuid4(),
        "name": name,
        "method": "GET",
        "url": url,
        "headers": {},
        "body": None,
        "capture": {},
        "timeout_ms": timeout_ms,
    }


def _eval(rid, kind="status_eq", config=None):
    return {"id": uuid4(), "request_id": rid, "name": "e", "kind": kind, "config": config or {"expected": 200}}


def _evidence(conn: _FakeConn) -> dict:
    for sql, args in conn.fetchrows:
        if "SET status = $1, verdict = $2" in sql:
            return {"status": args[0], "verdict": args[1], "evidence": json.loads(args[2])}
    raise AssertionError("no terminal write")


class _Script:
    """Class-level ``httpx.AsyncClient.request`` replacement driven by a script."""

    def __init__(self, steps: list[Any]):
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, client, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        step = self.steps.pop(0) if self.steps else self.steps_default()
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return await step()
        status, body = step
        return httpx.Response(status, json=body)

    def steps_default(self):
        return (200, {"ok": True})


async def _run(items, evals, script: _Script | None, **patches):
    conn = _Conn(items, evals)
    pool = _FakePool(conn)
    ctx = [patch.object(engine_mod, k, v) for k, v in patches.items()]
    with _patch_pool(pool):
        for c in ctx:
            c.start()
        try:
            if script is not None:
                async def _fake_request(self, method, url, **kw):  # noqa: ANN001
                    return await script(self, method, url, **kw)

                with patch.object(httpx.AsyncClient, "request", _fake_request):
                    await engine_mod.execute_run(uuid4())
            else:
                await engine_mod.execute_run(uuid4())
        finally:
            for c in ctx:
                c.stop()
    return conn


@pytest.mark.asyncio
async def test_timeout_ms_threaded_to_httpx_and_default() -> None:
    rid1, rid2 = uuid4(), uuid4()
    items = [_item("a", timeout_ms=15000, rid=rid1), _item("b", rid=rid2) | {"position": 1}]
    script = _Script([(200, {}), (200, {})])
    await _run(items, [_eval(rid1), _eval(rid2)], script)
    t1, t2 = script.calls[0]["timeout"], script.calls[1]["timeout"]
    assert t1.read == 15.0 and t1.connect == 10.0
    assert t2.read == 10.0 and t2.connect == 10.0


@pytest.mark.asyncio
async def test_503_then_200_is_two_attempts() -> None:
    rid = uuid4()
    script = _Script([(503, {}), (200, {})])
    conn = await _run([_item(rid=rid)], [_eval(rid)], script, _TRANSIENT_BASE_DELAY_MS=1)
    ev = _evidence(conn)
    assert ev["status"] == "passed"
    assert ev["evidence"]["requests"][0]["attempts"] == 2


@pytest.mark.asyncio
async def test_passing_429_is_not_retried() -> None:
    rid = uuid4()
    script = _Script([(429, {}), (200, {})])
    conn = await _run([_item(rid=rid)], [_eval(rid, config={"expected": 429})], script, _TRANSIENT_BASE_DELAY_MS=1)
    ev = _evidence(conn)
    assert ev["status"] == "passed" and ev["evidence"]["requests"][0]["attempts"] == 1
    assert len(script.calls) == 1


@pytest.mark.asyncio
async def test_read_timeout_then_200_is_retried() -> None:
    rid = uuid4()
    script = _Script([httpx.ReadTimeout("slow"), (200, {})])
    conn = await _run([_item(rid=rid, timeout_ms=1500)], [_eval(rid)], script, _TRANSIENT_BASE_DELAY_MS=1)
    ev = _evidence(conn)
    assert ev["status"] == "passed" and ev["evidence"]["requests"][0]["attempts"] == 2


@pytest.mark.asyncio
async def test_four_timeouts_exhaust_attempts_with_suffix() -> None:
    rid = uuid4()
    script = _Script([httpx.ReadTimeout("slow")] * 4)
    conn = await _run([_item(rid=rid, timeout_ms=1500)], [_eval(rid)], script, _TRANSIENT_BASE_DELAY_MS=1)
    ev = _evidence(conn)
    r = ev["evidence"]["requests"][0]
    assert ev["status"] == "error" and r["attempts"] == 4
    assert r["transport_error"] == "ReadTimeout: slow (timeout_ms=1500)"
    assert len(script.calls) == 4


@pytest.mark.asyncio
async def test_trickling_response_hits_attempt_ceiling() -> None:
    rid = uuid4()

    async def never_finishes():
        await asyncio.sleep(5)

    script = _Script([never_finishes] * 4)
    conn = await _run(
        [_item(rid=rid, timeout_ms=100)], [_eval(rid)], script,
        _TRANSIENT_BASE_DELAY_MS=1, _ATTEMPT_CEILING_EXTRA_SECONDS=0.02,
    )
    r = _evidence(conn)["evidence"]["requests"][0]
    assert r["attempts"] == 4 and r["transport_error"].startswith("TimeoutError: attempt ceiling")


@pytest.mark.asyncio
async def test_unrendered_base_url_is_not_retried() -> None:
    rid = uuid4()
    conn = await _run([_item(rid=rid, url="{{BASE_URL}}/x")], [_eval(rid)], None, _TRANSIENT_BASE_DELAY_MS=1)
    r = _evidence(conn)["evidence"]["requests"][0]
    assert r["attempts"] == 1 and r["transport_error"].startswith("UnsupportedProtocol")
    assert "BASE_URL" in r["substitution_missed"]


@pytest.mark.asyncio
async def test_local_protocol_error_is_not_retried() -> None:
    rid = uuid4()
    script = _Script([httpx.LocalProtocolError("Illegal header value b' lead'"), (200, {})])
    conn = await _run([_item(rid=rid)], [_eval(rid)], script, _TRANSIENT_BASE_DELAY_MS=1)
    r = _evidence(conn)["evidence"]["requests"][0]
    assert r["attempts"] == 1 and r["transport_error"].startswith("LocalProtocolError")
    assert len(script.calls) == 1


@pytest.mark.asyncio
async def test_config_substitution_through_engine_records_raw_and_misses() -> None:
    rid = uuid4()
    script = _Script([(200, {"id": "u-1", "msg": "hello abc"})])
    evals = [
        _eval(rid, "json_path_eq", {"path": "$.id", "expected": "{{NOPE}}"}),
        _eval(rid, "body_contains", {"needle": "{{RUN_ID}}"}) | {"id": uuid4()},
    ]
    conn = await _run([_item(rid=rid)], evals, script)
    r = _evidence(conn)["evidence"]["requests"][0]
    assert r["substitution_missed"] == ["NOPE"]
    eq, contains = r["evaluations"]
    assert eq["detail"]["expected_raw"] == "{{NOPE}}" and eq["detail"]["expected"] == "{{NOPE}}"
    assert contains["detail"]["needle_raw"] == "{{RUN_ID}}" and len(contains["detail"]["needle"]) == 12


@pytest.mark.asyncio
async def test_blocked_item_has_zero_attempts_and_no_retry() -> None:
    rid = uuid4()
    script = _Script([(200, {})])
    conn = await _run([_item(rid=rid)], [_eval(rid)], script, is_blocked_host=lambda url, env: (True, "test-block"))
    r = _evidence(conn)["evidence"]["requests"][0]
    assert r["attempts"] == 0 and r["transport_error"].startswith("blocked:")
    assert script.calls == []


@pytest.mark.asyncio
async def test_run_id_builtin_is_rendered() -> None:
    rid = uuid4()
    script = _Script([(200, {})])
    await _run([_item(rid=rid, url="http://t.test/{{RUN_ID}}")], [_eval(rid)], script)
    tail = script.calls[0]["url"].rsplit("/", 1)[1]
    assert len(tail) == 12 and all(c in "0123456789abcdef" for c in tail)


@pytest.mark.asyncio
async def test_budget_skips_remaining_items_and_sets_engine_error() -> None:
    rid1, rid2 = uuid4(), uuid4()

    async def slow():
        await asyncio.sleep(0.15)
        return httpx.Response(200, json={})

    script = _Script([slow, (200, {})])
    items = [_item("a", rid=rid1), _item("b", rid=rid2, url="http://t.test/{{RUN_ID}}") | {"position": 1}]
    conn = await _run(items, [_eval(rid1), _eval(rid2)], script, RUN_BUDGET_SECONDS=0.05)
    ev = _evidence(conn)
    assert ev["status"] == "error" and ev["evidence"]["engine_error"] == "run budget exceeded"
    reqs = ev["evidence"]["requests"]
    assert reqs[0]["attempts"] == 1 and reqs[0]["passed"] is True
    assert reqs[1] == reqs[1] | {
        "response_status": 0, "latency_ms": 0, "response_headers": {}, "response_body_excerpt": None,
        "evaluations": [], "captured": [], "substitution_missed": [], "passed": False, "attempts": 0,
        "transport_error": "skipped: run budget exceeded",
    }
    assert "{{" not in reqs[1]["url"]
    assert len(script.calls) == 1


def test_pre_change_evidence_without_attempts_validates() -> None:
    old = {
        "request_id": str(uuid4()), "request_name": "r", "method": "GET", "url": "http://x",
        "response_status": 200, "latency_ms": 1, "passed": True,
    }
    assert RunRequestResult.model_validate(old).attempts == 1
    assert RunEvidence.model_validate({"requests": [old]}).requests[0].attempts == 1
    with pytest.raises(ValidationError):
        RunRequestResult.model_validate(old | {"attempts": -1})
