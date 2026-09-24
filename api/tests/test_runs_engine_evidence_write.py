"""The terminal evidence write must land, whatever the response bodies held.

Regression: a plain ``GET`` of a WebP image decoded to text full of NUL
characters; ``json.dumps`` rendered them as ``\\u0000``, Postgres ``jsonb``
refused the terminal UPDATE (``UntranslatableCharacterError: unsupported
Unicode escape sequence``), the engine only logged it, and the run stayed
``running`` forever — so a runner polling it hung until its own ceiling.

The DB-backed tests here use the real Postgres behind ``DATABASE_URL`` (the
refusal is Postgres's, a fake pool cannot reproduce it) and skip cleanly
without one, like every other DB-backed test (``api/tests/_db.py``).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from api.runs import engine as engine_mod
from api.runs.schemas import RunEvidence, RunRequestResult
from api.tests._db import db_pool  # noqa: F401  (pytest fixture)
from api.tests.test_engine_retry_budget import _Conn, _eval, _item
from api.tests.test_runs_engine_concurrency import _FakePool, _patch_pool

# The first 64 bytes, verbatim, of the WebP thumbnail whose plain GET wedged a
# run on a local instance (2026-09-23): RIFF header, VP8X chunk, start of the
# ICCP chunk. 20 of the 64 bytes are NUL.
_REAL_WEBP_PREFIX = (
    b"RIFF 8\x00\x00WEBPVP8X\n\x00\x00\x00 \x00\x00\x00\x1b\x02\x00\x1b\x02\x00IC"
    b"CP\xc8\x01\x00\x00\x00\x00\x01\xc8\x00\x00\x00\x00\x040\x00\x00mntrRGB XYZ \x07\xe0"
)


# ---------- helpers ----------


async def _seed_run(
    url: str,
    *,
    evals: list[tuple[str, dict[str, Any]]],
) -> UUID:
    """Insert a one-request collection + a pending run; return the run id."""
    conn = await asyncpg.connect(url)
    try:
        coll_id = await conn.fetchval(
            "INSERT INTO collections (owner_user_id, name) VALUES ('t', 'c') RETURNING id"
        )
        req_id = await conn.fetchval(
            """
            INSERT INTO requests (owner_user_id, name, method, url)
            VALUES ('t', 'thumbnail', 'GET', 'http://target.test/thumbs/loop.webp')
            RETURNING id
            """
        )
        await conn.execute(
            "INSERT INTO collection_items (collection_id, request_id, position) VALUES ($1, $2, 0)",
            coll_id,
            req_id,
        )
        for kind, config in evals:
            await conn.execute(
                "INSERT INTO evaluations (request_id, name, kind, config) VALUES ($1, $2, $2, $3::jsonb)",
                req_id,
                kind,
                json.dumps(config),
            )
        return await conn.fetchval(
            """
            INSERT INTO runs (created_by_kind, created_by_id, collection_id)
            VALUES ('user', 't', $1) RETURNING id
            """,
            coll_id,
        )
    finally:
        await conn.close()


async def _execute_against(database_url: str, run_id: UUID, response: httpx.Response) -> None:
    """Run the real engine against the real DB; the target answers ``response``."""

    async def _fake_request(self, method, url, **kw):  # noqa: ANN001
        return response

    pool = await asyncpg.create_pool(
        dsn=database_url, min_size=1, max_size=2, statement_cache_size=0
    )
    try:
        with (
            patch.object(engine_mod, "get_pool", return_value=pool),
            patch.object(httpx.AsyncClient, "request", _fake_request),
        ):
            await engine_mod.execute_run(run_id)
    finally:
        await pool.close()


async def _load_run(url: str, run_id: UUID) -> dict[str, Any]:
    conn = await asyncpg.connect(url)
    try:
        row = await conn.fetchrow(
            "SELECT status, verdict, evidence FROM runs WHERE id = $1", run_id
        )
        completed = await conn.fetch(
            "SELECT payload FROM events WHERE target_id = $1 AND kind = 'run.completed'",
            run_id,
        )
    finally:
        await conn.close()
    return {
        "status": row["status"],
        "verdict": row["verdict"],
        "evidence": json.loads(row["evidence"]),
        "completed_events": [json.loads(e["payload"]) for e in completed],
    }


# ---------- real Postgres ----------


@pytest.mark.asyncio
async def test_binary_webp_body_reaches_terminal_status_with_evidence(db_pool: str) -> None:  # noqa: F811
    run_id = await _seed_run(db_pool, evals=[("status_eq", {"expected": 200})])
    response = httpx.Response(
        200, headers={"content-type": "image/webp"}, content=_REAL_WEBP_PREFIX
    )

    await _execute_against(db_pool, run_id, response)

    run = await _load_run(db_pool, run_id)
    assert run["status"] == "passed", run
    assert run["verdict"] == "passed"
    assert len(run["completed_events"]) == 1
    req = run["evidence"]["requests"][0]
    assert req["response_status"] == 200
    assert req["passed"] is True
    excerpt = req["response_body_excerpt"]
    # The body is still recognisable (magic bytes survive); each NUL became
    # U+FFFD, the same character the lenient UTF-8 decode uses for bad bytes.
    assert excerpt.startswith("RIFF 8\ufffd\ufffdWEBPVP8X\n\ufffd\ufffd\ufffd ")
    assert "\x00" not in excerpt


@pytest.mark.asyncio
async def test_nul_in_json_body_keys_and_eval_detail_is_stored(db_pool: str) -> None:  # noqa: F811
    # Valid JSON may carry ``\u0000`` (and lone surrogates) in keys and
    # values; they reach the excerpt as a dict and the eval detail as the
    # observed value — not just the string excerpt.
    run_id = await _seed_run(
        db_pool,
        evals=[("json_path_contains", {"path": "$.name", "needle": "b"})],
    )
    response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=b'{"name": "a\\u0000b", "k\\u0000": "\\ud800", "n": 1}',
    )

    await _execute_against(db_pool, run_id, response)

    run = await _load_run(db_pool, run_id)
    assert run["status"] == "passed", run
    req = run["evidence"]["requests"][0]
    assert req["response_body_excerpt"] == {"name": "a\ufffdb", "k\ufffd": "\ufffd", "n": 1}
    detail = req["evaluations"][0]["detail"]
    assert detail["observed"] == "a\ufffdb" and detail["found"] is True
    assert req["evaluations"][0]["passed"] is True


# ---------- terminal-write failure fallback (fake pool, no DB needed) ----------


class _TerminalWriteFails(_Conn):
    """The main terminal UPDATE raises the error Postgres raised in the incident."""

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "SET status = $1, verdict = $2" in sql:
            self.fetchrows.append((sql, args))
            raise asyncpg.exceptions.UntranslatableCharacterError(
                "unsupported Unicode escape sequence"
            )
        return await super().fetchrow(sql, *args)


@pytest.mark.asyncio
async def test_failed_terminal_write_still_ends_run_in_error() -> None:
    rid = uuid4()
    conn = _TerminalWriteFails([_item(rid=rid)], [_eval(rid)])

    async def _fake_request(self, method, url, **kw):  # noqa: ANN001
        return httpx.Response(200, json={})

    with _patch_pool(_FakePool(conn)), patch.object(httpx.AsyncClient, "request", _fake_request):
        await engine_mod.execute_run(uuid4())

    assert conn.row_status == "error"
    error_writes = [
        args for sql, args in conn.fetchrows
        if "SET status = 'error'" in sql and "status = 'running'" in sql
    ]
    assert len(error_writes) == 1
    evidence = json.loads(error_writes[0][0])
    assert evidence["requests"] == []
    assert evidence["engine_error"].startswith("terminal write failed")
    assert "UntranslatableCharacterError" in evidence["engine_error"]
    completed = [
        args for sql, args in conn.executes
        if "INSERT INTO events" in sql and "run.completed" in args
    ]
    assert len(completed) == 1


# ---------- the fallback against the real refusal ----------


def _no_sanitising(obj: Any) -> Any:
    return obj


@pytest.mark.asyncio
async def test_unsanitised_nul_reaching_postgres_still_ends_run_in_error(db_pool: str) -> None:  # noqa: F811
    # Content that slips past the sanitiser (simulated by bypassing it) makes
    # Postgres refuse the verdict write for real; the run must still finish.
    run_id = await _seed_run(db_pool, evals=[("status_eq", {"expected": 200})])
    response = httpx.Response(
        200, headers={"content-type": "image/webp"}, content=_REAL_WEBP_PREFIX
    )

    with patch.object(engine_mod, "_pg_safe", _no_sanitising):
        await _execute_against(db_pool, run_id, response)

    run = await _load_run(db_pool, run_id)
    assert run["status"] == "error" and run["verdict"] == "error"
    assert run["evidence"] == {
        "requests": [],
        "engine_error": (
            "terminal write failed (UntranslatableCharacterError); request evidence dropped"
        ),
    }
    assert len(run["completed_events"]) == 1
    assert run["completed_events"][0]["status"] == "error"


@pytest.mark.asyncio
async def test_error_path_write_retries_without_request_results(db_pool: str) -> None:  # noqa: F811
    # The engine-error write (pre-flight / HTTP-loop crash) carries whatever
    # results were collected; if Postgres refuses them it retries without.
    run_id = await _seed_run(db_pool, evals=[])
    conn = await asyncpg.connect(db_pool)
    try:
        await conn.execute("UPDATE runs SET status = 'running' WHERE id = $1", run_id)
    finally:
        await conn.close()
    result = RunRequestResult(
        request_id=uuid4(),
        request_name="thumbnail",
        method="GET",
        url="http://target.test/thumbs/loop.webp",
        response_status=200,
        response_body_excerpt=_REAL_WEBP_PREFIX.decode("utf-8", "replace"),
        latency_ms=1,
        passed=False,
    )

    pool = await asyncpg.create_pool(dsn=db_pool, min_size=1, max_size=2, statement_cache_size=0)
    try:
        with (
            patch.object(engine_mod, "get_pool", return_value=pool),
            patch.object(engine_mod, "_pg_safe", _no_sanitising),
        ):
            await engine_mod._record_terminal_error(
                run_id, [result], engine_error="http loop crashed"
            )
    finally:
        await pool.close()

    run = await _load_run(db_pool, run_id)
    assert run["status"] == "error"
    assert run["evidence"] == {
        "requests": [],
        "engine_error": "http loop crashed; request evidence dropped",
    }
    assert len(run["completed_events"]) == 1


# ---------- _pg_safe ----------


def test_pg_safe_replaces_nul_and_lone_surrogates_everywhere() -> None:
    evidence = {
        "k\x00": ["a\x00b", "\ud800", "\udfff!", 1, 2.5, None, True, ("t\x00",)],
        "fine": "café \U0001f600",
    }
    out = engine_mod._pg_safe(evidence)
    r = "\N{REPLACEMENT CHARACTER}"
    assert out == {
        f"k{r}": [f"a{r}b", r, f"{r}!", 1, 2.5, None, True, [f"t{r}"]],
        "fine": "café \U0001f600",
    }
    assert evidence["k\x00"][0] == "a\x00b"  # input not mutated


def test_evidence_json_has_no_escape_postgres_refuses() -> None:
    result = RunRequestResult(
        request_id=uuid4(),
        request_name="r\x00",
        method="GET",
        url="http://x/\x00",
        response_status=200,
        response_headers={"x-h": "v\x00"},
        response_body_excerpt={"k\x00": ["\ud800"]},
        latency_ms=1,
        passed=True,
    )
    text = engine_mod._evidence_json(RunEvidence(requests=[result], engine_error="e\x00"))
    assert "\\u0000" not in text
    assert "\\ud800" not in text
    assert json.loads(text)["engine_error"] == "e\N{REPLACEMENT CHARACTER}"
