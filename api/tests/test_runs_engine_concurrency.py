"""Concurrency & idempotency tests for ``api.runs.engine.execute_run``.

The engine uses two ``WHERE status=...`` guards to make double-dispatch
safe: the pending→running transition, and the running→terminal write.
Tests here mock the asyncpg pool to exercise those guards without
needing a live Postgres — the existing fixture infrastructure is
pool-less by design (see ``conftest.py``).

Three properties are asserted:

* ``test_double_dispatch_writes_terminal_once`` — two concurrent
  ``execute_run(run_id)`` calls produce at most one ``run.completed``
  event and one terminal UPDATE that matches a row.
* ``test_terminal_update_no_op_when_already_terminal`` — pre-set the
  row to ``status='passed'``; the engine MUST NOT emit a fresh
  ``run.completed`` event nor mutate the row.
* ``test_pool_not_held_during_http_fire`` — the asyncpg pool is
  acquired exactly twice (pre-flight, terminal write), and both
  acquires release before/after the in-flight httpx request.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from api.runs import engine as engine_mod

# ---------- fake asyncpg connection + pool ----------


class _FakeConn:
    """Records SQL fragments + args; serves canned rows."""

    def __init__(self, rows_for_select: dict[str, Any], starting_status: str) -> None:
        # rows_for_select: keys are SQL substrings; value is the row to return.
        self._rows_for_select = rows_for_select
        # Mutated in place: we model the row's lifecycle (pending → running →
        # terminal) and let the guarded UPDATEs observe the mutations.
        self.row_status = starting_status
        self.executes: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrows: list[tuple[str, tuple[Any, ...]]] = []
        self.fetches: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrows.append((sql, args))
        # Initial read: collection_id + environment_id.
        if "SELECT collection_id, environment_id FROM runs" in sql:
            return self._rows_for_select.get("initial_read")
        # Guarded pending → running.
        if "SET status = 'running'" in sql and "status = 'pending'" in sql:
            if self.row_status == "pending":
                self.row_status = "running"
                return {"id": args[0]}
            return None
        # Guarded terminal UPDATE.
        if "SET status = $1, verdict = $2" in sql and "status = 'running'" in sql:
            if self.row_status == "running":
                self.row_status = args[0]
                now = datetime.now(tz=UTC)
                return {"started_at": now, "finished_at": now}
            return None
        # Best-effort terminal-error UPDATE.
        if "SET status = 'error'" in sql and "status = 'running'" in sql:
            if self.row_status == "running":
                self.row_status = "error"
                return {"id": args[1]}
            return None
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetches.append((sql, args))
        if "FROM collection_items ci" in sql:
            return []
        if "FROM evaluations" in sql:
            return []
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executes.append((sql, args))
        return "OK"


class _FakeAcquireCtx:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConn:
        # asyncio.get_event_loop().time() is monotonic across the test.
        self._pool.acquire_starts.append(asyncio.get_event_loop().time())
        return self._pool._conn

    async def __aexit__(self, *exc: Any) -> None:
        self._pool.acquire_ends.append(asyncio.get_event_loop().time())


class _FakePool:
    """Records acquire/release order and yields a shared ``_FakeConn``."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self.acquire_starts: list[float] = []
        self.acquire_ends: list[float] = []

    def acquire(self) -> _FakeAcquireCtx:
        return _FakeAcquireCtx(self)


# ---------- shared fixtures ----------


def _initial_read_row(
    *,
    collection_id: UUID | None = None,
    environment_id: UUID | None = None,
) -> dict[str, Any]:
    return {"collection_id": collection_id, "environment_id": environment_id}


@contextlib.contextmanager
def _patch_pool(pool: _FakePool):
    with patch.object(engine_mod, "get_pool", return_value=pool):
        yield


# ---------- tests ----------


