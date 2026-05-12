"""python-chess wrapper for the in-repo chess eval module.

FastAPI-free, like ``api/runs/engine.py`` — the route layer is the only
HTTP-aware code. Two jobs:

1. A stateless legality oracle (FEN -> sorted UCI legal moves).
2. A deterministic one-sided opponent for server-driven games.

**Deterministic engine.** The "engine" is intentionally trivial: it picks
a single legal move, chosen reproducibly from ``(seed, ply)``. ``ply`` is
the number of moves already played (``move_count`` /
``jsonb_array_length(move_history)``) — i.e. the 0-based index of the move
about to be played. For a given ``(seed, ply)`` the choice is fixed:

    moves = sorted(uci(m) for m in board.legal_moves)
    rng   = random.Random(_rng_seed(seed, ply))   # sha256("{seed}:{ply}")
    pick  = rng.choice(moves)

Sorting by UCI string makes the candidate list order-stable across
python-chess versions. ``_rng_seed`` hashes ``"{seed}:{ply}"`` through
sha256 (not Python's ``hash()``, which is salted for str/bytes and so not
reproducible across processes) and takes the first 8 bytes as the RNG seed
int. This means a fixture author can pin a ``seed`` in the
``POST /v1/modules/chess/games`` body and script the opponent's whole line;
``api/modules/chess/README.md`` documents the canonical seed and line that
``fixtures/chess.json`` relies on.

The engine plays *legally*, not *well* — SPEC: "validates the harness's
side end-to-end", not "beats the harness".
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

import chess

STARTING_FEN = chess.STARTING_FEN

# Response/stored status values. ``illegal_move`` is a *response* status
# only — it never lands in chess_games.status (an illegal move leaves the
# board untouched, no UPDATE runs).
STATUS_IN_PROGRESS = "in_progress"
STATUS_HARNESS_WON = "harness_won"
STATUS_HARNESS_LOST = "harness_lost"
STATUS_DRAW = "draw"
STATUS_ILLEGAL_MOVE = "illegal_move"


class ChessInputError(ValueError):
    """Raised for malformed FEN / un-parseable UCI — the route maps to 422."""


def _rng_seed(seed: int, ply: int) -> int:
    digest = hashlib.sha256(f"{seed}:{ply}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def engine_move_uci(board: chess.Board, seed: int, ply: int) -> str:
    """The engine's deterministic choice for ``board`` at ``ply``.

    ``board`` must have at least one legal move (caller checks game-over
    before calling this).
    """
    candidates = sorted(m.uci() for m in board.legal_moves)
    rng = random.Random(_rng_seed(seed, ply))
    return rng.choice(candidates)


def _turn_str(board: chess.Board) -> str:
    return "white" if board.turn == chess.WHITE else "black"


def _board_from_fen(fen: str) -> chess.Board:
    try:
        return chess.Board(fen)
    except ValueError as e:
        raise ChessInputError(f"invalid FEN: {e}") from e


# ---------- legality oracle ----------


@dataclass(frozen=True)
class LegalMovesResult:
    fen: str
    turn: str
    legal_moves: list[str]
    is_game_over: bool


def legal_moves(fen: str) -> LegalMovesResult:
    board = _board_from_fen(fen)
    return LegalMovesResult(
        fen=board.fen(),
        turn=_turn_str(board),
        legal_moves=sorted(m.uci() for m in board.legal_moves),
        is_game_over=board.is_game_over(),
    )


# ---------- server-driven games ----------


@dataclass(frozen=True)
class NewGameState:
    fen: str
    turn: str
    status: str
    move_history: list[str]
    move_count: int


def new_game(engine_color: str, *, seed: int = 0, starting_fen: str | None = None) -> NewGameState:
    """Open a game; if the engine plays White it makes the first move now.

    ``engine_color`` is "white" or "black" (validated at the schema layer).
    """
    fen = starting_fen if starting_fen is not None else STARTING_FEN
    board = _board_from_fen(fen)
    move_history: list[str] = []
    if engine_color == "white" and not board.is_game_over():
        uci = engine_move_uci(board, seed, len(move_history))
        board.push_uci(uci)
        move_history.append(uci)
    return NewGameState(
        fen=board.fen(),
        turn=_turn_str(board),
        status=_status_for(board, engine_color),
        move_history=move_history,
        move_count=len(move_history),
    )


@dataclass(frozen=True)
class MoveResult:
    fen: str
    turn: str
    status: str
    engine_move: str | None
    move_history: list[str]
    move_count: int
    legal_moves: list[str]


def _status_for(board: chess.Board, engine_color: str) -> str:
    """Terminal-or-ongoing status from the board's perspective + who the engine is."""
    if not board.is_game_over():
        return STATUS_IN_PROGRESS
    outcome = board.outcome()
    if outcome is None or outcome.winner is None:
        return STATUS_DRAW
    winner_color = "white" if outcome.winner == chess.WHITE else "black"
    # The harness plays the side the engine does NOT.
    harness_color = "black" if engine_color == "white" else "white"
    return STATUS_HARNESS_WON if winner_color == harness_color else STATUS_HARNESS_LOST


def apply_move(
    *,
    current_fen: str,
    move_history: list[str],
    engine_color: str,
    seed: int,
    harness_move_uci: str,
) -> MoveResult:
    """Apply the harness's move (UCI) and, if the game continues, the engine's reply.

    Raises ``ChessInputError`` if ``harness_move_uci`` is un-parseable as
    UCI notation. A *parseable but illegal-given-position* move returns a
    ``MoveResult`` with ``status == "illegal_move"``, the board untouched,
    and ``engine_move is None`` — NOT an exception (the route returns 200).
    """
    board = _board_from_fen(current_fen)

    try:
        move = chess.Move.from_uci(harness_move_uci)
    except ValueError as e:
        raise ChessInputError(f"unparseable UCI move: {e}") from e
    if move == chess.Move.null() or move not in board.legal_moves:
        return MoveResult(
            fen=board.fen(),
            turn=_turn_str(board),
            status=STATUS_ILLEGAL_MOVE,
            engine_move=None,
            move_history=list(move_history),
            move_count=len(move_history),
            legal_moves=sorted(m.uci() for m in board.legal_moves),
        )

    new_history = list(move_history)
    board.push(move)
    new_history.append(move.uci())

    engine_move: str | None = None
    if not board.is_game_over():
        engine_move = engine_move_uci(board, seed, len(new_history))
        board.push_uci(engine_move)
        new_history.append(engine_move)

    return MoveResult(
        fen=board.fen(),
        turn=_turn_str(board),
        status=_status_for(board, engine_color),
        engine_move=engine_move,
        move_history=new_history,
        move_count=len(new_history),
        legal_moves=sorted(m.uci() for m in board.legal_moves),
    )
