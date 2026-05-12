"""Tests for the in-repo chess module.

Split:
- pure engine logic (``api.modules.chess.engine``) — no DB;
- ``/legal-moves`` route — no DB (stateless oracle);
- ``/games`` + ``/games/{id}/moves`` routes — need real Postgres
  (``chess_games`` state); skip cleanly when DATABASE_URL is unreachable.
"""

from __future__ import annotations

import hashlib
import random
from uuid import uuid4

import chess
import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.modules.chess import engine as ce
from api.tests._db import db_pool, operator_test_client  # noqa: F401

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
MID_FEN = "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"


# ---------- helpers re-deriving the engine choice independently ----------


def _expected_engine_move(board: chess.Board, seed: int, ply: int) -> str:
    cands = sorted(m.uci() for m in board.legal_moves)
    rng_seed = int.from_bytes(hashlib.sha256(f"{seed}:{ply}".encode()).digest()[:8], "big")
    return random.Random(rng_seed).choice(cands)


# ---------- pure engine: legality ----------


def test_legal_moves_startpos() -> None:
    result = ce.legal_moves(START_FEN)
    assert result.turn == "white"
    assert result.is_game_over is False
    assert "e2e4" in result.legal_moves
    assert "g1f3" in result.legal_moves
    assert len(result.legal_moves) == 20
    assert result.legal_moves == sorted(result.legal_moves)
    assert result.fen == START_FEN


def test_legal_moves_midgame_matches_ground_truth() -> None:
    result = ce.legal_moves(MID_FEN)
    board = chess.Board(MID_FEN)
    assert result.legal_moves == sorted(m.uci() for m in board.legal_moves)
    assert result.turn == "white"
    assert result.fen == board.fen()
    assert "e4d5" in result.legal_moves


def test_legal_moves_malformed_fen_raises_input_error() -> None:
    with pytest.raises(ce.ChessInputError):
        ce.legal_moves("not a fen at all")


# ---------- pure engine: games + moves ----------


def test_new_game_engine_black_makes_no_move() -> None:
    state = ce.new_game("black", seed=0)
    assert state.move_history == []
    assert state.move_count == 0
    assert state.turn == "white"
    assert state.status == "in_progress"


def test_new_game_engine_white_moves_first_deterministic() -> None:
    state = ce.new_game("white", seed=0)
    assert len(state.move_history) == 1
    board = chess.Board()
    assert state.move_history[0] == _expected_engine_move(board, 0, 0)
    assert state.turn == "black"
    assert state.status == "in_progress"


def test_apply_move_illegal_given_position_no_change() -> None:
    # e2e5 parses as UCI but is illegal from startpos.
    result = ce.apply_move(
        current_fen=START_FEN,
        move_history=[],
        engine_color="black",
        seed=0,
        harness_move_uci="e2e5",
    )
    assert result.status == "illegal_move"
    assert result.engine_move is None
    assert result.fen == START_FEN
    assert result.move_history == []
    assert "e2e4" in result.legal_moves


def test_apply_move_unparseable_uci_raises() -> None:
    with pytest.raises(ce.ChessInputError):
        ce.apply_move(
            current_fen=START_FEN,
            move_history=[],
            engine_color="black",
            seed=0,
            harness_move_uci="hello",
        )


def test_apply_move_legal_then_engine_reply_deterministic() -> None:
    result = ce.apply_move(
        current_fen=START_FEN,
        move_history=[],
        engine_color="black",
        seed=0,
        harness_move_uci="e2e4",
    )
    assert result.status == "in_progress"
    assert result.engine_move is not None
    assert len(result.move_history) == 2
    board = chess.Board()
    board.push_uci("e2e4")
    assert result.engine_move == _expected_engine_move(board, 0, 1)


def test_scripted_line_matches_readme_table() -> None:
    """The fixtures/chess.json scripted line — re-derived against the engine."""
    expected = {
        "e2e4": "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
        "g1f3": "rnbqkbnr/ppp1ppp1/8/3p3p/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 0 3",
        "f1c4": "rnbqkbnr/p1p1ppp1/8/1p1p3p/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 4",
        "e1g1": "rn1qkbnr/p1pbppp1/8/1p1p3p/2B1P3/5N2/PPPP1PPP/RNBQ1RK1 w kq - 2 5",
    }
    fen = ce.STARTING_FEN
    history: list[str] = []
    for wmove, want_fen in expected.items():
        result = ce.apply_move(
            current_fen=fen,
            move_history=history,
            engine_color="black",
            seed=0,
            harness_move_uci=wmove,
        )
        assert result.status == "in_progress", (wmove, result.status)
        assert result.fen == want_fen, (wmove, result.fen)
        fen = result.fen
        history = result.move_history


