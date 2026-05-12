"""In-repo chess module — a legality oracle + a one-sided server opponent.

This is the eval *target*, not a protected resource: the endpoints are
**intentionally unauthenticated**. They hold no secrets, only ephemeral
game state, and they exist to be hit by the run engine, which sends exactly
the headers in a request template (no auto-injected auth). Requiring auth
here would force every chess request template in ``fixtures/chess.json`` to
carry an env-sourced key header — friction with no security benefit. Not a
precedent for the resource APIs.

Game state lives in the ``chess_games`` table (durable on purpose: a deploy
landing mid-run must not drop the board). A ``/moves`` call is a
read-modify-write under ``SELECT ... FOR UPDATE`` inside a transaction so a
harness move + the engine reply land atomically.
"""

from __future__ import annotations

import json
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from api.modules.chess import engine as chess_engine
from api.modules.chess.schemas import (
    CreateGameRequest,
    CreateGameResponse,
    LegalMovesRequest,
    LegalMovesResponse,
    SubmitMoveRequest,
    SubmitMoveResponse,
)
from api.shared.db import get_db

router = APIRouter(prefix="/v1/modules/chess")


def _jsonb_list(value: object) -> list[str]:
    # asyncpg returns a jsonb column as a str by default (no codec registered);
    # the str branch is the real path. The list fallback is defensive in case a
    # codec is ever wired up.
    if value is None:
        return []
    if isinstance(value, str):
        return json.loads(value)
    return list(value)


@router.post("/legal-moves", response_model=LegalMovesResponse)
async def legal_moves(body: LegalMovesRequest) -> LegalMovesResponse:
    try:
        result = chess_engine.legal_moves(body.fen)
    except chess_engine.ChessInputError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return LegalMovesResponse(
        fen=result.fen,
        legal_moves=result.legal_moves,
        turn=result.turn,
        is_game_over=result.is_game_over,
    )


@router.post("/games", response_model=CreateGameResponse)
async def create_game(
    body: CreateGameRequest,
    db: asyncpg.Connection = Depends(get_db),
) -> CreateGameResponse:
    try:
        state = chess_engine.new_game(
            body.engine_color.value,
            seed=body.seed,
            starting_fen=body.starting_fen,
        )
    except chess_engine.ChessInputError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    starting_fen = body.starting_fen if body.starting_fen is not None else chess_engine.STARTING_FEN
    row = await db.fetchrow(
        """
        INSERT INTO chess_games (engine_color, seed, starting_fen, fen, move_history, move_count, status)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
        RETURNING id
        """,
        body.engine_color.value,
        body.seed,
        starting_fen,
        state.fen,
        json.dumps(state.move_history),
        state.move_count,
        state.status,
    )
    engine_move = state.move_history[-1] if (body.engine_color.value == "white" and state.move_history) else None
    return CreateGameResponse(
        game_id=str(row["id"]),
        fen=state.fen,
        turn=state.turn,
        status=state.status,
        engine_color=body.engine_color.value,
        engine_move=engine_move,
        move_history=state.move_history,
    )


@router.post("/games/{game_id}/moves", response_model=SubmitMoveResponse)
async def submit_move(
    game_id: UUID,
    body: SubmitMoveRequest,
    db: asyncpg.Connection = Depends(get_db),
) -> SubmitMoveResponse:
    async with db.transaction():
        row = await db.fetchrow(
            """
            SELECT id, engine_color, seed, starting_fen, fen, move_history, move_count, status
            FROM chess_games
            WHERE id = $1
            FOR UPDATE
            """,
            game_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Game not found")

        move_history = _jsonb_list(row["move_history"])
        try:
            result = chess_engine.apply_move(
                current_fen=row["fen"],
                move_history=move_history,
                engine_color=row["engine_color"],
                seed=row["seed"],
                harness_move_uci=body.move,
            )
        except chess_engine.ChessInputError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

        if result.status != chess_engine.STATUS_ILLEGAL_MOVE:
            await db.execute(
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

    return SubmitMoveResponse(
        game_id=str(game_id),
        fen=result.fen,
        turn=result.turn,
        status=result.status,
        engine_move=result.engine_move,
        move_history=result.move_history,
        legal_moves=result.legal_moves,
    )
