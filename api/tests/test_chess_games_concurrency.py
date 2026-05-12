"""Concurrency test for ``POST /v1/modules/chess/games/{id}/moves``'s
``SELECT ... FOR UPDATE`` row lock.

This is the only test that proves the lock does its job. Two transactions
race to apply a move on the *same* ``game_id``; the row lock must serialize
them so:

* exactly one applies a real state transition (board advances, ``move_count``
  grows by 2 — harness move + engine reply);
* the other, running *after* the winner committed, re-reads the post-first-
  move board and finds the same move now illegal (it's the engine's turn) →
  ``status == 'illegal_move'``, board untouched, no second apply, no lost
  update.

DB-gated like the other DB-backed tests: skips cleanly when no Postgres is
reachable (``api/tests/_db.py`` pattern). Drives the route handler's exact
transaction logic against two ``asyncpg`` connections — the cleanest way to
demonstrate serialization on the row lock without TestClient (which is
synchronous and can't actually run two requests concurrently).
"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

import asyncpg
import pytest

from api.modules.chess import engine as ce
from api.tests._db import db_pool  # noqa: F401

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
RACED_MOVE = "e2e4"


async def _create_game(url: str) -> str:
    conn = await asyncpg.connect(url)
    try:
        state = ce.new_game("black", seed=0)
        row = await conn.fetchrow(
            """
            INSERT INTO chess_games (engine_color, seed, starting_fen, fen, move_history, move_count, status)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
            RETURNING id
            """,
            "black",
            0,
            ce.STARTING_FEN,
            state.fen,
            json.dumps(state.move_history),
            state.move_count,
            state.status,
        )
        return str(row["id"])
    finally:
        await conn.close()


def _jsonb_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return json.loads(value)
    return list(value)


async def _submit_move(url: str, game_id, *, move: str, hold: float) -> dict:
    """Mirror api.modules.chess.routes.submit_move's transaction body.

    ``hold`` is a sleep *inside* the transaction, after the FOR UPDATE
    SELECT and before COMMIT, to widen the race window deterministically.
    """
    conn = await asyncpg.connect(url)
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, engine_color, seed, fen, move_history, move_count, status
                FROM chess_games
                WHERE id = $1
                FOR UPDATE
                """,
                game_id,
            )
            assert row is not None
            move_history = _jsonb_list(row["move_history"])
            result = ce.apply_move(
                current_fen=row["fen"],
                move_history=move_history,
                engine_color=row["engine_color"],
                seed=row["seed"],
                harness_move_uci=move,
            )
            if hold:
                await asyncio.sleep(hold)
            if result.status != ce.STATUS_ILLEGAL_MOVE:
                await conn.execute(
                    """
                    UPDATE chess_games
                    SET fen = $1, move_history = $2::jsonb, move_count = $3, status = $4, updated_at = now()
                    WHERE id = $5
                    """,
                    result.fen,
                    json.dumps(result.move_history),
                    result.move_count,
                    result.status,
                    game_id,
                )
        return {
            "status": result.status,
            "fen": result.fen,
            "move_count": result.move_count,
            "move_history": result.move_history,
        }
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_concurrent_moves_serialize_on_row_lock(db_pool) -> None:  # noqa: F811
    url = db_pool
    game_id = UUID(await _create_game(url))

    # Transaction A grabs the lock and holds it briefly; B starts ~immediately
    # and blocks on FOR UPDATE until A commits. Both submit the same legal move.
    results = await asyncio.gather(
        _submit_move(url, game_id, move=RACED_MOVE, hold=0.4),
        _submit_move(url, game_id, move=RACED_MOVE, hold=0.0),
    )

    by_status: dict[str, list[dict]] = {"in_progress": [], "illegal_move": []}
    for r in results:
        by_status.setdefault(r["status"], []).append(r)

    # Exactly one applied a real transition; exactly one saw it as illegal.
    assert len(by_status["in_progress"]) == 1, results
    assert len(by_status["illegal_move"]) == 1, results

    winner = by_status["in_progress"][0]
    loser = by_status["illegal_move"][0]

    # Winner advanced the board by harness move + engine reply.
    assert winner["move_count"] == 2
    assert len(winner["move_history"]) == 2
    assert winner["move_history"][0] == RACED_MOVE
    assert winner["fen"] != START_FEN

    # Loser saw the post-winner board (e2e4 already played → White can't play it
    # again; it's the engine's reply that's on the board), did NOT re-apply.
    assert loser["move_count"] == 2
    assert len(loser["move_history"]) == 2
    assert loser["move_history"][0] == RACED_MOVE

    # The persisted row reflects exactly one apply — no double-apply, no lost
    # update; move_count == len(move_history).
    conn = await asyncpg.connect(url)
    try:
        final = await conn.fetchrow(
            "SELECT fen, move_history, move_count, status FROM chess_games WHERE id = $1",
            game_id,
        )
    finally:
        await conn.close()
    persisted_history = _jsonb_list(final["move_history"])
    assert final["move_count"] == 2
    assert len(persisted_history) == 2
    assert final["move_count"] == len(persisted_history)
    assert persisted_history[0] == RACED_MOVE
    assert final["status"] == "in_progress"
    assert final["fen"] == winner["fen"]


pytestmark = pytest.mark.asyncio
