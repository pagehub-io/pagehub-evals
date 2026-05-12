# chess module — in-repo eval target

A tiny `python-chess`-backed surface that exists to be hit by the run
engine. It is **deliberately unauthenticated** (it holds no secrets, only
ephemeral game state — see `routes.py`). Not a precedent for the resource
APIs.

## Endpoints

| Method & path | Body | Response |
|---|---|---|
| `POST /v1/modules/chess/legal-moves` | `{"fen": "<FEN>"}` | `{"fen", "legal_moves": ["e2e4", ...], "turn": "white"\|"black", "is_game_over": bool}` — moves UCI long-algebraic, sorted. Malformed FEN → `422`. |
| `POST /v1/modules/chess/games` | `{"engine_color": "white"\|"black", "seed": int = 0, "starting_fen": str\|null}` | `{"game_id", "fen", "turn", "status": "in_progress", "engine_color", "engine_move": uci\|null, "move_history": [...]}`. If `engine_color == "white"` the engine plays its opening move immediately and the response reflects it. |
| `POST /v1/modules/chess/games/{game_id}/moves` | `{"move": "<uci>"}` | `{"game_id", "fen", "turn", "status", "engine_move": uci\|null, "move_history": [...], "legal_moves": [...]}`. Un-parseable UCI → `422`. Legal-format-but-illegal-given-position → `200` with `status: "illegal_move"`, board unchanged, `engine_move: null`. Unknown `game_id` → `404`. A harness move that ends the game → terminal status (`harness_won` / `draw`), no engine reply; otherwise the engine replies and if *its* move ends the game → terminal status (`harness_lost` / `draw`). |

Statuses: `in_progress` | `illegal_move` (response-only) | `harness_won` | `harness_lost` | `draw`. Only the non-`illegal_move` set is ever persisted to `chess_games.status`.

## The deterministic engine

The "engine" picks **one legal move**, chosen reproducibly from
`(seed, ply)`:

- `ply` = the number of moves already played = `move_count` =
  `jsonb_array_length(move_history)` = the 0-based index of the move being
  made. The engine's first move (when it plays White) is at `ply == 0`.
- candidates = `sorted(m.uci() for m in board.legal_moves)` — UCI-string
  sort makes the list order-stable across python-chess versions.
- `rng = random.Random(_rng_seed(seed, ply))` where
  `_rng_seed(seed, ply) = int.from_bytes(sha256(f"{seed}:{ply}").digest()[:8], "big")`
  — sha256, not Python's `hash()` (which is salted for str/bytes and not
  reproducible across processes).
- engine move = `rng.choice(candidates)`.

This means a fixture author who pins a `seed` in the `/games` body gets a
fully deterministic opponent and can script the harness's whole line.

### Canonical seed + scripted line used by `fixtures/chess.json`

`chess.json`'s `chess-playable-game` collection opens a game with
`{"engine_color": "black", "seed": 0}` and walks this White line. Each row
is the FEN **after the harness's White move and the engine's Black reply**
— i.e. the `$.fen` the `/moves` response carries, which the fixture's
`expected-fen` eval asserts.

| Step | Harness (White) | Engine (Black), `ply` | FEN after the reply |
|---|---|---|---|
| open | — | — | `rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1` |
| 1 | `e2e4` | `d7d5` (ply 1) | `rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2` |
| 2 | `g1f3` | `h7h5` (ply 3) | `rnbqkbnr/ppp1ppp1/8/3p3p/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 0 3` |
| 3 | `f1c4` | `b7b5` (ply 5) | `rnbqkbnr/p1p1ppp1/8/1p1p3p/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 4` |
| 4 | `e1g1` | `c8d7` (ply 7) | `rn1qkbnr/p1pbppp1/8/1p1p3p/2B1P3/5N2/PPPP1PPP/RNBQ1RK1 w kq - 2 5` |

Final status is `in_progress` — no checks, no mate, no illegal move in the
window.

These numbers are **independently re-derivable** — they are not "whatever
the code does". Run:

```python
import chess, random, hashlib
def _rng_seed(seed, ply):
    return int.from_bytes(hashlib.sha256(f"{seed}:{ply}".encode()).digest()[:8], "big")
def engine_move(board, seed, ply):
    return random.Random(_rng_seed(seed, ply)).choice(sorted(m.uci() for m in board.legal_moves))
board, history, seed = chess.Board(), [], 0
for w in ["e2e4", "g1f3", "f1c4", "e1g1"]:
    board.push_uci(w); history.append(w)
    em = engine_move(board, seed, len(history)); board.push_uci(em); history.append(em)
    print(w, "->", em, "->", board.fen())
```

`api/tests/test_chess.py::test_deterministic_game_given_seed` pins exactly
this table against the live engine; if the `(seed, ply)` derivation ever
changes, that test fails before the fixture's evals would.

## Storage

Game state lives in `chess_games` (`api/shared/schema.sql`) — durable on
purpose: a deploy landing mid-run must not drop the board (the run engine
would otherwise get a `404` halfway through `chess-playable-game` → `error`
verdict, flaky eval). No `owner_user_id` (games are anonymous), no reaper
this slice (the table is tiny and only read by PK; a slice-N reaper prunes
by `created_at`).
