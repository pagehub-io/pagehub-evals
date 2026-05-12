# SPEC

> Build: pagehub-evals — fixture import/export (declarative JSON bundles;
> `POST /v1/fixtures/import` upsert-by-name + `GET /v1/collections/{id}/export`;
> round-trip invariant; `fixtures/chess.json` as first consumer).

## Manager

### Problem
Populating a pagehub-evals instance with collections / requests / evaluations today means hand-POSTing many resources one at a time, or running imperative `platform/evals/seeds/` scripts that build them over HTTP. There is no named, declarative, version-controlled artifact that *is* an eval suite. So suites aren't diffable in code review, can't be re-applied idempotently to a fresh environment, and a hand-built collection can't be captured back into source. Cost of doing nothing: every new eval target (chess being the first) is bespoke setup work, environments drift, and "what evals does this instance actually have?" is unanswerable from git.

### Users
- **Eval authors** (the people writing collections that grade harnesses) — want to write a suite as one JSON file, check it in, and import it anywhere.
- **Operators** running pagehub-evals environments (staging/prod/local) — want `POST /v1/fixtures/import` to bring a fresh DB to a known state, idempotently, with a created/updated count they can trust.
- **Harness owners** integrating an LLM coding harness — get a concrete, self-contained target (`chess.json`): a legality oracle plus the collections that score their harness against it.
- **Reviewers** of this repo — get a diffable `fixtures/` directory instead of imperative seed scripts.

### Success criteria
- `POST /v1/fixtures/import` of `fixtures/chess.json` into an empty DB creates the chess module's environments, requests (with inline evaluations), and the `chess-legality` + `chess-playable-game` collections; response reports accurate `created` / `updated` counts.
- Re-importing the same fixture immediately after reports `created: 0` and only `updated` counts (or zero changes) — provably idempotent.
- `GET /v1/collections/{id}/export` returns a fixture bundle that imports cleanly into a different environment and reproduces the same collection (items resolve by request name, not UUID).
- Round-trip eval passes: `export → import → export` yields byte-identical bundles modulo timestamps and ids — and this is enforced by a checked-in eval, not a one-time manual check.
- `fixtures/chess.json` round-trips through that eval like any other fixture.
- A harness that returns a legal move for a given FEN passes `chess-legality`; a harness that plays a full game correctly against the server-side opponent passes `chess-playable-game`; wrong/illegal moves fail with evidence.

### In scope
- Fixture file format: self-contained declarative JSON bundle — `environments[]`, `requests[]` (each with inline `evaluations[]`), `collections[]` whose items reference requests by **name**, not UUID.
- `POST /v1/fixtures/import` — upsert-by-name, idempotent, returns created/updated counts. Auth via operator JWT.
- `GET /v1/collections/{id}/export` — dumps the collection + its requests + their evaluations as a fixture bundle.
- A checked-in eval asserting the `export → import → export` identity (modulo timestamps/ids).
- `fixtures/` directory in the repo for checked-in fixture files.
- `fixtures/chess.json`: the chess module (FEN → legal moves oracle + one-sided game opponent, backed by `python-chess` server-side), the `chess-legality` collection (harness returns a legal move for a FEN; eval checks the move is in `legal_moves`), and the `chess-playable-game` collection (server-side engine plays one side, validates the harness's side end-to-end).
- Keeping eval *coverage* of all the above in `platform/evals/seeds/` as before — that layer keeps using seeds; this repo gets fixtures only.

### Out of scope
- Any UI — no fixtures-management screen, no chess board / game viewer. Triage UX for the resulting runs is the existing operator UI's job, untouched here.
- Seed scripts inside pagehub-evals — fixtures are the only distribution unit here; no imperative population code lands in this repo.
- Importing/exporting non-collection-scoped things or partial-bundle merge semantics beyond upsert-by-name (no rename detection, no deletes-on-import, no dependency-graph diffing).
- Porting `platform/evals/` data into fixture format wholesale (chess is the only consumer this slice).
- A general-purpose chess service — the module is a legality oracle and a one-sided opponent and nothing more (no openings, no clocks, no ratings, no PGN library surface).
- Versioned/migratable fixture schema, signing, or a fixture registry — single declared shape, take it or leave it.

### Open questions for the trio
1. **Chess module placement (flag — Architect to settle).** "A chess module backed by `python-chess` server-side" reads two ways: (i) a new lightweight endpoint surface inside pagehub-evals (e.g. `/v1/modules/chess/legal-moves`, `/v1/modules/chess/play`) that the fixture's requests target via the instance's own base URL; or (ii) an external module the fixture's requests reach via a `{{BASE_URL}}`-style env var. **Recommended reading: (i)** — pagehub-evals already hosts the thing-under-test pattern, an in-repo module keeps `chess.json` truly self-contained (no external deploy to stand up before the fixture works), and it's the smaller moving-parts story. Either way the module stays tiny: FEN in → legal moves out, plus "engine plays one side" for the playable-game collection. (Review note for the Architect: the brief assumes this module exists; if it doesn't yet, that's a real prerequisite, not a detail.)
2. Does `import` need an explicit "dry run" mode (report counts, change nothing) for operators validating a fixture before applying? Nice-to-have; defaulting to no unless cheap.
3. On name collision across resource *types* sharing a namespace (e.g. a request and a collection both named `chess-legality`) — is name uniqueness per-type or global? Affects the upsert key. (Implementation detail flagged for the Architect.)
4. Export auth: operator-only, or also harness-key-readable? Recommend operator-only (export is an authoring action), but worth a line in the spec.

## Designer

### Surfaces

This slice is machine-facing. The "surfaces" are:

1. **The fixture file format** — a self-contained declarative JSON bundle, the
   primary artifact operators hand-edit and `git diff`. Lives in `fixtures/*.json`.
2. **`POST /v1/fixtures/import`** — body is a fixture bundle; upserts by name;
   returns per-kind `{created, updated}` counts plus a top-level
   `warnings: list[str]`. Idempotent (a re-import reports `created: 0`). Request
   bodies over **1 MiB (1,048,576 bytes)** are rejected with `413` (self-describing
   body) before any JSON parsing.
3. **`GET /v1/collections/{id}/export`** — returns a fixture bundle (the
   collection + its requests + their evaluations). `Content-Type:
   application/json`, served inline (no `Content-Disposition`; callers redirect
   to a file themselves — keeps it usable from Swagger "Try it out").
4. **OpenAPI docs** at `/docs` and `/redoc` — `FixtureBundle`, `FixtureImportResponse`,
   `FixtureImportError` schemas must be fully modeled (Pydantic `response_model`),
   so the format is discoverable without reading source.
5. **`fixtures/` directory** in the repo — checked-in fixture files; `fixtures/chess.json`
   is the first consumer. No seed scripts — fixtures only.

### The fixture bundle shape

Top-level keys, all optional except `version` (any key may be `[]`):

```jsonc
{
  "version": 1,                       // schema version; importer rejects unknown majors
  "environments": [                   // OMITTED or [] in exports (see below)
    {
      "name": "chess-local",
      "variables": { "CHESS_BASE_URL": "http://localhost:8002" },   // the instance's own origin
      "secrets":   { }                       // KEYS ONLY, every value MUST be "" (non-empty → 422). chess.json needs none.
    }
  ],
  "requests": [
    {
      "name": "legal-moves-from-startpos",
      "method": "POST",
      "url": "{{CHESS_BASE_URL}}/v1/modules/chess/legal-moves",
      "headers": { "Content-Type": "application/json" },
      "body": { "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1" },
      "capture": {},                  // JSONPath-lite, value must start with "$"
      "evaluations": [                // INLINE — not a separate top-level array
        { "name": "ok",        "kind": "status_eq",     "config": { "expected": 200 } },
        { "name": "e4-listed", "kind": "body_contains", "config": { "needle": "e2e4" } }
      ]
    }
  ],
  "collections": [
    {
      "name": "chess-legality",
      "description": "Harness must return a legal move for each FEN.",
      "items": [                      // ARRAY of request NAMES (strings). Array index == position (0,1,2,...). No explicit `position` field.
        "legal-moves-from-startpos",
        "legal-moves-from-midgame"
      ]
    }
  ]
}
```

> **Note on `items` shape (reconciled with Architect):** items are bare request
> *name strings*, not `{ "request": "<name>" }` objects. The string-array form is
> what the Architect's `FixtureCollection.items: list[str]` model declares; this
> Designer section originally sketched the object form and has been brought into
> line. Resolution semantics are unchanged: every name must resolve to a request
> **in this same bundle** (bundle-only; see Error states).

Field mapping onto stored shapes (`api/*/schemas.py`, `api/shared/schema.sql`):

- `environments[]` → `environments` table. `variables` = plaintext map (as today).
  **`secrets` in a fixture carries KEYS ONLY with empty-string placeholder values.**
  Rationale: secrets are Fernet ciphertext at rest; a fixture is a git-tracked
  declarative file and must never carry decrypted secret material. On import: for
  each secret key with an empty value, the importer **preserves the existing
  ciphertext** if the environment already has that key, and creates the key with
  an empty/unset marker if it's new (operator fills it in later via
  `PATCH /v1/environments/{id}`, which already exists). A non-empty secret value
  in a fixture is a **hard 422** ("fixtures must not contain secret values; use
  empty placeholders") — fail loud, don't silently encrypt-and-store. On export:
  `environments` is omitted entirely (see below), so this asymmetry only bites
  hand-authored fixtures, and the 422 is the guardrail.
  > **Resolved with the Architect** — the Architect's Contracts/Trade-offs
  > sections now adopt this exact rule (`FixtureEnvironment.secrets: dict[str,
  > str]`, every value MUST be `""`, non-empty → 422 at parse time; import
  > preserves the target environment's existing ciphertext for a known key, and
  > writes `encrypt("")` as an unset placeholder for a new key). An earlier
  > Architect sketch said "plaintext, importer Fernet-encrypts" — that's been
  > dropped (it could never round-trip: Fernet ciphertext is non-deterministic,
  > and a git-tracked fixture must not carry decrypted secret material). No
  > remaining conflict here.
- `requests[]` → `requests` table. `body` is arbitrary JSON (`Any`, as today).
  `capture` keeps its existing validation (`^[A-Za-z_]\w*$` keys, `$`-prefixed
  JSONPath-lite values). `evaluations[]` nested under each request → `evaluations`
  table rows with `request_id` resolved post-insert; `kind` validated against
  `EvaluationKind` and `config` validated by the per-kind config model — an
  unknown kind or bad config shape is a 422 referencing the offending request +
  evaluation name (see Error states).
- `collections[]` → `collections` + `collection_items`. `items[]` is a list of
  request **names** (strings), each resolved to a UUID at import time. `position`
  is the array index — there is no explicit `position` field, and any object-form
  item (e.g. `{ "request": "...", "position": 3 }`) is rejected (`422`:
  "collection items are request names, not objects") so two fixtures can't
  disagree with themselves about ordering.
- Volatile fields (`id`, `created_at`, `updated_at`, `owner_user_id`, and
  `requests.id`/`evaluations.id`/`collections.id`/`collection_items.*`) **never
  appear in a fixture** — not on import (ignored if present, with a warning in
  the response) and not on export (stripped).

### Primary flows

1. **Author & import a fixture (operator):**
   write/edit `fixtures/chess.json` → `POST /v1/fixtures/import` with the file as
   the JSON body → 200 with `{created, updated}` per resource kind →
   operator confirms counts match expectation → commits the file.
2. **Re-import (idempotency check / CI):**
   `POST /v1/fixtures/import` with the *same* file again → 200 with `created: 0`
   for every kind (`updated` non-zero — rows were re-touched — but nothing new
   was made) → re-import created nothing, so it's idempotent. (`created: 0` is
   the idempotency signal — see the note in Import semantics on why there is no
   `unchanged` bucket.)
