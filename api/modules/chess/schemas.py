from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ChessColor(str, Enum):
    WHITE = "white"
    BLACK = "black"


class GameStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    ILLEGAL_MOVE = "illegal_move"
    HARNESS_WON = "harness_won"
    HARNESS_LOST = "harness_lost"
    DRAW = "draw"


# ---------- /legal-moves ----------


class LegalMovesRequest(BaseModel):
    fen: str = Field(..., min_length=1, max_length=200)


class LegalMovesResponse(BaseModel):
    fen: str
    legal_moves: list[str]
    turn: ChessColor
    is_game_over: bool


# ---------- /games ----------


class CreateGameRequest(BaseModel):
    engine_color: ChessColor
    seed: int = Field(default=0, ge=0, le=2**63 - 1)
    starting_fen: str | None = Field(default=None, min_length=1, max_length=200)


class CreateGameResponse(BaseModel):
    # Field order is the response key order — keep diff-friendly / contract-stable.
    model_config = ConfigDict(use_enum_values=True)

    game_id: str
    fen: str
    turn: ChessColor
    status: GameStatus
    engine_color: ChessColor
    engine_move: str | None = None
    move_history: list[str]


# ---------- /games/{id}/moves ----------


class SubmitMoveRequest(BaseModel):
    move: str = Field(..., min_length=1, max_length=10)


class SubmitMoveResponse(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    game_id: str
    fen: str
    turn: ChessColor
    status: GameStatus
    engine_move: str | None = None
    move_history: list[str]
    legal_moves: list[str]