# ---------- /legal-moves route (no DB) ----------


@pytest.fixture
def client_no_db():
    with TestClient(app) as c:
        yield c


def test_legal_moves_route_startpos(client_no_db) -> None:
    r = client_no_db.post("/v1/modules/chess/legal-moves", json={"fen": START_FEN})
    assert r.status_code == 200
    body = r.json()
    assert body["turn"] == "white"
    assert body["is_game_over"] is False
    assert "e2e4" in body["legal_moves"]
    assert body["fen"] == START_FEN


def test_legal_moves_route_malformed_fen_422_not_500(client_no_db) -> None:
    r = client_no_db.post("/v1/modules/chess/legal-moves", json={"fen": "garbage fen"})
    assert r.status_code == 422


def test_legal_moves_route_missing_fen_422(client_no_db) -> None:
    r = client_no_db.post("/v1/modules/chess/legal-moves", json={})
    assert r.status_code == 422


# ---------- /games + /moves routes (need DB) ----------


@pytest.fixture
def client_with_db(db_pool):  # noqa: F811
    with operator_test_client(db_pool) as c:
        yield c


def test_open_game_engine_white_moves_first(client_with_db) -> None:
    r = client_with_db.post(
        "/v1/modules/chess/games", json={"engine_color": "white", "seed": 0}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["engine_move"] is not None
    assert body["turn"] == "black"
    assert body["status"] == "in_progress"
    assert len(body["move_history"]) == 1


def test_open_game_engine_black_no_move(client_with_db) -> None:
    r = client_with_db.post(
        "/v1/modules/chess/games", json={"engine_color": "black", "seed": 0}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["engine_move"] is None
    assert body["turn"] == "white"
    assert body["move_history"] == []


def test_moves_legal_then_engine_reply(client_with_db) -> None:
    open_r = client_with_db.post(
        "/v1/modules/chess/games", json={"engine_color": "black", "seed": 0}
    )
    game_id = open_r.json()["game_id"]
    r = client_with_db.post(
        f"/v1/modules/chess/games/{game_id}/moves", json={"move": "e2e4"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "in_progress"
    assert body["engine_move"] is not None
    assert len(body["move_history"]) == 2
    assert body["fen"] != START_FEN


def test_moves_illegal_given_position_returns_200(client_with_db) -> None:
    open_r = client_with_db.post(
        "/v1/modules/chess/games", json={"engine_color": "black", "seed": 0}
    )
    game_id = open_r.json()["game_id"]
    r = client_with_db.post(
        f"/v1/modules/chess/games/{game_id}/moves", json={"move": "e2e5"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "illegal_move"
    assert body["engine_move"] is None
    assert body["move_history"] == []
    assert body["fen"] == START_FEN
    assert body["legal_moves"]


def test_moves_unparseable_uci_422(client_with_db) -> None:
    open_r = client_with_db.post(
        "/v1/modules/chess/games", json={"engine_color": "black", "seed": 0}
    )
    game_id = open_r.json()["game_id"]
    r = client_with_db.post(
        f"/v1/modules/chess/games/{game_id}/moves", json={"move": "hello"}
    )
    assert r.status_code == 422


def test_moves_unknown_game_404(client_with_db) -> None:
    r = client_with_db.post(
        f"/v1/modules/chess/games/{uuid4()}/moves", json={"move": "e2e4"}
    )
    assert r.status_code == 404


def test_deterministic_game_given_seed(client_with_db) -> None:
    open_r = client_with_db.post(
        "/v1/modules/chess/games", json={"engine_color": "black", "seed": 0}
    )
    game_id = open_r.json()["game_id"]
    expected = [
        ("e2e4", "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"),
        ("g1f3", "rnbqkbnr/ppp1ppp1/8/3p3p/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 0 3"),
        ("f1c4", "rnbqkbnr/p1p1ppp1/8/1p1p3p/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 4"),
        ("e1g1", "rn1qkbnr/p1pbppp1/8/1p1p3p/2B1P3/5N2/PPPP1PPP/RNBQ1RK1 w kq - 2 5"),
    ]
    for mv, want_fen in expected:
        r = client_with_db.post(
            f"/v1/modules/chess/games/{game_id}/moves", json={"move": mv}
        )
        assert r.status_code == 200, (mv, r.text)
        body = r.json()
        assert body["status"] == "in_progress", (mv, body)
        assert body["fen"] == want_fen, (mv, body["fen"])