3. **Export a collection:**
   `GET /v1/collections/{id}/export` → 200, body is a `FixtureBundle` with
   `environments: []`, the collection, every request its items reference, every
   evaluation on those requests → operator pipes to `fixtures/<name>.json`.
4. **Round-trip (the invariant eval):**
   pick a collection → `GET …/export` (call it A) → `POST …/import` of A →
   `GET …/export` again (call it B) → assert
   `normalize_for_roundtrip(A) == normalize_for_roundtrip(B)`, where
   `normalize_for_roundtrip` is the single shared normalizer in
   `api/fixtures/schemas.py` (Architect's Contracts section pins its exact
   behavior: drop server-assigned fields wherever they appear, drop
   `environments` from the comparison, sort `requests`/`collections`/per-request
   `evaluations` by `name`, keep collection `items` in array order, carry
   `version` through). Two consumers run it: a pure pytest in
   `api/tests/test_fixtures_roundtrip.py` (drives `fixtures.engine` against a
   test DB) and a `platform/evals/seeds/` seed in the *platform* repo (the
   SPEC's "checked-in eval"). The assertion is exactly: **export → import →
   export is identical modulo timestamps/ids — and in practice modulo nothing,
   because export emits requests in first-referenced order and assigns positions
   densely from array index, so the normalizer's sort steps are no-ops on
   export-produced bundles.**
5. **Harness runs `chess-legality` (downstream of this slice):**
   harness authenticates → `POST /v1/runs` against the imported `chess-legality`
   collection → engine executes each request (harness's move responses are the
   fixture's `requests[]`) → evaluations assert legality → verdict.

### Import semantics & response shape

`POST /v1/fixtures/import` request body = a `FixtureBundle`. Response
(`FixtureImportResponse`, 200):

```jsonc
{
  "environments": { "created": 1, "updated": 0 },
  "requests":     { "created": 2, "updated": 0 },
  "evaluations":  { "created": 4, "updated": 0 },
  "collections":  { "created": 2, "updated": 0 },
  "warnings": [
    "environments[0]: ignored unexpected key 'id'",
    "requests[1].evaluations[0]: existing evaluation reused (matched by name)"
  ]
}
```

- **`{created, updated}` only — no `unchanged`, no `deleted` bucket** (reconciled
  with the Architect, who owns the importer). `created` = rows that did not exist
  by their unique key before this import; `updated` = rows that existed and were
  re-touched (whether or not the values actually changed). `created: 0` on a
  re-import is the idempotency signal flow #2 keys on. Distinguishing "touched but
  unchanged" needs a per-row before/after diff that operators don't act on; and
  for children that are delete-all-then-reinsert (evaluations, collection items)
  there's no stable per-row identity to count `deleted` against — so the eval
  set / item list count rolls up to its **parent** (an evaluation set is
  "created" if its request was created, "updated" if the request already existed).
  Trade-off noted: an operator can't see from the response that the import *also
  silently dropped* three stale evaluations or two stale collection items — the
  declarative-replace behavior below is the only documentation of that, so it has
  to be loud in the OpenAPI description. (Designer's earlier draft listed
  `unchanged`/`deleted`; brought into line.)
- **Upsert key is `name`** scoped to the importing operator (`owner_user_id`),
  which matches the existing `UNIQUE (owner_user_id, name)` on environments and
  collections. Requests have no unique constraint today — Architect adds
  `UNIQUE (owner_user_id, name)` on `requests`; this also means `POST`/`PATCH
  /v1/requests` can now 409 (mirroring collections/environments).
- **Evaluations** are children of a request; the importer **replaces** a
  request's evaluation set with the fixture's list (declarative desired-state) —
  delete-all-then-reinsert per request. An evaluation present in the DB but
  absent from the fixture is **dropped** (silently, modulo the loud OpenAPI doc).
  The whole eval set counts under its parent request's bucket (see above).
- **Collection items: import REPLACES the item list.** A fixture is
  declarative/desired-state. If a collection's stored items have diverged from
  the fixture (rows added via `POST /v1/collections/{id}/items` since the last
  import), import **discards the divergence and rewrites items to exactly the
  fixture's order**. This is the highest-stakes semantic decision in the slice —
  it must be documented at the top of the OpenAPI description for the endpoint
  and in `fixtures/README.md`. (Flagged below — Architect/Manager sign-off
  wanted.)
- **All-or-nothing.** The whole import runs in one transaction. One bad
  evaluation `kind` in a 50-request fixture rolls back *everything* — no partial
  state. Best-effort import would leave operators guessing what landed; reject it.

### Export shape

`GET /v1/collections/{id}/export` → 200, `Content-Type: application/json`,
body is a `FixtureBundle` with:

- `version`: current.
- `environments`: **`[]` always.** Export cannot know which environment a
  collection "belongs to" — collections reference `{{VAR}}`s, not env ids; the
  binding only exists per-run. Emitting a guessed env would be wrong; emitting
  all envs would leak unrelated secrets' key names. So: empty list. A
  hand-authored fixture may include `environments`; an export never does. **Flag
  for Architect:** confirm `[]` (not key-omitted) so the round-trip eval's
  canonical form is stable.
- `requests`: every request referenced by the collection's items, **in item
  order**, deduplicated (a request used twice appears once in `requests[]`,
  twice in `items[]`), each with its `evaluations[]` inline (in `created_at`
  then `name` order — deterministic).
- `collections`: exactly the one collection, `items[]` as a list of request
  **name strings** in position order.
- No `id`, `created_at`, `updated_at`, `owner_user_id` anywhere.
- 404 if the collection id doesn't exist or isn't visible to the caller.

### `chess.json` worked example

`fixtures/chess.json` ships two collections. The chess module is an **in-repo**
endpoint surface at `/v1/modules/chess/...` (Architect resolved Manager
open-question #1 this way: `api/modules/chess/`, `python-chess`-backed,
**unauthenticated** — it's the eval *target*, not a protected resource, and
keeping it in-repo makes `chess.json` truly self-contained). The fixture pins
the host with one bundled env var, `CHESS_BASE_URL`, set to the instance's own
origin (`http://localhost:8002` local; the public Vercel URL on staging/prod),
so request `url`s read `{{CHESS_BASE_URL}}/v1/modules/chess/...`.

**Module endpoints the fixture pins (matching the Architect's Chess module HTTP
contract):**

- `POST {{CHESS_BASE_URL}}/v1/modules/chess/legal-moves` — body
  `{ "fen": "<FEN>" }`, `fen` required → `200 { "fen": "<echoed normalized
  FEN>", "legal_moves": ["e2e4","g1f3", …], "turn": "white"|"black",
  "is_game_over": bool }` (UCI long algebraic, sorted). Invalid FEN → `422`.
  Used by `chess-legality`.
- `POST {{CHESS_BASE_URL}}/v1/modules/chess/games` — body `{ "engine_color":
  "white"|"black", "seed"?: <int>, "starting_fen"?: "<FEN>" }` (`seed`
  defaults to `0`, `starting_fen` defaults to startpos) → `200 { "game_id":
  "<uuid>", "fen": "<current FEN>", "turn": "white"|"black", "status":
  "in_progress", "engine_color": "white"|"black", "engine_move": "<uci>"|null,
  "move_history": ["<uci>", …] }` (`engine_move` non-null iff the engine just
  moved, i.e. `engine_color == "white"`). Used by `chess-playable-game` to open
  with `engine_color == "black"`.
- `POST {{CHESS_BASE_URL}}/v1/modules/chess/games/{{GAME_ID}}/moves` — body
  `{ "move": "<uci>" }` → `200` for any *parseable* UCI string: `{ "game_id":
  "<uuid>", "fen": "<current FEN>", "turn": "white"|"black", "status":
  "in_progress"|"illegal_move"|"harness_won"|"harness_lost"|"draw",
  "engine_move": "<uci>"|null, "move_history": ["<uci>", …], "legal_moves":
  ["..."] }`. An illegal-given-the-position move → board unchanged,
  `status: "illegal_move"`, `engine_move: null`, `legal_moves` lists what would
  have been accepted (evidence) — **not a 4xx**. `404` if `game_id` unknown;
  `422` only if `move` is un-parseable as UCI notation. Routing "illegal given
  position" to a body `status` (not a transport error) is deliberate — every
  step's eval is then a uniform `json_path_eq $.status`, never a transport-error
  special case.

**`chess-legality` collection** — N independent requests (no `capture` threading,
order doesn't matter), each posting a known FEN:

```jsonc
{
  "name": "legal-from-startpos",
  "method": "POST",
  "url": "{{CHESS_BASE_URL}}/v1/modules/chess/legal-moves",
  "headers": { "Content-Type": "application/json" },
  "body": { "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1" },
  "evaluations": [
    { "name": "ok",            "kind": "status_eq",     "config": { "expected": 200 } },
    { "name": "e4-is-legal",   "kind": "body_contains", "config": { "needle": "e2e4" } },
    { "name": "echoes-fen",    "kind": "json_path_eq",  "config": { "path": "$.fen", "expected": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1" } }
  ]
}
```

Note: with the current `EvaluationKind` set (`status_eq`, `json_path_eq`,
`header_present`, `body_contains`) we can't do "asserted move ∈ returned
`legal_moves` array" generically — `body_contains` on the UCI string is the
pragmatic stand-in (the shipped fixture hard-codes a FEN and a known-legal move
and asserts `body_contains` on that move). **Flag:** if the Architect wants a
real `move_in_legal_moves` kind, that's a small new `EvaluationKind` + config
model; otherwise `body_contains` is acceptable for the scaffold.

**`chess-playable-game` collection** — an *ordered* sequence; the module holds
game state in `chess_games`, the engine plays Black (picking a **uniformly
random legal move**, RNG seeded by the request-body `seed`), the harness (whose
move-bodies *are* the fixture's `requests[]`) plays White. The fixture pins a
known `seed` so the engine's replies are deterministic and the author can script
a full legal White line; `capture` threads `game_id` forward. (**Inherited flag
from the Architect:** the engine seed comes from the `/games` request *body*, not
from `game_id` — `game_id` is server-assigned, unknowable at authoring time — and
`api/modules/chess/README.md` must document the canonical `seed` + scripted line
`chess.json` relies on, or the per-step `$.fen` evals are meaningless.)

1. **open-game** — `POST …/v1/modules/chess/games` body
   `{ "engine_color": "black", "seed": <documented constant> }`.
   `capture`: `{ "GAME_ID": "$.game_id", "FEN": "$.fen", "TURN": "$.turn" }`.
   evals: `status_eq 200`; `json_path_eq $.turn == "white"` (engine plays
   Black, so after the opening it is White's — the harness's — turn).
2. **white-move-1** — `POST …/v1/modules/chess/games/{{GAME_ID}}/moves` body
   `{ "move": "e2e4" }` (a hardcoded sound move from the scripted line; the
   *point* is the harness under test produces these, but as a fixture they're
   literal). `capture`: `{ "FEN": "$.fen", "STATUS": "$.status" }`. evals:
   `status_eq 200`; `json_path_eq $.status == "in_progress"` (the harness's move
   was legal — it'd be `"illegal_move"` otherwise — and didn't end the game);
   `json_path_eq $.fen == "<expected post-engine-reply FEN from the documented line>"`.
3. **white-move-2 … white-move-K** — same shape, walking the short scripted line.
4. **final** — last request's evals assert `$.status` is `in_progress` or `draw`
   — **never `harness_lost`** (and never `illegal_move`). ("A+C playable": the
   harness end-to-end plays a side without an illegal move and without getting
   mated in the scripted window.)

The turn loop, as request+eval pairs: `open → (move, assert-status-and-fen)* →
assert-final-status`. `capture` carries `GAME_ID` forward (with `FEN`/`STATUS`
along for evidence); the module is the source of truth for the board.

### Error states

- **Unknown evaluation `kind` in a fixture** → `422`, body
  `{ "error": "invalid_evaluation_kind", "request": "<name>", "evaluation":
  "<name>", "kind": "<bad value>", "allowed": ["status_eq","json_path_eq",
  "header_present","body_contains"] }`. Nothing imported (transaction rolled
  back). Operator fixes the kind and re-imports.
- **Bad evaluation `config` shape** (e.g. `status_eq` without `expected`) →
  `422`, `{ "error": "invalid_evaluation_config", "request": "<name>",
  "evaluation": "<name>", "detail": "<pydantic message>" }`. Rolled back.
- **Collection item names a request not in the bundle** → **bundle-only
  resolution** (the Architect adopted this — Designer's lean): every name in
  `items` MUST appear in *this* bundle's `requests[]`. No fall-back to whatever
  request happens to be stored under that name (that would make import
  non-deterministic across instances — the drift this feature exists to kill).
  Unresolved → `422`, `{ "error": "unresolved_request_ref", "collection":
  "<name>", "item_index": N, "request": "<name>" }`. Consequence: a
  "collections-only" fixture (items referencing requests it doesn't declare) is
  illegal — by design. A future opt-in `allow_external_refs` flag could relax
  this if a real need shows up; YAGNI now.
- **Duplicate `name` within one fixture** (two `requests[]` with the same name,
  two `environments[]`, etc.) → `422`,
  `{ "error": "duplicate_name", "kind": "request", "name": "<name>" }`. Reject
  before touching the DB — the bundle is internally inconsistent.
- **Duplicate request name within one collection's `items[]`** is *allowed*
  (a request can appear at multiple positions).
- **Empty fixture** (`{ "version": 1 }` or all arrays empty) → `200` with all
  counts zero, `warnings: ["fixture contained no resources"]`. Not an error —
  makes `import` safe to call defensively.
- **Secret value present in a fixture** (`secrets: { "K": "actual-value" }`) →
  `422`, `{ "error": "secret_value_in_fixture", "environment": "<name>",
  "key": "K" }`. (See bundle-shape note above.)
- **Unknown top-level key / unknown `version` major** → `422`,
  `{ "error": "unsupported_fixture_version", "got": N, "supported": [1] }` or
  `{ "error": "unexpected_top_level_key", "key": "<k>" }`. (Unexpected keys
  *inside* a resource are warnings, not errors — forward-compat for additive
  fields.)
- **Malformed JSON body** → FastAPI's default `422` for a bad request body;
  acceptable.
- **`GET …/export` on a nonexistent/invisible collection id** → `404`
  `{ "error": "collection_not_found" }`.

### Edge cases

- **Round-trip of an empty collection** (no items): export emits
  `requests: []`, `collections: [{name, description, items: []}]`,
  `environments: []`; import accepts it; re-export is identical. ✔
- **A request shared by two collections, both exported separately:** each export
  bundle independently includes that request; importing both is idempotent on
  the second (the shared request reports `updated`, never a duplicate `created`).
- **Concurrent imports of the same fixture** (CI + operator): the per-row upsert
  is `INSERT ... ON CONFLICT (owner_user_id, name) DO UPDATE`; last writer wins;
  both calls return consistent counts (one may see `created`, the other
  `updated`). The collection-items REPLACE happens inside the transaction so
  it's atomic per collection.
- **Very large fixture** (hundreds of requests): single transaction, single
  response with aggregate counts — no streaming, no pagination. If this becomes
  a problem it's a follow-up; flag if the Architect foresees timeout risk on
  Vercel's function limit.
- **Long `name`s / `url`s:** validated by the existing `max_length` constraints
  on the create schemas (`name` 200, `url` 2000); a fixture exceeding them gets
  the same `422` with `request: <name>` context.
- **`body` containing `{{VAR}}` placeholders that no bundled environment
  defines:** not validated at import time (consistent with how requests are
  created today — substitution failures surface at run time, not author time).
  No error.
- **Re-import after manually deleting a collection item via the API:** import
  rewrites the item list from the fixture, so the deletion is undone — this is
  the declarative-desired-state contract, and the OpenAPI doc says so.
- **Stripping volatile fields the operator left in by accident** (e.g. copied an
  API response into a fixture): `id`/`created_at`/`updated_at`/`owner_user_id`
  are silently ignored with a `warnings[]` entry — don't 422 on them, that'd
  punish the obvious "export, tweak, re-import" loop.

---

**Flags for Architect / Manager (status as of reconciliation):**

- *(highest stakes — RESOLVED)* Import **replaces** a collection's `items[]` and
  a request's `evaluations[]` — declarative desired-state, divergence is
  discarded. Architect confirms; **must be documented loudly** at the top of the
  `POST /v1/fixtures/import` OpenAPI description and in `fixtures/README.md` —
  this is the one behavior that surprises operators (a re-import undoes any item
  or eval they added via the resource APIs since the last import).
- *(RESOLVED)* Export emits `environments: []` (never bundles an env, never
  key-omits it). Architect confirms — keeps the round-trip normal form stable.
- *(RESOLVED)* `requests` gains `UNIQUE (owner_user_id, name)` (Architect, in
  Integrations); side effect: `POST`/`PATCH /v1/requests` can now 409 — mirrored
  on those routes.
- *(RESOLVED — `unchanged`/`deleted` dropped)* `FixtureImportResponse` is
  `{created, updated}` per kind + a top-level `warnings: list[str]`. Designer's
  earlier draft had `unchanged` + `deleted`; Architect's simpler shape wins
  (`created: 0` is the idempotency signal). **Residual UX cost, accepted:** the
  response gives operators no number for "this import silently dropped N stale
  evaluations / items" — the loud OpenAPI doc on declarative-replace is the only
  surfacing of that. Designer flags it but does not block.
- *(RESOLVED)* Secrets in a fixture = keys-only with `""` values; non-empty value
  → hard `422` at parse time; import preserves the target env's existing
  ciphertext for a known key, writes `encrypt("")` as an unset placeholder for a
  new key. Architect adopted this; the earlier "plaintext, importer encrypts"
  sketch is gone.
- *(RESOLVED)* Request-ref resolution: **bundle-only** (Architect adopted
  Designer's lean). A collections-only fixture is illegal, by design.
- *(OPEN — optional)* A `move_in_legal_moves` `EvaluationKind` for the chess
  fixture instead of `body_contains` against a UCI string — small new
  `EvaluationKind` + config model; not blocking, `body_contains` is acceptable.
- *(OPEN — Designer ask)* The error bodies in **Error states** above all use a
  rich `{ "error": "<code>", ...context }` shape; the Architect's Contracts
  section doesn't declare a `FixtureImportError` Pydantic model for them. Either
  model it (so `/docs` shows the 422 shapes) or explicitly say errors ride
  FastAPI's default `HTTPException` `{"detail": ...}` envelope with the code
  string in `detail`. Designer's preference: model it.

AGREE: yes

## Architect

### Components

**`api/fixtures/` — new resource module (routes + schemas + engine).**
- `api/fixtures/schemas.py` — the authoritative Pydantic models for the fixture bundle (`FixtureBundle`, `FixtureEnvironment`, `FixtureRequest`, `FixtureEvaluation`, `FixtureCollection`) plus the import-response model (`FixtureImportResponse`) and the canonical-form normalizer. Boundary: pure data shapes + validation; no DB, no FastAPI.
- `api/fixtures/engine.py` — the import/export logic. Same convention as `api/runs/engine.py`: **MUST NOT import any `fastapi.*` symbol.** Takes an `asyncpg.Connection` (already inside a transaction, opened by the route), an owner id, and a `FixtureBundle`; returns a `FixtureImportResponse`-shaped result. Also the inverse: `build_export(conn, collection_id) -> FixtureBundle`. The route layer owns the transaction, HTTP status mapping, and `record_event`. Keeping the engine FastAPI-free is worth it here — the import path is non-trivial (3 resource types, name resolution, child-replace) and is exercised directly by the pytest round-trip test without spinning up the app.
- `api/fixtures/routes.py` — `APIRouter(prefix="/v1/fixtures")`, mounted in `api/main.py` alongside the others. Exactly one route: `POST /v1/fixtures/import`. Auth: `require_user` (operator-only authoring, mirrors collections/requests/environments). Wraps `engine.import_bundle` in a single `async with auth.db.transaction():`, then `record_event(... kind="fixtures.imported" ...)` on success.

**Export route placement — sub-route on collections, NOT on fixtures.** `GET /v1/collections/{collection_id}/export` lives in `api/collections/routes.py`. Rationale: it's a *projection of a collection* (the resource is `/v1/collections/{id}`), it needs the same 404-on-missing-collection behavior already implemented there, and the existing pattern is "operations on a collection hang off `/v1/collections/{id}/...`" (cf. `/items`). Putting it under `/v1/fixtures/...` would force the id into a query param or a second path segment with no resource of its own. The handler is thin: `require_user`, 404 if collection missing, then `return await fixtures.engine.build_export(auth.db, collection_id)` with `response_model=FixtureBundle`. (It imports the fixtures engine — a leaf dependency, no cycle: `collections.routes -> fixtures.engine`, and `fixtures.engine` imports nothing from `collections`.)

**`api/modules/chess/` — new "module" namespace (the eval *target*, not an eval primitive).**
- `api/modules/__init__.py`, `api/modules/chess/routes.py` (`APIRouter(prefix="/v1/modules/chess")`), `api/modules/chess/schemas.py`, `api/modules/chess/engine.py` (the `python-chess` wrapper: legal-move enumeration + the trivial "engine plays one side" mover; FastAPI-free). New top-level `api/modules/` package signals "things-under-test hosted in-repo" and leaves room for future eval modules without polluting the resource namespace. Mounted in `api/main.py` with `tags=["Modules: chess"]`.
- New table `chess_games` in `api/shared/schema.sql` (see Integrations) — game state must survive a serverless cold start / uvicorn restart mid-run, so it is NOT in-memory.

**`fixtures/` — checked-in fixture files (repo root, not under `api/`).** Plain data, sibling to `api/` and `mobile/`. First and only file this slice: `fixtures/chess.json`.

### Contracts

#### Fixture bundle JSON (the file format = `FixtureBundle` Pydantic model)

```jsonc
{
  "version": 1,                      // int, required, == 1; importer rejects anything else (422)
  "environments": [                  // optional, default []
    {
      "name": "chess-local",
      "variables": { "CHESS_BASE_URL": "http://localhost:8002" },   // the instance's own origin
      "secrets": { }                 // KEYS ONLY; every value MUST be "" — non-empty → 422 (see secrets rule). chess.json needs none.
    }
  ],
  "requests": [                      // optional, default []; names unique within the bundle
    {
      "name": "chess-legality-probe",
      "method": "POST",
      "url": "{{CHESS_BASE_URL}}/v1/modules/chess/legal-moves",
      "headers": { "Content-Type": "application/json" },
      "body": { "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1" },  // any JSON, or null
      "capture": { },                // var_name -> JSONPath-lite ($-prefixed), same rules as requests.capture
      "evaluations": [
        { "name": "move-is-legal", "kind": "body_contains", "config": { "needle": "e2e4" } }
      ]
    }
  ],
  "collections": [                   // optional, default []; names unique within the bundle
    {
      "name": "chess-legality",
      "description": "Harness returns a legal move for a FEN.",
      "items": [ "chess-legality-probe" ]   // ARRAY of request *names*; array index == position (0..n-1)
    }
  ]
}
```

Model rules (enforced at parse time → 422 before any DB write):
- `version: int` — required, must equal `1`. Forward-compat hook; a future shape bumps it and the importer dispatches. (Recommend yes — costs one field, buys a clean migration story; SPEC's "out of scope: versioned/migratable schema" is about *migration tooling*, not about reserving the field.)
- `FixtureEvaluation { name: str(1..200), kind: EvaluationKind, config: dict }` — `config` validated through the **existing** `api/evaluations/schemas.py` `_CONFIG_VALIDATORS[kind]` via a `model_validator(mode="after")`, identical to `CreateEvaluationRequest`. A bad `kind` or malformed `config` is a 422 at import, never a stored no-op row. (Reuse, do not re-declare, the per-kind config models.)
- `FixtureRequest` reuses the `requests/schemas.py` constraints: `method` regex `^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$`, `url` 1..2000, `capture` validated by `_validate_capture_dict` (import it). `body: Any = None`.
- `FixtureEnvironment.secrets: dict[str, str]` — **keys only, every value MUST be the empty string `""`.** A non-empty value anywhere → **422 at parse time** (`{"error": "secret_value_in_fixture", "environment": "<name>", "key": "<k>"}`), before any DB write. Resolves the Designer's keys-only annotation vs. the earlier "plaintext, importer encrypts" sketch: **keys-only-with-blank-values wins** — a fixture is a git-tracked artifact and must never carry decrypted secret material, and Fernet ciphertext is non-deterministic so it could never round-trip anyway. On import, per key with an empty value: if the target environment already has that key, **preserve its existing ciphertext untouched**; if the key is new, store it with an empty-string ciphertext placeholder (`encrypt("")`) for the operator to fill in later via the existing `PATCH /v1/environments/{id}`. `environments` is always `[]` on export, so this asymmetry only affects hand-authored fixtures and the 422 is the guardrail. (`api/environments/schemas.py` `CreateEnvironmentRequest.secrets` keeps accepting plaintext for the *direct* `POST /v1/environments` path — unchanged; the keys-only rule is a property of the *fixture* model, not of environments.)
- `FixtureCollection.items: list[str]` — request names. **Bundle-only resolution: every name in `items` MUST appear in this bundle's `requests[]`, else 422.** Rationale: a fixture is a *self-contained declarative artifact* (SPEC: "self-contained", "no external deploy to stand up before the fixture works"). Falling back to "whatever request with that name happens to already be in the DB owned by this operator" makes import non-deterministic across instances — exactly the drift the feature exists to kill. Consequence: a "collections-only" fixture is illegal; that's the right call. (A future slice can add an opt-in `allow_external_refs` flag if a real need shows up; YAGNI now.)
- Name uniqueness is **per resource type** within the bundle (a request and a collection may both be named `chess-legality`). Cross-type collisions are harmless because they upsert into different tables on different unique keys; SPEC open question #3 resolved: per-type.
- Empty bundle (`{"version":1}`) is valid and a no-op (all counts zero).

#### `POST /v1/fixtures/import`
- Auth: `require_user` → 403 for harness keys / anonymous.
- Body: `FixtureBundle` (the JSON above). Malformed → 422 with field path.
- Success → `200` with `FixtureImportResponse`:
  ```json
  {
    "environments": { "created": 1, "updated": 0 },
    "requests":     { "created": 3, "updated": 0 },
    "evaluations":  { "created": 4, "updated": 0 },
    "collections":  { "created": 2, "updated": 0 },
    "warnings": [
      "environments[0]: ignored unexpected field 'id'",
      "fixture contained no resources"
    ]
  }
  ```
  - `warnings: list[str]` — present always (empty list when nothing to say). Carries non-fatal observations: a volatile/server-assigned field left on a resource object by an export-tweak-reimport loop (`id`, `created_at`, `updated_at`, `owner_user_id`, `request_id`, `collection_id`) is **silently dropped with a warning, never a 422** (don't punish the obvious loop); an empty bundle emits `["fixture contained no resources"]`. Unknown fields *inside* a resource → warning (forward-compat for additive fields); an unknown *top-level* key or unknown `version` major → 422 (those are structural). (Note: there is no `position` field anywhere in the format — collection `items` is a `list[str]` of request names; an item given as an object is a plain type error → 422, not a warning.) This matches the Designer's `warnings[]` annotation — adopting it into the contract.
  - `created` = rows that did not exist (by unique key) before this import; `updated` = rows that existed and were touched (whether or not values actually changed). **No `unchanged` bucket and no `deleted` bucket this slice.** `unchanged` needs a per-row before/after value diff for a number operators don't act on; "re-import reports `created: 0`" is the idempotency signal SPEC's success criteria ask for and `created`/`updated` already give it. `deleted` would be actively misleading: evaluations and collection items use **delete-all-children-then-reinsert**, so a "deleted" count would equal the entire child population of every touched parent on every re-import — noise, not signal. For `evaluations` and `collection_items`, the count is reported **against the parent**: an evaluation set is `created` if its request was created, `updated` if its request already existed (same for items vs. their collection). (Document this so the count is reproducible.) The locked response shape is `{created, updated}` per resource type + a top-level `warnings: list[str]`; a re-import shows `created: 0` (and `updated` ≥ 0) — that's the idempotency signal. (The Designer's prose is consistent with this.)
- Errors: 422 (bad bundle, unresolved item name, bad eval config, secret value present in a fixture, unknown top-level key, unknown `version` major), 403 (not operator), 500 (unexpected) — and the transaction rolls back, so a 422/500 leaves the DB exactly as before.
- **Error envelope: FastAPI's default `HTTPException` `{"detail": ...}` shape — NO dedicated `FixtureImportError` Pydantic model.** Every 422 case carries enough context inside `detail` (a plain string or a small dict, e.g. `{"error": "secret_value_in_fixture", "environment": "<name>", "key": "<k>"}` or `"collection 'chess-legality' item 'chess-legality-probe' not found in bundle requests[]"`); Pydantic body-validation 422s already emit the standard `{"detail": [{"loc": ..., "msg": ...}]}` list and we don't override that. There is **no** `FixtureImportError` schema for the builder to declare — raise `HTTPException(status_code=422, detail=...)` from the route after the engine signals a validation failure. (The Designer's Surfaces list mentions `FixtureImportError` informally; this contract is authoritative: default envelope only. The illustrative `{"error": ...}` dicts elsewhere in this section are the *value* of `detail`, not a separate response_model.)
- **REPLACE semantics for children — CONFIRMED (the Designer's "highest-stakes" flag, resolved).** Import is *declarative desired-state*, not a merge. For each request the bundle declares, its `evaluations[]` **fully replace** that request's stored evaluation set — rows present in the DB but absent from the fixture are deleted (via the per-parent delete-all-then-reinsert). For each collection the bundle declares, its `items[]` **fully replace** that collection's stored item list — items added out-of-band via `POST /v1/collections/{id}/items` since the last import are discarded and order is rewritten to exactly the fixture's array order. This must be the first sentence of the endpoint's OpenAPI description and the headline of `fixtures/README.md`. Caveat: top-level `environments`/`requests`/`collections` are *upserted*, not replaced — import never deletes a request or collection the bundle doesn't mention; only *children of mentioned parents* get the replace treatment. (No deletes-on-import at the top level — matches the Manager's out-of-scope line.)
- **All-or-nothing.** The whole import runs inside one `async with auth.db.transaction():`. One bad eval `kind` in a 50-request bundle rolls everything back — no partial state.
- Records `events` row: `kind="fixtures.imported"`, `target_kind="fixtures"`, `target_id=NULL`, `payload={counts...}`.

#### `GET /v1/collections/{collection_id}/export`
- Auth: `require_user` → 403 otherwise. (SPEC open question #4 resolved: operator-only; export is an authoring action and may surface request bodies/headers that include `{{SECRET}}` placeholders — fine, those are placeholders not values — but "authoring = operator" holds.)
- 404 if the collection doesn't exist.
- `200` with `FixtureBundle`: `version: 1`, `environments: []` (a collection has no canonical environment — see below), `requests: [...]` (every distinct request referenced by the collection's items, in *first-referenced* order), `collections: [{ name, description, items: [request names in position order] }]` (exactly one collection).
- `environments` is always `[]` on export — *stated explicitly*: which environment a collection runs against is a run-time binding (`runs.environment_id`), not a property of the collection, and environments hold secrets we will not dump. An operator who wants the env in a fixture authors it by hand.
- A collection with zero items exports `requests: []` and `collections: [{... "items": []}]` — round-trips fine.
- `Content-Type: application/json`, served inline (no `Content-Disposition`) — consistent with the Designer's Surfaces note.

#### Canonical serialization + the round-trip normalization function

The acceptance contract — "export → import → export is identical modulo timestamps/ids" — is pinned to this exact normalization, applied to **both** export bundles before comparison. It lives in `api/fixtures/schemas.py` as `normalize_for_roundtrip(bundle: dict) -> dict` and is the single source of truth used by both the pytest test and the `platform/evals/seeds/` seed:

1. Drop server-assigned fields wherever they appear: `id`, `request_id`, `collection_id`, `owner_user_id`, `created_at`, `updated_at`, `position` (positions are implied by array order; ids/timestamps are non-deterministic).
2. `environments`: dropped entirely from the comparison (export emits `[]`, import of a collection-only bundle ignores absent envs — symmetric).
3. `requests[].headers`, request `body` objects, evaluation `config`, environment `variables`: left as-is (Python dicts compare order-insensitively; a `body_eq`-style assertion that serializes to JSON must `json.dumps(..., sort_keys=True)`).
4. `requests[]` sorted by `name`; `collections[]` sorted by `name`; `evaluations[]` within a request sorted by `name`; collection `items` kept in array order (order is meaningful).
5. `version` copied through (must be `1` on both sides).

Because export already emits requests in first-referenced order and the importer assigns `collection_items.position` densely from array index, a clean round-trip needs only steps 1, 4, 5 in practice — but the normalizer is defined to be robust to authored-by-hand input too.

Key emission order in the export response (FastAPI serializes Pydantic field order — just declare the models this way, no custom encoder): top level `version, environments, requests, collections`; within a request `name, method, url, headers, body, capture, evaluations`; within an evaluation `name, kind, config`; within a collection `name, description, items`. Gives stable, diff-friendly JSON for the checked-in fixture.

#### Chess module HTTP contract (the eval target — must be drivable by the run engine: request templates + `{{VAR}}` + `capture` only, zero client-side logic)

Mounted at `/v1/modules/chess` (component decision above). In `chess.json` the request templates use **one** environment variable for the host — `CHESS_BASE_URL` — so the URLs read `{{CHESS_BASE_URL}}/v1/modules/chess/legal-moves` etc. (The full `/v1/modules/chess/...` prefix is part of the contract — request templates carry it literally; `CHESS_BASE_URL` is host-only. Pick `CHESS_BASE_URL` not `BASE_URL` so the fixture's intent is self-documenting; the bundled `chess-local` environment sets it to the instance's own origin — `http://localhost:8002` for local dev, the public Vercel URL when an operator runs it against staging/prod.)

1. **`POST /v1/modules/chess/legal-moves`** — stateless legality oracle.
   - Body `{ "fen": "<FEN string>" }`, `fen` **required** (no default — keeps the contract honest). Invalid FEN → `422` with detail.
   - `200` → `{ "fen": "<echoed normalized FEN>", "legal_moves": ["e2e4", "g1f3", ...], "turn": "white"|"black", "is_game_over": bool }` — moves in **UCI long algebraic** (`e2e4`, `e7e8q`), sorted. (UCI not SAN: SAN needs board context to disambiguate and is harder for a harness to produce mechanically; and `body_contains` on the JSON array text gives a clean "is this move legal?" check.)
   - How `chess-legality` uses it: the shipped fixture hard-codes a FEN and a known-good move, evaluates `body_contains { "needle": "<that move>" }` against the response (whose `legal_moves` includes it). SPEC says "eval checks the move is in `legal_moves`" — that's exactly this. (A "harness supplies the move" variant would need a second request to POST the move somewhere; not needed for the legality collection.)

2. **`POST /v1/modules/chess/games`** — start a server-driven game.
   - Body `{ "engine_color": "white"|"black", "seed": <int>?, "starting_fen": "<FEN>"? }`. `seed` defaults to a fixed constant (`0`) — see the playable-game note below for why it's in the body. `starting_fen` defaults to the standard start position. If `engine_color == "white"`, the server immediately makes the first move.
   - `200` → `{ "game_id": "<uuid>", "fen": "<current FEN>", "turn": "white"|"black", "status": "in_progress", "engine_color": "white"|"black", "engine_move": "<uci>"|null, "move_history": ["<uci>", ...] }` (`engine_move` non-null iff the engine just moved, i.e. `engine_color == "white"`).
   - The "engine" is deliberately trivial: **pick a uniformly random legal move**, RNG seeded by the request-body `seed`. It does NOT need to play well, only legally — SPEC: "validates the harness's side end-to-end", not "beats the harness".

3. **`POST /v1/modules/chess/games/{game_id}/moves`** — submit the harness's move; server replies.
   - Body `{ "move": "<uci>" }`.
   - `200` always for any *parseable* UCI string (no 4xx for an illegal-given-the-position move) → `{ "game_id": "<uuid>", "fen": "<current FEN>", "turn": "white"|"black", "status": "in_progress"|"illegal_move"|"harness_won"|"harness_lost"|"draw", "engine_move": "<uci>"|null, "move_history": ["<uci>", ...], "legal_moves": ["..."] }`.
     - Illegal-given-position harness move → board unchanged, `status: "illegal_move"`, `engine_move: null`, `legal_moves` lists what *would* have been accepted (evidence).
     - Legal harness move that ends the game (checkmate by harness / stalemate) → `status: "harness_won"` / `"draw"`, `engine_move: null`.
     - Otherwise the server applies the harness move, makes its own random legal reply; if *that* ends the game → `"harness_lost"` / `"draw"`; else `"in_progress"`.
   - `404` if `game_id` unknown. `422` only if `move` is *un-parseable* as UCI notation (not "illegal given the position" — that's `illegal_move` in the body). Routing "illegal given position" to a body `status` field makes the playable-game collection's evals uniform: every step asserts `json_path_eq $.status == "in_progress"` (or the terminal value on the last step), never a transport error.
   - **How `chess-playable-game` is built as a run collection:** request 1 POSTs to `/games` with a known `seed` and `engine_color == "black"`, `capture`s `$.game_id` into `GAME_ID`; requests 2..n each POST to `/games/{{GAME_ID}}/moves` with a *hard-coded* harness (White) move from a *fixed scripted line*. The line is authorable precisely because the engine's RNG is seeded by the body's `seed` (NOT by `game_id`, which is server-assigned and unknowable at authoring time) — with a known seed the engine's (Black) replies are deterministic, so the author scripts a full legal line and each step's eval asserts the expected post-engine-reply `$.fen` / `$.status`. **This is the one subtle bit of the contract — flag for the builder: the engine seed comes from the request body, defaults to a constant, and the shipped `chess.json` relies on a documented seed+line.**

   Auth on the chess module: **unauthenticated.** Deliberate — these endpoints hold no secrets, only ephemeral game state; they exist to be hit by the run engine, which does not auto-inject auth (it sends exactly the headers in the request template). Requiring auth would force every chess request template in `chess.json` to carry a `{{HARNESS_KEY}}` header sourced from an environment in the bundle — more moving parts, and `chess.json` couldn't run without first authoring that environment with a live key. A one-line comment in `api/modules/chess/routes.py`: "intentionally unauthenticated — eval target, not a protected resource." Not a precedent for the resource APIs.

### Integrations

- **`requests` table — ADD a unique constraint `UNIQUE (owner_user_id, name)`.** "Upsert by name" demands it; without it `INSERT ... ON CONFLICT` has no arbiter and "re-import is idempotent" is impossible. Idempotent DDL in `api/shared/schema.sql` (Postgres has no `ADD CONSTRAINT IF NOT EXISTS`, so a `DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'requests_owner_user_id_name_key') THEN ALTER TABLE requests ADD CONSTRAINT requests_owner_user_id_name_key UNIQUE (owner_user_id, name); END IF; END $$;` block, alongside the existing `ADD COLUMN IF NOT EXISTS capture`). The deployed DB is freshly scaffolded (slice-2 just landed, `runs` is a stub) — no duplicate-named rows to block the constraint, so plain `ADD CONSTRAINT` (not `NOT VALID` + later `VALIDATE`) is fine; PLAN should note that against a *dirty* DB a dedupe pass would be required first. Side effect: **`POST /v1/requests` (`api/requests/routes.py::create_request`) gains a `try/except asyncpg.UniqueViolationError → HTTPException(409)` wrapper around the INSERT.** Correction to an earlier draft of this section: there is **no `PATCH /v1/requests/{id}` route today** (the `UpdateRequestRequest` schema exists, the route doesn't) — nothing to change there. And this is **new** behavior, not a "mirror" — `POST /v1/collections` / `POST /v1/environments` / `PATCH /v1/environments/{id}` currently do *not* catch `UniqueViolationError` on `(owner_user_id, name)` either (they 500 on a duplicate name). Tightening those is out of scope for this slice; only `POST /v1/requests` is touched, because this slice is what introduces the constraint that makes the violation reachable. PLAN: list the `create_request` 409 catch as a discrete change.
- **`environments`, `collections`** — already `UNIQUE (owner_user_id, name)`. Import uses `INSERT ... ON CONFLICT (owner_user_id, name) DO UPDATE SET ... RETURNING (xmax = 0) AS inserted` to get the created/updated flag in one round trip (`xmax = 0` ⇒ this was an INSERT, not an UPDATE — standard asyncpg trick).
- **`evaluations`, `collection_items`** — no unique constraint, and we don't add one; both are *children* fully owned by their parent in the declarative model, so import does **delete-all-children-then-bulk-insert** per parent. We delete by `request_id` / `collection_id` explicitly inside the txn (the parent row survives — not relying on cascade). `collection_items.position` assigned `0..len(items)-1` from array index.
- **`api/shared/secrets.py`** — `FixtureEnvironment.secrets` in the file are **keys-only with `""` values** (a non-empty value is rejected at parse time, 422 — see Contracts). On import the engine reads the *target* environment's existing `secrets` JSONB and, per fixture key: keeps the existing ciphertext if the key is already there; else writes `encrypt("")` as an unset placeholder. The `encrypt`/`decrypt` helpers are reused as-is; `decrypt` is never invoked on the import path, and export emits `environments: []`, so no secret material ever traverses the fixture boundary in either direction. (The "preserve existing ciphertext" half is trivially implementable: it's a dict merge over the loaded `secrets` JSONB before the UPDATE — no decrypt involved.)
- **`api/shared/schema.sql` — new `chess_games` table** (small). Authoritative DDL lives in `api/shared/schema.sql` (and matches the chess-contract section above): columns `id`, `engine_color TEXT` (`CHECK IN ('white','black')`), `seed BIGINT` (engine RNG seed from the request body, default 0), `starting_fen TEXT` (FEN the game opened from, for replay), `fen TEXT` (current position), `move_history JSONB` (ordered UCI moves played, evidence + per-ply RNG replay), `move_count INTEGER` (== `jsonb_array_length(move_history)`), `status TEXT` (`CHECK IN ('in_progress','harness_won','harness_lost','draw')` — `illegal_move` is a transient response status, never persisted), `created_at`, `updated_at`. No owner column — games are unauthenticated and ephemeral; a slice-N reaper can `DELETE WHERE created_at < now() - interval '1 day'` (not this slice; note it).
- **`api/runs/engine.py`** — unchanged. The chess collections are ordinary collections of ordinary requests; the engine already does `{{VAR}}` substitution and `capture`. The contract above is shaped *around* the engine's existing capabilities — notably: only `capture` can carry state between requests, so `game_id` MUST be a JSONPath-addressable top-level field in the `/games` response. If any engine change turns out to be needed, the chess contract is wrong, not the engine.
- **`requirements.txt` AND `api/requirements.txt`** — add `chess==1.11.2` (pin the current stable; builder confirms exact version at implementation time) to **both** files, kept in sync per the comment at the top of `api/requirements.txt` (root file = dev/test superset; `api/` file = what Vercel bundles). `python-chess` is pure-Python, no native build — safe in the serverless function.
- **`api/main.py`** — two new `include_router` lines: `fixtures_router` (`tags=["Fixtures"]`) and the chess router (`tags=["Modules: chess"]`). One new line in `api/collections/routes.py` for the `/export` sub-route.
- **`api/shared/events.py`** — `record_event` called once per successful import (`fixtures.imported`). The chess module is the thing-under-test, not an authoring surface — it does **not** emit events (noise; the run that drives it already emits `run.*`).
- **`platform/evals/seeds/` (separate repo, `~/github/pagehub-io/platform`)** — the round-trip *eval* (the SPEC's "checked-in eval asserting export→import→export identity") lands as `platform/evals/seeds/pagehub_evals_fixtures.py`: POST `fixtures/chess.json` to `/v1/fixtures/import`, GET `/v1/collections/{chess-legality}/export`, POST that back to `/v1/fixtures/import`, GET `/export` again, assert `normalize_for_roundtrip(a) == normalize_for_roundtrip(b)` in that layer's assertion vocabulary. Plus a *unit*-level guard inside this repo: `api/tests/test_fixtures_roundtrip.py` (pure pytest, no live server — drives `fixtures.engine` against a test DB, asserts the same equality). This split is intentional — SPEC says "eval coverage goes in `platform/evals/seeds/` as before" AND "add an eval that asserts exactly this": the platform seed is the *eval*, the pytest is the *fast regression guard*. **The platform seed is a sibling PR in a different repo; flag it in PLAN, do not block the pagehub-evals PR on it.**

### Trade-offs

- **`requests` gets `UNIQUE (owner_user_id, name)`** — chose this over "name-uniqueness scoped to within-a-single-import-bundle". The bundle-scoped alternative would let `POST /v1/requests` keep creating duplicate-named rows and then import would have to *guess* which one to update (or error on ambiguity), and `GET /collections/{id}/export` becomes ambiguous (which `chess-legality-probe` did the item reference?). The constraint is the simpler invariant. Accepted risk: a populated DB with pre-existing duplicate names blocks the migration — not a concern here (fresh scaffold), but PLAN notes that against a dirty DB a dedupe step is needed first. Also accepted: `POST /v1/requests` can now 409 on a duplicate name — a behavior change for an endpoint with essentially no real callers yet; `create_request` gets a `try/except asyncpg.UniqueViolationError → 409`. (No `PATCH /v1/requests/{id}` exists, so nothing else to touch; the sibling `POST`s on collections/environments don't currently catch this and we're not changing them in this slice.)
- **Bundle-only request-name resolution** (reject items referencing requests not in the bundle) over "fall back to stored requests" — chose determinism/self-containment over flexibility. A "collections-only" fixture becomes illegal; that's a feature, because the fallback makes the same fixture produce different results on different instances depending on what's already there — the exact failure mode this feature exists to prevent.
- **`version: 1` field reserved now** vs. omit-it-YAGNI — chose to reserve. One field, zero cost, and it's the difference between "we can evolve the format" and "every consumer breaks the day we need to". SPEC's "no versioned/migratable schema" is about not building *migration tooling* this slice — reserving the discriminator is orthogonal and cheap.
- **Array index == `collection_items.position`** vs. explicit `position` ints in the file — chose array index. Positions in a fixture are always dense `0..n-1`; an explicit field invites the file to disagree with itself (gaps, dupes, out-of-order) and forces validation for no expressive gain. The importer assigns positions; export emits items in position order; the array *is* the order.
- **No `unchanged` and no `deleted` count** in the import response — chose `{created, updated}` + `warnings` only. `unchanged` needs a per-row before/after value comparison; operators act on "did anything get created" (drift) and "is it idempotent" (`created: 0` on re-run), both answered by `created`+`updated`. `deleted` would be misleading given delete-all-then-reinsert for children. Add either later if someone actually wants it.
- **Secrets in a fixture = keys-only with `""` values; non-empty value = hard 422; import preserves existing ciphertext / writes an unset placeholder for new keys** — chose this (the Designer's annotation) over "plaintext secret values, importer Fernet-encrypts" (an earlier sketch in this section). A fixture is a git-tracked declarative artifact: it must never carry decrypted secret material, full stop. And it couldn't round-trip anyway — Fernet ciphertext is non-deterministic, so an `export → import → export` of a bundle with real secrets would never be byte-stable. The cost is a one-directional asymmetry (a hand-authored fixture can name secret *keys* but never *fill* them; the operator finishes the job via `PATCH /v1/environments/{id}`) — acceptable, and export emitting `environments: []` means the asymmetry only ever bites hand-authored bundles, with the 422 as the guardrail.
- **Chess engine = uniformly-random legal move, seeded via a request-body `seed` (default 0)** over (a) `game_id`-seeded (un-authorable: `game_id` is server-assigned) or (b) a real engine (overkill, slow, non-deterministic, and "play *legally*" is the whole bar). The seeded-RNG-from-body choice is the only one that lets a fixture author script a full legal line and assert each `$.fen`. Cost: the chess README must document the canonical seed/line used by `chess.json`, and "seed in body" is a slightly non-obvious bit of the contract — flagged for the builder.
- **`chess_games` table** over in-memory dict — chose durable. Serverless cold starts and `uvicorn --reload` would otherwise drop a game mid-`chess-playable-game` run and the run engine would see a `404` halfway through (→ `error` verdict, flaky eval). The table is tiny and pays for itself the first time a deploy lands during a run. Accepted: it accretes rows until a reaper exists (slice-N) — bounded, harmless, noted.
- **Chess module unauthenticated** over auth-via-`{{HARNESS_KEY}}`-from-environment — chose unauthenticated. The run engine doesn't auto-inject auth, so auth here would mean every chess request template carries an env-sourced key header and `chess.json` can't run without first authoring that environment with a live key — friction with no security benefit (no secrets behind these endpoints). Documented as a deliberate "eval target, not a protected resource" decision; not a precedent for the resource APIs.
- **Export omits `environments`** (`always []`) over "dump the environments the collection has been run against" — a collection has no canonical environment (it's a run-time binding on `runs`, possibly several), and environments hold Fernet secrets we will not export. An operator who wants the environment in a fixture writes it by hand. Stated in the contract so the round-trip eval doesn't expect them.
- **Single all-or-nothing transaction** for import (`async with auth.db.transaction():` around the whole engine call) over per-resource commits — a partially-applied fixture is worse than a cleanly-rejected one; "import is idempotent" implies "import is atomic". asyncpg's connection-scoped transaction is exactly the tool. Cost: a huge bundle holds one connection for the duration — fine at this scale (`chess.json` is a handful of rows; pool size is 10).

AGREE: yes
NIT: add `api/modules/chess/README.md` (or `fixtures/README.md` §chess) documenting the canonical engine `seed` + scripted move line for `chess-playable-game` — PLAN should assign an owner; the fixture's per-step `$.fen` evals are meaningless without the documented line.
NIT (builder note): "engine RNG seeded by the body `seed`" needs per-ply determinism, not just per-game — each `/games/{id}/moves` call reconstructs the engine's choice freshly, so the engine must derive its move from `(seed, ply_index)` (e.g. `random.Random(hash((seed, ply)))` over the sorted legal-move list, or replay the stored `moves` history to re-advance one RNG) rather than `random.Random(seed)` once. Implementation detail; flagging so it isn't discovered the hard way when the scripted line doesn't reproduce.