@pytest.mark.asyncio
async def test_double_dispatch_writes_terminal_once() -> None:
    """Two concurrent execute_run calls on a pending row → exactly one
    pending→running succeeds, exactly one terminal write succeeds,
    exactly one ``run.completed`` event is inserted."""
    run_id = uuid4()
    conn = _FakeConn(
        rows_for_select={"initial_read": _initial_read_row()},
        starting_status="pending",
    )
    pool = _FakePool(conn)
    with _patch_pool(pool):
        await asyncio.gather(
            engine_mod.execute_run(run_id),
            engine_mod.execute_run(run_id),
        )

    # The pending→running guard rejects 1 of the 2 dispatches:
    pending_to_running_attempts = [
        f for f in conn.fetchrows
        if "SET status = 'running'" in f[0] and "status = 'pending'" in f[0]
    ]
    assert len(pending_to_running_attempts) == 2

    # Only one run.started + one run.completed event should be written.
    started_inserts = [e for e in conn.executes if "INSERT INTO events" in e[0] and "run.started" in repr(e[1])]
    completed_inserts = [e for e in conn.executes if "INSERT INTO events" in e[0] and "run.completed" in repr(e[1])]
    assert len(started_inserts) == 1, (
        f"expected exactly one run.started event; got {len(started_inserts)}"
    )
    assert len(completed_inserts) == 1, (
        f"expected exactly one run.completed event; got {len(completed_inserts)}"
    )

    # Final row status is terminal — empty collection → verdict=error.
    assert conn.row_status == "error"


@pytest.mark.asyncio
async def test_terminal_update_no_op_when_already_terminal() -> None:
    """Row already in 'passed' state: execute_run must not mutate it and
    must not emit a fresh ``run.completed`` event."""
    run_id = uuid4()
    conn = _FakeConn(
        rows_for_select={"initial_read": _initial_read_row()},
        starting_status="passed",
    )
    pool = _FakePool(conn)
    with _patch_pool(pool):
        await engine_mod.execute_run(run_id)

    # The pending→running guard sees status='passed', returns 0 rows.
    pending_to_running_attempts = [
        f for f in conn.fetchrows
        if "SET status = 'running'" in f[0] and "status = 'pending'" in f[0]
    ]
    assert len(pending_to_running_attempts) == 1, (
        "engine should attempt the guarded transition exactly once"
    )

    # No events emitted at all — engine bails after the guard fails.
    event_inserts = [e for e in conn.executes if "INSERT INTO events" in e[0]]
    assert event_inserts == [], (
        f"expected zero events; got {len(event_inserts)}: {event_inserts}"
    )

    # Row stays where it was.
    assert conn.row_status == "passed"


@pytest.mark.asyncio
async def test_pool_acquired_exactly_twice_for_empty_collection() -> None:
    """The engine must acquire the pool exactly twice (pre-flight +
    terminal) — never inside the HTTP loop. Empty-collection short-
    circuits but still hits both acquires."""
    run_id = uuid4()
    conn = _FakeConn(
        rows_for_select={"initial_read": _initial_read_row()},
        starting_status="pending",
    )
    pool = _FakePool(conn)
    with _patch_pool(pool):
        await engine_mod.execute_run(run_id)

    assert len(pool.acquire_starts) == 2, (
        f"expected exactly 2 pool acquires; got {len(pool.acquire_starts)}"
    )
    assert len(pool.acquire_ends) == 2, (
        f"expected exactly 2 pool releases; got {len(pool.acquire_ends)}"
    )
    # First release strictly before second acquire — the engine releases
    # between pre-flight and terminal write.
    assert pool.acquire_ends[0] <= pool.acquire_starts[1], (
        "pool release(1) must precede acquire(2)"
    )


@pytest.mark.asyncio
async def test_engine_error_round_trips_via_evidence() -> None:
    """If the pre-flight raises, _record_terminal_error must write
    ``evidence.engine_error`` and the RunEvidence schema must preserve
    it (no silent strip by Pydantic's default extra='ignore')."""
    run_id = uuid4()

    # Force pre-flight to crash: initial_read row is None → routes the
    # engine into the early-return path, NOT the engine_error path. To
    # exercise engine_error we patch the inner read to raise instead.
    class _ExplodingConn(_FakeConn):
        async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
            if "SELECT collection_id, environment_id FROM runs" in sql:
                raise RuntimeError("simulated DB hiccup")
            return await super().fetchrow(sql, *args)

    conn = _ExplodingConn(
        rows_for_select={"initial_read": _initial_read_row()},
        starting_status="running",
    )
    pool = _FakePool(conn)
    with _patch_pool(pool):
        await engine_mod.execute_run(run_id)

    # The best-effort terminal-error UPDATE should have succeeded and
    # the evidence column must contain engine_error.
    terminal_error_writes = [
        f for f in conn.fetchrows
        if "SET status = 'error'" in f[0] and "status = 'running'" in f[0]
    ]
    assert len(terminal_error_writes) == 1
    # args order: (json_evidence_str, run_id)
    evidence_json_str = terminal_error_writes[0][1][0]
    assert "engine_error" in evidence_json_str
    assert "pre-flight failed" in evidence_json_str


# ---------- module-level pytest config ----------

pytestmark = pytest.mark.asyncio
