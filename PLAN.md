# PLAN

> Build: pagehub-evals — fixture import/export. Read `SPEC.md` first (Manager / Designer / Architect sections are the contract).

## Security

### Threat model
- **Malicious / careless eval author** (can author fixtures, has an operator JWT): commits a fixture with a live secret value; crafts a fixture that creates 100k requests / a 5000-item collection; crafts a fixture whose request templates point outbound at an arbitrary host; tries to clobber another operator's resources via upsert-by-name.
- **Operator A vs operator B** (both hold operator JWTs, different `actor_id`): A imports a bundle that upserts/overwrites B's named requests/collections/environments; A exports B's collection.
- **Anonymous / unauthenticated internet caller**: hits the chess module (it's unauthenticated by SPEC decision); spams `POST /v1/modules/chess/games` to fill `chess_games`; feeds malformed FEN / UCI strings hoping for a 500 + stack trace; tries to enumerate `chess_games` rows.
- **Harness key holder** (run-execution credential, not an operator): tries to call `POST /v1/fixtures/import` or `GET /v1/collections/{id}/export` — must get 403 (`require_user` only).
- **Run engine as confused deputy**: a fixture's request templates point `{{CHESS_BASE_URL}}/...` at an attacker host and the engine fires them (SSRF). Same surface as `POST /v1/requests` today; fixtures don't widen it.

### Authn / Authz
- `POST /v1/fixtures/import`: `require_user` (operator JWT, slug-matched to `pagehub-evals`) — same gate as `POST /v1/requests` / `POST /v1/collections` / `POST /v1/environments` (`api/requests/routes.py:55`, `api/collections/routes.py:53`, `api/environments/routes.py:57`). Harness keys and anonymous → 403 via `require_user` (`api/dependencies.py:132`). Imported rows get `owner_user_id = auth.actor_id`.
- `GET /v1/collections/{id}/export`: `require_user`. **Matches the existing `GET /v1/collections/{id}` read pattern** (`api/collections/routes.py:94` — any operator can read any collection; there is no `owner_user_id` filter in the SELECT). I agree with the recommendation: keep export as broad as the existing collection read — tightening *one* read path while `GET /{id}`, `GET ""`, and `GET /{id}/items` stay broad would be inconsistent security theater (an operator who can `GET /{id}` already sees every request body/header/eval the export would dump). If the team ever wants per-owner collection isolation, that's a separate cross-cutting change to every collections read route, flagged here, not smuggled into export. 404 (not 403) when the collection id doesn't exist — no existence oracle distinction needed since all operators can read all collections anyway.
- Chess module (`POST /v1/modules/chess/legal-moves`, `/games`, `/games/{id}/moves`): **intentionally unauthenticated** (Architect/SPEC decision — eval target, not a protected resource). Audited below.

### Data exposure
- **Secrets never cross the fixture boundary in either direction.**
  - *Import:* `FixtureEnvironment.secrets` is keys-only — every value MUST be `""`; a non-empty value is a hard 422 at *parse time* (`{"error":"secret_value_in_fixture","environment":...,"key":...}`), before any DB write. So a git-committed fixture physically cannot carry live credential material — the model rejects it. On import, per key: existing key → preserve the target environment's stored ciphertext untouched (a dict-merge over the loaded `secrets` JSONB, `decrypt` never invoked); new key → write `encrypt("")` as an unset placeholder. `decrypt` (`api/shared/secrets.py`) is never on the import code path.
  - *Export:* `environments: []` always (Architect contract — "a collection has no canonical environment"). The export builder (`fixtures.engine.build_export`) MUST NOT read `environments.secrets` at all — not even to mask it. Verify in code review: the export SELECT touches `collections`, `collection_items`, `requests`, `evaluations` only; no join to `environments`. So an export never leaks even a masked secret or a secret *key name*. This is the airtight property.
  - *Residual (expected, fine):* after import an operator can `PATCH /v1/environments/{id}` to fill in the real secret value. That's the intended hand-off; the fixture stays clean, the live value lives only in the DB (Fernet at rest). Not a finding.
  - Request `body`/`headers` in a fixture may contain `{{SECRET}}` *placeholders* — those are variable references, not values; safe to export. (The run engine substitutes them per-run from the bound environment; they're never resolved on the export path.)
- **Owner scoping on upsert** — `INSERT ... ON CONFLICT (owner_user_id, name) DO UPDATE` for environments/collections (existing constraints) and for `requests` (new `UNIQUE (owner_user_id, name)` per Architect). The conflict target includes `owner_user_id`, so operator A's import can only collide with — and therefore only update — A's own rows. B's `chess-legality` collection and A's `chess-legality` collection are distinct rows; A's import never touches B's. Confirmed: no cross-owner clobber via import. **Code-review check:** the `ON CONFLICT` arbiter must be the *named* `(owner_user_id, name)` constraint, not `(name)` alone, and the `WHERE`/`SET` clauses must not be writable to set `owner_user_id` from the fixture (volatile field — stripped, warned).
- **Audit / event payloads** — `record_event` is called once on a successful import: `kind="fixtures.imported"`, `target_kind="fixtures"`, `target_id=NULL`, `payload={counts...}` (the four `{created,updated}` pairs, plus `len(warnings)` is fine). **Do NOT log the fixture body** — it can be large, and even though `secrets` are keys-only, a request `body` is arbitrary JSON an author might (wrongly) have stuffed a token into; log counts only. If a fixture grows a top-level `name`/label field later, log that string; for now there is none, so just counts. The `warnings[]` strings are returned to the caller (already in the response body) — they reference field paths and resource names, not values; safe.
- **Chess game state is non-sensitive** — `chess_games` rows are `{id (uuid), engine_color, seed, fen, moves[], status, timestamps}`. A FEN is a board position; `moves` is a UCI list; `seed` is an int. Nothing here is a secret, an identifier of a person, or cross-referenceable to anything sensitive — there is no `owner_user_id` on the table by design. Enumeration risk: `game_id` is a v4 UUID (`gen_random_uuid()`), unguessable; even if guessed, the row leaks only game state, which is itself returned by the API anyway. No `LIST /games` endpoint exists (and none should — adding one would be a (mild) enumeration nicety with zero product value). Acceptable.
- **Error messages** — chess endpoints on malformed FEN/UCI must return `422` with a short `detail` string ("invalid FEN", "unparseable UCI move"), never a 500 with a Python traceback. `python-chess` raises `ValueError` on bad input (`chess.Board(fen)`, `chess.Move.from_uci(...)`); the module engine must catch `ValueError` (and `IndexError` for some malformed UCI) and translate to a 422 — no bare exception bubbling. Same discipline on import: a bad eval `kind`/`config`, unresolved item, duplicate name, etc. → explicit 422, never a stack trace.

### Mitigations
- **Secret leakage via committed fixtures** → keys-only `dict[str,str]` model with a non-empty-value-is-422 validator at parse time; export emits `environments: []` and never queries `environments`. (See Data exposure.)
- **Cross-owner clobber via upsert** → `ON CONFLICT (owner_user_id, name)`; `owner_user_id` is set server-side from `auth.actor_id`, never from the fixture (volatile → stripped + warned).
- **Harness key / anon escalation to import/export** → `require_user` on both routes; same gate as every other authoring route.
- **SSRF via fixture-authored request templates** → no new surface. A fixture can author a request with any `url` — but so can `POST /v1/requests` today; the run engine's `is_blocked_host` deny-list (`api/runs/_ssrf.py`) is the chokepoint and it's unchanged. In staging/prod it blocks loopback/link-local/RFC1918 literal IPs and non-http(s) schemes; `CHESS_BASE_URL` in prod is `https://pagehub-evals-production.vercel.app` (a public host) so legitimate chess requests pass. DNS-rebinding window is a pre-existing slice-3 item, not introduced here. Confirmed: no widening; the import path does not need to (and must not) re-validate URLs differently from `POST /v1/requests` — reuse the `requests/schemas.py` constraints (`url` 1..2000, method regex) verbatim via the `FixtureRequest` model.
- **Fixture-as-DoS (oversized bundle)** → **add a hard cap on the fixture bundle.** Starlette/FastAPI has no default request-body size limit; a 50MB JSON with 100k requests would be parsed into memory, validated, and pushed through one transaction holding one pooled connection (pool size 10) — a cheap way for one operator to stall the API and bloat the DB. **Concrete caps the builder implements (same numbers in the Reliability section — these are the canonical values):**
  - `FixtureCollection.items` (and therefore any imported collection): **≤ 50** — exactly `api/runs/_constants.py:COLLECTION_ITEM_CAP` (the shared leaf constant; the runs route imports it too); **import the constant, don't re-literal it** so the two never drift. A fixture declaring a 5000-item collection is pointless (the run engine refuses to execute any collection with >50 items). Reject at parse time: 422 `{"error":"collection_too_large","collection":"<name>","items":N,"max":50}`.
  - `FixtureBundle.requests`: **≤ 200** (a comfortable multiple of 50; defensive).
  - `FixtureBundle.collections`: **≤ 50**.
  - `FixtureBundle.environments`: **≤ 50**.
  - `FixtureRequest.evaluations`: **≤ 20** per request.
  - Raw request body: **≤ 1 MiB (1,048,576 bytes)** — enforced by a pure-ASGI middleware (`api/fixtures/_body_limit.py`, `FixtureBodyLimitMiddleware`) scoped to the `POST /v1/fixtures/import` path, *before* the body is parsed: it streams the request body off the wire chunk-by-chunk, counting bytes, and short-circuits with a self-describing `413` the moment the running total exceeds the cap. It does **not** trust the `Content-Length` header (a client can lie or omit it) — the stream-and-count is the actual guard and is strictly better. Starlette has no default body limit, so the middleware is the size guard; the post-parse Pydantic caps (`_MAX_REQUESTS` / `_MAX_COLLECTIONS` / `_MAX_ENVIRONMENTS` / `_MAX_EVALUATIONS_PER_REQUEST` / `_MAX_COLLECTION_ITEMS` on `FixtureBundle` / `FixtureRequest`) are a second line, not the byte cap.

  These are all parse-time / pre-parse rejections — no DB churn. Flag for the Architect: the SPEC's "very large fixture (hundreds of requests)" edge-case note explicitly *defers* a limit ("if this becomes a problem it's a follow-up") — I'm pushing back: an *unbounded* operator-triggered transaction is a real (if low-severity, since it needs an operator JWT) abuse vector and the cap is one Pydantic annotation each. At minimum align `items` to `COLLECTION_ITEM_CAP`. (Reliability concurs; Data should note the cap exists so its "chess.json is a handful" framing isn't read as "no limit".)
- **Unbounded chess-game creation (disk fill)** → low risk (a `chess_games` row is tiny: a UUID, two short strings, a JSONB list, two timestamps — well under 1KB), and the endpoint is anonymous so a determined spammer *can* create rows freely. **Resolved (matches SPEC + Reliability + Data): no reaper and no `chess_games_created_at` index this slice** — the table is tiny and only ever read by PK, an unused index buys nothing now; the slice-N reaper ships `DELETE FROM chess_games WHERE created_at < now() - interval '1 day'` *and* the `created_at` index together. A per-IP rate limit on `POST /v1/modules/chess/games` is new infra and out of scope for this slice. So: accept the (low-severity, anonymous, capacity-only) risk explicitly with the slice-N reaper as the tracked follow-up. (No `LIST`/enumeration endpoint, so the rows are write-mostly garbage, not a data-exposure issue — purely a capacity one.)
- **Parser-DoS on chess input** → none material. FEN strings and UCI moves are tiny (bounded by FastAPI's normal body handling once a byte cap is in place; even without one, a `{"fen": "..."}` body is small). `python-chess` is pure-Python, parses FEN/UCI in linear time on a fixed-size 64-square board — no regex backtracking, no recursion blow-up, no algorithmic complexity hole. The only requirement is catching `ValueError`/`IndexError` and returning 422 (above). The `seed` parameter: type it as `int` (Pydantic `int`, with a sane range bound, e.g. `Field(ge=0, le=2**63-1)` to match the `BIGINT` column) — **not `Any`** — and feed it to `random.Random`; an int seed has no injection or resource-exhaustion angle. (Also obeys the project rule: every request body is a fully-typed Pydantic model, no `Any`/`dict` params on the chess routes.)
- **SQL injection** → all new DB writes go through asyncpg parameterized queries (`$N` placeholders, `json.dumps(...)` into `$N::jsonb`) exactly like `api/requests/routes.py:58` and `api/environments/routes.py:60`. **No string-interpolated SQL anywhere in `fixtures/engine.py` or `modules/chess/engine.py`.** The one place existing code builds SQL dynamically is `update_environment` (`api/environments/routes.py:128` — a field list driven by which optional body fields were set); the import engine should *not* need that pattern (it writes a fixed column set per resource). If a code-review reveals it does, the column names must come from a hard-coded allowlist, never from fixture keys.
- **`requests` UNIQUE constraint + 409** → `POST /v1/requests` (`api/requests/routes.py:54`) gains `try/except asyncpg.UniqueViolationError → HTTPException(409)`. No real security impact: the conflict is owner-scoped, so a 409 can only ever fire on the caller's *own* duplicate name — the 409 body must not echo the *other* row's owner (it can't; there's only one owner involved) and should just say "a request named '<name>' already exists" (the name is the caller's own input). Idempotent DDL block in `api/shared/schema.sql` per the Architect; against a *dirty* DB with pre-existing duplicate names a dedupe pass is required first — noted, not a concern on this fresh scaffold.
- **Chess game-state events** → **resolved: no events from the chess module at all** (SPEC §Integrations is explicit — "it does not emit events"; Reliability concurs; Data treats it as already-decided). The run that drives a `chess-playable-game` collection already emits `run.*` events covering the whole sequence; a `chess_game.created` row would be marginal triage value and a per-move event pure volume. I withdraw the earlier "one `chess_game.created`" suggestion — go with SPEC. (If anyone later wants a `created` event for "when did this game start, what seed", payload would be `{seed, engine_color}`, non-sensitive — but not this slice.) No event write means no new audit-payload exposure surface on the chess path.

### Open concerns
- **Bundle size cap (the one item that still needs Architect sign-off)** — SPEC's edge-case note *defers* a fixture size limit; the PLAN (Security + Reliability, same numbers) overrides that: `collections[].items` ≤ `COLLECTION_ITEM_CAP` (50, the shared leaf constant in `api/runs/_constants.py`), `requests[]` ≤ 200, `collections[]` ≤ 50, `environments[]` ≤ 50, `evaluations[]` per request ≤ 20, raw body ≤ 1 MiB. ~five Pydantic `max_length` annotations + a small pre-parse body-size middleware (`FixtureBodyLimitMiddleware`); it contradicts a SPEC sentence, so Architect to confirm. (Recommendation stands: ship the caps; Data should add a one-liner acknowledging the cap so its "chess.json is a handful" framing isn't read as "no limit".)
- **Chess-game retention** — RESOLVED: no reaper and no `chess_games` `created_at` index this slice (table is tiny, PK-only access); the slice-N reaper ships `DELETE WHERE created_at < now() - interval '1 day'` *with* the index, together. Accepted, tracked follow-up. (Settled in favour of SPEC + Data; Security no longer leans "add the index now".)
- **Chess events** — RESOLVED: no events from the chess module (SPEC §Integrations is explicit; Reliability + Data agree). The earlier "one `chess_game.created`" suggestion is withdrawn.
- **Export auth breadth** — I'm matching the existing "any operator reads any collection" pattern for `GET /v1/collections/{id}/export`. If anyone on the trio actually wants per-owner collection isolation, say so now — it's a cross-cutting change to every collections read route, not an export-only tweak, and out of scope as currently specced.

## Reliability

### Failure modes

- **Bad eval `kind`/`config` in request #37 of 50 → whole import rolls back.** The
  Architect's design (`api/fixtures/engine.py` called inside one
  `async with auth.db.transaction():` in `api/fixtures/routes.py`) delivers
  all-or-nothing: a `422` raised after any row write happens *inside* the txn, the
  `with` block exits via exception, asyncpg issues `ROLLBACK`, nothing is applied.
  The 422 body must name the offending resource (`{"error":
  "invalid_evaluation_config", "request": "<name>", "evaluation": "<name>", ...}`),
  so the operator knows what to fix without diffing the DB. **Confirmed: single
  transaction delivers this** — no "best-effort" path, no per-resource commits.
  Subtlety the builder must honour: any validation that *can* be done at parse time
  (`kind` against `EvaluationKind`, per-kind `config` shape, non-empty secret value,
  duplicate names in the bundle, unsupported `version`) belongs in the Pydantic
  `FixtureBundle` model — that fails the request *before* the txn opens, which is
  strictly better (no DB churn, FastAPI's standard 422 envelope). Only validation
  that needs DB state (resolving a collection item's request name against rows just
  inserted) runs inside the txn.
- **DB connection drop mid-import → asyncpg raises → txn aborts → 500.** Acceptable.
  asyncpg surfaces `ConnectionDoesNotExistError` / `InterfaceError`; the txn never
  commits; the route's unhandled-exception path returns 500. No partial state because
  nothing was committed. Operator retries; the retry is safe (see Idempotency). The
  route must NOT retry internally — the one risky window (socket dies *after* COMMIT
  but before the response reaches the caller) would double-apply on a blind retry;
  asyncpg makes that window tiny but non-zero, and an operator-driven re-POST is just
  as safe and is observable.
- **A huge fixture holding a long transaction open.** `api/shared/schema.py` applies
  the DDL under `pg_advisory_xact_lock` on boot (`schema.py:22`), but *normal* writes
  aren't serialized — a big import takes row locks on every `requests` /
  `collections` / `environments` row it upserts and on the `evaluations` /
  `collection_items` rows it delete-then-reinserts, held until the txn ends. Two
  operators importing overlapping fixtures concurrently: the second blocks on the
  first's row locks, then proceeds (last writer wins per the Architect's
  `ON CONFLICT ... RETURNING (xmax = 0)`). Acceptable for an operator-only authoring
  action at this scale (`chess.json` is a handful of rows; pool size 10). **But
  recommend a sane cap on the bundle, aligned with the run engine's
  `COLLECTION_ITEM_CAP = 50` (`api/runs/_constants.py`, imported by
  `api/runs/routes.py`):** a fixture declaring a 5000-request collection is
  *useless* — the run engine refuses to execute any collection with >50 items, so
  the collection import just wrote can never run. Reject at parse time:
  `len(collection.items) <= 50` per collection (422
  `{"error": "collection_too_large", "collection": "<name>", "items": N, "max":
  50}`), plus a defensive overall cap (≤ ~200 `requests[]`, ≤ ~50 `collections[]`,
  ≤ ~20 `evaluations[]` per request — round numbers; the run engine is the real
  bound). **Flag: the per-collection cap MUST track
  `api/runs/_constants.py:COLLECTION_ITEM_CAP` — import the constant or it drifts.** Without the
  cap a malformed fixture is a slow self-DoS, not a crash — low severity, cheap to
  prevent. (Security section raises the same point; this is the reliability framing.)
- **Chess `/legal-moves` with garbage FEN → 422, not 500.** `python-chess`
  `chess.Board(fen)` raises `ValueError` on a malformed FEN; the route must catch it
  and return 422, never let it propagate to a 500. Same for `/games` with a bad
  `starting_fen`. Un-parseable UCI on `/moves` → 422; illegal-given-position UCI →
  200 with `status: "illegal_move"` (per the contract — a body field, not a transport
  error). The line between "422" and "200 illegal_move" is exactly "parses as UCI
  notation" vs. "is a legal move on this board" — the builder must not conflate them
  or the playable-game evals (`json_path_eq $.status`) break.
- **`/moves` on an unknown `game_id` → 404.** A run that lost its `GAME_ID` capture
  (engine bug, or the game row got reaped) sees a 404 mid-collection → the run engine
  marks that step failed → `error`/`failed` verdict. Acceptable; the table is durable
  (below) so this should never happen from a cold start. No reaper this slice, so a
  reaped-out-from-under-a-run scenario can't occur either.

### Idempotency

- **Re-importing the same fixture is a no-op-shaped operation.** `created: 0`
  everywhere; `updated` reflects the rows re-touched by the upsert (or zero, if the
  bundle is empty). **Confirmed contract per SPEC: there is NO `unchanged` bucket** —
  an unchanged re-import reports every touched row under `updated` with `created: 0`,
  and `created: 0` is *the* idempotency signal (SPEC §"Import semantics", flow #2,
  and the Architect's Contracts section). This is deterministic: `created` is computed
  from `xmax = 0` on the upsert's `RETURNING`, a hard fact about whether the row
  pre-existed — no heuristic, no value-diff. The pytest and the platform seed both
  assert `created == 0` on the second import.
- **REPLACE semantics are idempotent.** For each request in the bundle:
  `DELETE FROM evaluations WHERE request_id = $1` then bulk-`INSERT` the bundle's
  list. For each collection: `DELETE FROM collection_items WHERE collection_id = $1`
  then bulk-`INSERT` with `position` = array index `0..n-1`. Same input → same final
  state, every time (the delete is unconditional, the insert is from a fixed list,
  positions are deterministic). **The `evaluations.id` / `collection_items.id`
  churning on every import is acceptable** — those ids are not referenced by any
  stable surface (no FK points at them; `runs` reference `collection_id` /
  `environment_id`, never `collection_items.id` or `evaluations.id`), and the
  round-trip normalizer drops `id` / `request_id` / `collection_id` before any
  comparison (Architect's `normalize_for_roundtrip` step 1). So byte-stability of
  export survives the id churn. Confirmed.
- **No run gets orphaned by a re-import.** `runs.collection_id` and
  `runs.environment_id` are `ON DELETE SET NULL`, but **import never deletes a
  collection or environment** — top-level resources are upsert-only (Architect:
  "import never deletes a request or collection the bundle doesn't mention; only
  *children of mentioned parents* get the replace treatment"). The collection row's
  `id` is preserved across re-import (`ON CONFLICT (owner_user_id, name) DO UPDATE`
  keeps the existing PK). Therefore a re-import never nulls a `runs` FK, never orphans
  historical run evidence. Confirmed.
- **Concurrent imports of the same fixture (CI + operator).** Per-row
  `INSERT ... ON CONFLICT DO UPDATE`; last writer wins; both calls return internally
  consistent counts (one may report `created`, the other `updated` for the same row).
  The collection-items REPLACE is atomic per collection because it's inside the one
  txn. No corruption, no partial-collection state.

### The round-trip invariant as a reliability property

- The acceptance property is `export → import → export` byte-identical modulo
  ids/timestamps, enforced via the single source of truth
  `normalize_for_roundtrip(bundle: dict) -> dict` in `api/fixtures/schemas.py`
  (Architect's Contracts §"Canonical serialization"). What can break byte-stability,
  and how each is pinned:
  - **Dict key ordering.** FastAPI serializes a Pydantic model in *field declaration
    order* — the export bundle's key order is fixed by how `FixtureBundle` /
    `FixtureRequest` / `FixtureEvaluation` / `FixtureCollection` are declared
    (Architect spells out the exact order: top level `version, environments, requests,
    collections`; request `name, method, url, headers, body, capture, evaluations`;
    evaluation `name, kind, config`; collection `name, description, items`).
    **Confirmed `model_dump()` preserves declaration order.** The normalizer
    additionally sorts `requests[]`/`collections[]` by `name` and per-request
    `evaluations[]` by `name`, so even a hand-authored bundle in a different order
    normalizes to the same thing. The *values* of `headers`/`body`/`config`/
    `variables` are dicts compared order-insensitively in Python — but any test that
    serializes them for a `body_eq`-style assertion MUST
    `json.dumps(..., sort_keys=True)` (Architect step 3 says exactly this).
  - **`None`-vs-absent fields.** **Pick one and make export and the normalizer
    agree.** Recommendation: **always-emit** for the modeled optional fields
    (`headers: {}`, `body: null`, `capture: {}`, `description: null`, `items: []`,
    `evaluations: []`, `environments: []`) — i.e. do NOT set
    `response_model_exclude_none=True` on the `/export` route. Rationale: the project
    rule prefers `response_model_exclude_none=True` "where appropriate", but here it
    is *not* appropriate — a re-import of an export must produce the same export, and
    "omit when None" makes `body: null` disappear on export while a hand-authored
    `body: null` is *present* on import, so the round-trip of a `null`-body request
    isn't byte-stable unless the normalizer also strips Nones. Cleaner to always emit.
    If the builder prefers `exclude_none`, the normalizer must then drop `None`-valued
    and `[]`/`{}`-valued keys on *both* sides — pick always-emit, it's less code and
    less subtle. **The builder must state which choice they took in the PR.**
  - **JSONB round-tripping through Postgres.** asyncpg returns a `jsonb` column as a
    Python value via `json.loads`; the existing code in `api/requests/routes.py:28-39`
    and `:61-70` does `json.dumps` on the way in, `json.loads` on the way out — there
    is no custom asyncpg JSONB codec registered, so it's the driver default. `{"a":
    1}` survives as `{"a": 1}`; `1.5` as `1.5`. The one nuance: JSON has no int/float
    distinction at the wire level, but Python's `json` module preserves `int` vs
    `float` on a round trip (`json.loads(json.dumps(3))` is `int 3`, not `3.0`), so a
    `body` like `{"fen": "...", "depth": 3}` stays integer-valued; floats are exact
    for the values JSON represents exactly (all the values a chess fixture uses).
    **Flag for the builder: keep `json.dumps` on insert and `json.loads` on read in
    the fixtures engine — do NOT register an asyncpg `jsonb` type codec, and do NOT
    pass a Python dict directly to a `$n::jsonb` parameter (asyncpg would str-cast it,
    not JSON-encode it).** This is a known-stable path; the existing requests routes
    already rely on exactly it.
  - **`environments` asymmetry.** Export always emits `environments: []` (Architect
    confirmed `[]`, not key-omitted); the normalizer drops `environments` entirely
    from the comparison (step 2). So a hand-authored fixture with `environments` still
    round-trips: import applies it, export drops it, normalizer ignores it.
  - **`collection_items.position` / first-referenced request order.** Export emits
    requests in first-referenced order and assigns positions densely from array index,
    so the normalizer's sort steps are no-ops on export-produced bundles — a clean
    round-trip needs only normalizer steps 1, 4, 5. Pinned by the Architect.
- **Recommended test:** `api/tests/test_fixtures_roundtrip.py` — `import(chess.json)`
  → `export(each of the two collections)` → `import` each → `export` each again →
  `assert normalize_for_roundtrip(a) == normalize_for_roundtrip(b)`. Drives
  `fixtures.engine` directly against a real test Postgres (no live server). This is
  the regression guard; the platform seed is the eval.

### Chess module reliability

- **State in `chess_games` survives cold starts / uvicorn restarts.** An in-memory
  dict would drop a game mid-`chess-playable-game` run on a serverless cold start or
  `uvicorn --reload`, and the run engine would see a 404 halfway through → a flaky
  `error` verdict. The Architect chose a table (`api/shared/schema.sql` new
  `chess_games`); **confirmed — that's the right call and the only correct one.** The
  row is small. **The Data section's `chess_games` DDL is the authoritative schema**;
  this Reliability prose refers to those column names, it does not define a parallel
  one. Concretely Data persists `id`, `engine_color`, `seed BIGINT`, `starting_fen`,
  `fen`, `move_history` JSONB, `move_count INTEGER` (`== jsonb_array_length(move_history)`,
  the ply index), `status`, `created_at`, `updated_at`. Where the prose below writes
  `moves` read it as Data's `move_history`; where it writes `ply = len(moves)` read it
  as Data's stored `move_count`. (The Security section's `moves[]` shorthand is the
  same column — not a third schema. Builder: follow Data's DDL verbatim.)
- **Concurrency on `/moves` — read-modify-write race.** Two `/moves` calls on the
  same `game_id` racing: each reads the FEN (and `moves` history), applies the harness
  move, computes the engine reply, writes the new FEN + appended `moves`. A lost
  update could corrupt the game (one writer's move vanishes; worse, the engine RNG
  derivation — keyed on ply count, see below — desyncs from the persisted board,
  making the position un-reconstructable). **Recommend: `SELECT ... FOR UPDATE` on
  the `chess_games` row inside a transaction for the whole read-modify-write in
  `/moves` (the `/games` open path is a plain INSERT, no contention).** In practice a
  single eval run drives one game strictly sequentially — the run engine fires
  requests one at a time, never overlapping — and two runs would never share a
  `game_id` (each `POST /games` mints a fresh UUID). So the race window is essentially
  "someone manually curls the same game id twice fast" — but `FOR UPDATE` is one
  clause, costs nothing on the uncontended path, and turns a silent corruption into a
  serialized correct result. **Flag: do it.** (An optimistic version column also
  works but is more code for no benefit here.)
- **Engine determinism — the single most fragile thing in the build.** SPEC: the
  engine's reply is a "uniformly random legal move", RNG seeded by the request-body
  `seed` (default `0`), and the `chess-playable-game` collection's per-step `$.fen`
  evals are only authorable if **the entire game line is deterministic given the seed
  AND the harness's scripted moves**. The Architect's NIT flags the trap: a fresh
  `random.Random(seed)` *per `/moves` call* picks the same index every call → the
  engine repeats its first move forever. The fix the builder MUST implement: derive
  the RNG from `(seed, ply_index)`, not `seed` alone. **Exactly what's persisted and
  how the move is chosen** (column names per the Data section's authoritative DDL):
  - `chess_games.seed` (BIGINT) — the constant from the `/games` request body.
  - `chess_games.move_history` (JSONB array of UCI strings) — the full move history,
    appended on every applied move (harness moves and engine moves both). Doubles as
    run evidence and the defensive board-replay source.
  - `chess_games.move_count` (INTEGER, `== jsonb_array_length(move_history)`, written in
    the same `UPDATE`) — **this is the ply index** the RNG keys on; cannot drift from
    `move_history` since both land in one statement.
  - `chess_games.starting_fen` — the FEN the game opened from (startpos by default);
    with `move_history` it makes the board fully re-derivable.
  - `chess_games.fen` — the current position (denormalized for cheap reads;
    re-derivable from `starting_fen` + `move_history` — the three MUST stay consistent,
    which is why `/moves` writes `fen` + `move_history` + `move_count` together inside
    the `FOR UPDATE` txn).
  - **Move selection (engine's turn):** let `ply = move_count` *at the moment the
    engine is about to move* (i.e. after the harness move was appended, so `move_count`
    already counts it). Compute the
    sorted list `legal = sorted(m.uci() for m in board.legal_moves)`. Pick
    `legal[random.Random((seed, ply)).randrange(len(legal))]` — a *fresh* `Random`
    seeded by the `(seed, ply)` tuple each time, so every engine reply draws from a
    different stream. (Equivalently: one `random.Random(seed)` advanced `ply` times —
    but the tuple-seed form is stateless and doesn't depend on replay order, which is
    safer.) Document the *exact* derivation in the README so the fixture author can
    reproduce the FENs by hand.
  - `api/modules/chess/README.md` MUST document: the canonical `seed` (`0`), the
    scripted harness move line (`e7e5`, ...), and the resulting expected FEN after
    each step — the literal values that go into `chess.json`'s `json_path_eq $.fen`
    evals, ideally with a `python-chess` one-liner comment that re-derives them so the
    numbers aren't circular with the test. **Assign this doc to the builder; the chess
    fixture's evals are unwritable without it, and a wrong derivation makes them
    silently wrong (they'd pass against the buggy engine and fail the moment the
    engine is "fixed").**

### Retries & backoff

- **Import: not retried internally.** A failed import (422 or 500) is returned to the
  operator, who re-POSTs if appropriate. The upsert semantics make a manual retry a
  no-op-shaped operation (see Idempotency), so this is safe — but an *automatic* retry
  is deliberately not added (the COMMIT-then-socket-died window would double-apply;
  operator re-POST is equally safe and observable).
- **Chess `/moves`: not retried.** The run engine owns request-level retry policy
  (unchanged by this slice); the chess module retries nothing. A `FOR UPDATE`
  lock-wait is bounded by the other writer's txn (fast — one board apply), so no
  backoff needed.
- **What is NOT retried, on purpose:** import (manual re-POST instead), chess module
  endpoints (stateless oracle / single board apply). Nothing in this slice introduces
  a new retry loop.

### Observability

- **Import logging.** Log at start: `fixture.import.start` with the resource counts
  the bundle declares (`environments=N requests=N collections=N`, total evaluations).
  On success: `fixture.import.ok` with the response counts (`{env: {created,updated},
  ...}`). On failure: `fixture.import.failed` with the *which-resource* context — the
  same `{"error": "...", "request": "...", "evaluation": "..."}` payload that goes in
  the 422 detail — so an oncall reads the log line and knows exactly what the
  operator's fixture got wrong without needing the request body. (On a 500: log the
  exception with traceback as usual.) **Do not log the fixture body** (large; a
  `body` field may contain a wrongly-pasted token) — counts and resource paths only;
  Security section says the same.
- **`record_event`.** Exactly one `events` row on success: `kind="fixtures.imported"`,
  `target_kind="fixtures"`, `target_id=NULL`, `payload={counts...}` (Architect's
  Integrations §`events.py`). No event on failure (the txn rolled back, including any
  event write — and a failed import isn't a fact worth persisting; the log line covers
  it).
- **Metrics — recommend one counter, or skip; my call: add it, it's one line.**
  `fixtures_imported_total` (no labels — import is rare and operator-only, so
  cardinality and volume are both trivial), in a new `api/fixtures/_metrics.py`
  mirroring `api/runs/_metrics.py`, incremented on success. Optionally
  `labelnames=("outcome",)` with `outcome ∈ {ok, rejected}` if the oncall wants to
  see a spike of bad fixtures — that's the only metric I'd consider; the per-resource
  counts live in the `events` row, not Prometheus. **If the builder skips import
  metrics entirely, that's defensible** (low-volume authoring action, `events` row is
  the durable record) — but the existing repo pattern is "engine emits counters", so a
  single `fixtures_imported_total` keeps it consistent. Pick one; don't ship a
  half-dozen import gauges.
- **Chess module: no metrics, no events.** Low value — it's the thing-under-test, and
  the run that drives it already emits `run.*` events and `pagehub_evals_runs_*`
  metrics. A `chess.*` event would be pure noise. Standard request/access logging is
  enough to debug a 422-on-bad-FEN. (The Security section floats one
  `chess_game.created` event; my reliability take: not needed, but if the trio wants
  it, it's harmless and one row per game.)
- **What an oncall sees when this misbehaves:** import wedged on a lock → a
  `fixture.import.start` line with no matching `.ok`/`.failed`, plus a long-running
  query in `pg_stat_activity` on `requests`/`collections` (the cap recommendation
  prevents this from being unbounded). Bad fixture → `fixture.import.failed` with the
  resource path. Chess game stuck → no symptom (rows tiny, abandoned harmlessly); a
  run that hit a stuck game shows up as a `failed`/`error` run with the 404 step in
  its evidence.

### Recovery

- **A half-imported fixture cannot happen** — single transaction, all-or-nothing. A
  422/500 leaves the DB exactly as it was; the operator fixes the fixture and
  re-POSTs. No cleanup, no compensating action, no manual reconciliation. RTO ≈ time
  to fix the JSON and re-POST.
- **A `chess_games` row stuck in a weird state.** The game is simply abandoned —
  nothing references it, the run that owned it is over (with a `failed` step in its
  evidence). Rows are tiny (one FEN, a short move list). **No reaper this slice** —
  recommend NOT building a TTL cleanup now; the Architect already notes a slice-N
  `DELETE WHERE created_at < now() - interval '1 day'` as the eventual answer. (The
  Security section leans toward at least adding a `created_at` index now so a future
  reaper is cheap — I agree that index is fine to add this slice; the reaper itself is
  slice-N.) Accretion is bounded (one row, a few hundred bytes, per `/games` call),
  harmless. **Flag: deferred, deliberate punt — not a gap.**
- **Schema migration on boot.** `api/shared/schema.py` applies `schema.sql`
  idempotently under `pg_advisory_xact_lock` (`schema.py:22`). The two new pieces:
  - `requests` `UNIQUE (owner_user_id, name)` — the Architect's
    `DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname =
    'requests_owner_user_id_name_key') THEN ALTER TABLE requests ADD CONSTRAINT ...
    END IF; END $$;` block is re-runnable (the `IF NOT EXISTS` guard makes the second
    boot a no-op). **Confirmed re-runnable.** Caveat the Architect already flagged:
    against a *dirty* DB (pre-existing duplicate-named requests) the `ADD CONSTRAINT`
    fails — not a concern here (fresh scaffold, `runs` is a stub, no real request
    rows), but if this ever runs against a populated DB a dedupe pass is a
    prerequisite. PLAN's Data section owns spelling that out; flagging the cross-ref.
  - `CREATE TABLE IF NOT EXISTS chess_games (...)` — idempotent by construction.
    **Confirmed fine.**
  Recovery from a bad deploy: roll back the code; the schema additions are
  forward-only and harmless (an unused unique constraint, an unused table) — no schema
  rollback needed.

### Test coverage

- **Unit tests:**
  - `api/tests/test_fixtures_import.py`:
    - `test_import_happy_path` — POST a small inline bundle (and/or `fixtures/chess.json`)
      into an empty DB; assert each kind's `{created, updated}` matches; assert rows
      landed (`SELECT count(*)`). *Proves:* the import contract end to end.
    - `test_reimport_is_idempotent` — import twice; assert second response has
      `created == 0` for every kind, `updated` ≥ 0; row counts unchanged;
      `collection_items` identical in content and order (ids may churn — allowed).
      *Proves:* the SPEC idempotency success-criterion.
    - `test_bad_eval_kind_rejected_422` — bundle with unknown `kind`; 422 with
      `{"error": "invalid_evaluation_kind", "request": ..., "evaluation": ...,
      "allowed": [...]}`; **nothing written** (row counts still zero). *Proves:*
      rollback on bad child.
    - `test_bad_eval_config_rejected_422` — `status_eq` without `expected`; 422
      `invalid_evaluation_config` + request/evaluation context; nothing written.
    - `test_non_empty_secret_value_rejected_422` — `secrets: {"K": "value"}`; 422
      `secret_value_in_fixture` at parse time (no DB write — ideally the txn never
      opens).
    - `test_unresolved_request_name_in_collection_item_rejected_422` — a
      `collections[].items` name not in `requests[]`; 422 `unresolved_request_ref`
      with `collection`, `item_index`, `request`; nothing written.
    - `test_duplicate_name_in_bundle_rejected_422` — two `requests[]` with the same
      name; 422 `duplicate_name` *before* any DB write.
    - `test_collection_too_large_rejected_422` — a `collections[].items` of length 51
      (one over `COLLECTION_ITEM_CAP`); 422 `collection_too_large`. *Proves:* the cap
      and that it tracks `api/runs/_constants.py:COLLECTION_ITEM_CAP`.
    - `test_all_or_nothing_rollback` — a bundle whose request #1 is fine and request
      #2 has a bad eval `config`; assert 422 AND request #1 did NOT land. *Proves:*
      the headline rollback invariant.
    - `test_owner_scoping` — operator A imports a bundle with collection `C`; operator
      B imports a bundle also naming `C`; assert two distinct rows
      (`UNIQUE (owner_user_id, name)`), each owned correctly, B's import doesn't touch
      A's.
    - `test_volatile_fields_stripped_with_warning` — a request object with a stray
      `id`/`created_at`; 200, the field ignored, `warnings[]` has the "ignored
      unexpected field" entry.
    - `test_empty_bundle_is_noop` — `{"version": 1}`; 200, all counts zero, `warnings
      == ["fixture contained no resources"]`.
    - `test_unsupported_version_rejected_422` — `{"version": 2}`; 422
      `unsupported_fixture_version`.
    - `test_export_then_import_unrelated_owner` — export `chess-legality`, assert
      `environments == []`, import into a fresh owner, assert it lands and the
      collection's items resolve by name.
    - `test_import_requires_operator` — harness-key / anonymous caller → 403.
  - `api/tests/test_fixtures_roundtrip.py` — `import(chess.json)` → `export` each of
    the two collections → `import` each → `export` each again → `assert
    normalize_for_roundtrip(a) == normalize_for_roundtrip(b)`. The single durable
    guard for the round-trip invariant. Also covers: empty-collection round-trip; a
    request shared by both collections appears once in each export's `requests[]`.
  - `api/tests/test_requests_routes.py` (extend existing) —
    `test_create_request_duplicate_name_409` — `POST /v1/requests` twice with the same
    name → second is 409 (the new `try/except asyncpg.UniqueViolationError` wrapper the
    new constraint makes reachable). This is a contract-surface change on a public
    route — it ships with this test.
  - `api/tests/test_chess_legal_moves.py`:
    - `test_legal_moves_startpos` — startpos FEN → 200, `turn == "white"`,
      `legal_moves` contains `e2e4` and `g1f3`, length 20, list is sorted.
    - `test_legal_moves_midgame` — a known mid-game FEN → 200, `legal_moves` matches
      `python-chess` ground truth, `turn` correct, `fen` echoed normalized.
    - `test_malformed_fen_422` — `{"fen": "not a fen"}` → 422, not 500.
    - `test_missing_fen_422` — body without `fen` → 422 (Pydantic).
  - `api/tests/test_chess_games.py`:
    - `test_open_game_engine_white_moves_first` — `POST /games {engine_color: "white",
      seed: 0}` → 200, `engine_move` non-null, `turn == "black"`, `status ==
      "in_progress"`, a `chess_games` row exists with `moves` length 1.
    - `test_open_game_engine_black_no_move` — `engine_color: "black"` → `engine_move ==
      null`, `turn == "white"`.
    - `test_moves_legal_harness_move_then_engine_reply` — open (engine white, seed 0),
      then `POST /games/{id}/moves {move: <a legal black move>}` → 200, `status ==
      "in_progress"`, `engine_move` non-null, `moves` length 3, `fen` advanced.
    - `test_moves_illegal_given_position_returns_200_illegal_move` — submit an illegal
      UCI for the position → 200, `status == "illegal_move"`, `engine_move == null`,
      board unchanged (`fen`/`moves` unchanged), `legal_moves` populated. **Not a
      4xx.**
    - `test_moves_unparseable_uci_422` — `move: "hello"` → 422.
    - `test_moves_unknown_game_404` — random UUID → 404.
    - `test_deterministic_game_given_seed` — open (engine white, seed 0), play the
      *exact scripted harness line from the README*, assert the FEN after each step
      equals the README's documented expected FEN. *Proves:* engine determinism — if
      the `(seed, ply)` derivation is wrong, this fails. Also locks the README's
      numbers so the fixture author can trust them.
  - `api/tests/test_chess_games_concurrency.py` (recommended; mirrors
    `api/tests/test_runs_engine_concurrency.py`): two concurrent `/moves` on the same
    `game_id` against real Postgres; assert exactly one move "wins" each round, the
    persisted `moves` is a coherent sequence, no lost update. *Proves:* the `FOR
    UPDATE` invariant. If the builder defers this, flag it as an open gap (below) —
    it's the only test that proves the lock does its job.
  - **Which tests need the CI Postgres service:** all of `test_fixtures_import.py`,
    `test_fixtures_roundtrip.py`, `test_create_request_duplicate_name_409`, the
    `test_chess_games*` tests (state in a table), and the concurrency test — the
    import engine's transaction logic and the `FOR UPDATE` semantics can't be
    meaningfully faked with a `FakeConn`. CI already runs `postgres:17` for the
    `backend-tests` job (`.github/workflows/ci.yml` — the `services.postgres` block;
    `DATABASE_URL: postgresql://pagehub_evals:postgres@localhost:5533/...`), so this
    is free — but the test files must actually connect to it (a real asyncpg pool
    against `DATABASE_URL`, apply `schema.sql`, truncate between tests) the way
    `test_runs_engine_concurrency.py` does. Pure parse-time validations
    (`duplicate_name`, `secret_value_in_fixture`, `unsupported_version`, malformed
    FEN, unparseable UCI) and the 403/404 route checks can use the existing
    `FakeConn`-style route tests (`api/tests/test_runs_routes.py` pattern) since they
    never reach the DB.
- **Eval coverage** (separate repo `~/github/pagehub-io/platform`,
  `http://localhost:4002` — confirmed up):
  - **New seed `platform/evals/seeds/pagehub_evals_fixtures.py`** — model on
    `seeds/pagehub_evals_collections.py` + the shared `seeds/_pagehub_evals.py`
    helpers. Request templates + per-request `evaluations`:
    - `fixtures-import-chess` — POST `fixtures/chess.json` (the literal file content)
      to `/v1/fixtures/import` with the operator JWT. Capture `IMPORT1` (whole
      response body). Evals: `status_eq 200`; `json_path_eq $.collections.created ==
      2`; `json_path_eq $.requests.created` == the known count; `json_path_eq
      $.environments.created == 1`.
    - `fixtures-import-chess-again` — POST the same file again. Evals: `status_eq 200`;
      `json_path_eq $.collections.created == 0`; `json_path_eq $.requests.created ==
      0`; `json_path_eq $.environments.created == 0` — the SPEC idempotency
      success-criterion.
    - `fixtures-export-legality` — `GET /v1/collections/{{LEGALITY_ID}}/export`
      (`LEGALITY_ID` captured from a `GET /v1/collections?name=chess-legality` step or
      from the import response if it returns ids — adjust to the actual response
      shape). Capture `EXPORT_A`. Evals: `status_eq 200`; `json_path_eq
      $.environments` == `[]`; `json_path_eq $.collections[0].name == "chess-legality"`.
    - `fixtures-reimport-export-a` — POST `{{EXPORT_A}}` back to `/v1/fixtures/import`.
      Evals: `status_eq 200`; `json_path_eq $.collections.created == 0`.
    - `fixtures-export-legality-b` — `GET .../export` again. Capture `EXPORT_B`. Evals:
      `status_eq 200`. **The round-trip identity assertion:** ideally a `body_eq` (or
      whatever the evals layer's equality vocabulary is) between `EXPORT_A` and
      `EXPORT_B`. Since the evals layer can't run `normalize_for_roundtrip` itself,
      the cleanest approach is to make export stable enough that raw `EXPORT_A ==
      EXPORT_B` byte-for-byte (the Architect's analysis says it is — requests in
      first-referenced order, positions dense, no Nones if always-emit), so a plain
      `body_eq EXPORT_A EXPORT_B` works; if that proves flaky, fall back to
      field-by-field assertions (`$.requests[0].name`, etc.). **This is the SPEC's
      "checked-in eval asserting the export → import → export identity".**
    - Spec markdown `~/github/pagehub-io/platform/evals/specs/pagehub-evals/fixtures-import-export.md`
      — the user-story for the above (model on `specs/collections-crud.md`).
  - **New seed `platform/evals/seeds/pagehub_evals_chess.py`** (or fold into the
    fixtures seed — separate file is cleaner) — exercises the chess module endpoints
    *directly* (unauthenticated, per the contract):
    - `chess-legal-moves-startpos` — POST `/v1/modules/chess/legal-moves` with the
      startpos FEN. Evals: `status_eq 200`; `body_contains "e2e4"`; `json_path_eq
      $.turn == "white"`; `json_path_eq $.fen` == the normalized startpos FEN.
    - `chess-legal-moves-bad-fen` — POST `{"fen": "garbage"}`. Evals: `status_eq 422`
      — proves the 422-not-500 contract.
    - `chess-open-game` — POST `/v1/modules/chess/games {engine_color: "white", seed:
      0}`. Capture `GAME_ID` (`$.game_id`). Evals: `status_eq 200`; `json_path_eq
      $.turn == "black"`; `json_path_eq $.status == "in_progress"`.
    - `chess-move-1` — POST `/v1/modules/chess/games/{{GAME_ID}}/moves {move:
      "e7e5"}`. Capture `FEN1`. Evals: `status_eq 200`; `json_path_eq $.status ==
      "in_progress"`; `json_path_eq $.fen ==` the README's documented FEN after step 1.
    - `chess-move-illegal` — POST a clearly-illegal-for-the-position UCI. Evals:
      `status_eq 200`; `json_path_eq $.status == "illegal_move"` — the body-status
      contract.
    - `chess-move-unknown-game` — POST `/moves` to a random UUID. Evals: `status_eq
      404`.
    - Spec markdown `specs/pagehub-evals/chess-module.md`.
    - Note: the `chess-legality` and `chess-playable-game` collections *themselves* get
      exercised end-to-end as run collections in a follow-up (the JTBD pivot builds
      `POST /v1/runs` against them); this slice's seeds hit the module HTTP surface
      directly and prove the fixture imports/round-trips.
  - **Status:** the two platform seeds + two spec markdowns are part of this slice's
    *durable test surface* per the build skill — the builder **creates the files in the
    platform repo and reports them**, but the platform-repo PR is a sibling PR; do NOT
    block the pagehub-evals PR on its merge (Architect's Integrations note says exactly
    this). The builder must (a) write the seeds, (b) confirm they run against a local
    stack (`http://localhost:4002` is up), (c) report the sibling-PR branch in the
    pagehub-evals PR description.
- **Concurrency / fault injection:**
  - `api/tests/test_fixtures_import.py::test_all_or_nothing_rollback` — proves the
    single-transaction rollback invariant against real Postgres.
  - `api/tests/test_chess_games_concurrency.py` — proves the `/moves` `SELECT FOR
    UPDATE` invariant (no lost update under concurrent submits). Real Postgres against
    the `db` Compose / CI service; cannot be mocked.
  - Both follow the existing `api/tests/test_runs_engine_concurrency.py` shape.
- **Open coverage gaps (flagged so they're not silently lost):**
  - The chess engine's `(seed, ply)` determinism is only as good as the README's
    documented line — `test_deterministic_game_given_seed` pins it, but if the builder
    writes the README *after* the test, the two could be circular (test asserts what
    the code does, README copies the test). The README's expected FENs must be
    independently re-derivable (e.g. a comment showing the `python-chess` one-liner
    that produces them) — flag in PR review.
  - The `body_eq EXPORT_A EXPORT_B` round-trip eval assumes raw export bytes are
    stable (no normalizer in the evals layer). If they're not (e.g. the builder ships
    `response_model_exclude_none=True` after all, or dict ordering surprises us), the
    eval is flaky — the *pytest* `test_fixtures_roundtrip.py` (which *does* run
    `normalize_for_roundtrip`) is the real guard; the eval is a weaker mirror. The
    builder should confirm raw-byte stability before relying on `body_eq` in the seed.
  - No eval exercises the `chess-playable-game` *collection* as a run yet (needs `POST
    /v1/runs`, the JTBD-pivot slice) — this slice's eval coverage is the module HTTP
    surface + fixture import/export round-trip. Explicitly out of scope, not a gap in
    *this* slice — flagged so the next slice picks it up.

## Data

> Scope of DB changes this slice: (1) add `UNIQUE (owner_user_id, name)` to
> `requests`; (2) new `chess_games` table; (3) no column changes to
> `environments` / `collections` / `evaluations` / `collection_items` — the
> importer works against existing shapes. All DDL goes in
> `api/shared/schema.sql` and must be re-runnable every boot
> (`api/shared/schema.py:23` executes the whole file under
> `pg_advisory_xact_lock` each lifespan).

### Shapes

**`requests` — gains `UNIQUE (owner_user_id, name)`** (new). Today
`api/shared/schema.sql:25-37` declares `requests` with **no** name uniqueness —
multiple rows can share `(owner_user_id, name)`. Fixture upsert-by-name and
`GET /v1/collections/{id}/export` (which resolves items back to a request *name*)
both need the constraint. Postgres has no `ADD CONSTRAINT IF NOT EXISTS`, so add
this `DO` block right after the existing `ALTER TABLE requests ADD COLUMN IF NOT
EXISTS capture ...` line (`schema.sql:36`):

```sql
-- Idempotent: re-runnable every boot. Postgres has no ADD CONSTRAINT IF NOT
-- EXISTS, so guard on pg_constraint. Fixture import upserts requests by
-- (owner_user_id, name); without this, ON CONFLICT has no arbiter.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'requests_owner_user_id_name_key'
    ) THEN
        ALTER TABLE requests
            ADD CONSTRAINT requests_owner_user_id_name_key UNIQUE (owner_user_id, name);
    END IF;
END $$;
```

The constraint name `requests_owner_user_id_name_key` deliberately matches what
Postgres auto-generates for `UNIQUE (owner_user_id, name)`, so a hand-run
`ALTER TABLE ... ADD UNIQUE` and this guarded block converge on the same name.

> **Migration risk, accepted (stated):** if a deployed DB already holds two
> `requests` rows with the same `(owner_user_id, name)`, the `ALTER TABLE` raises
> and the whole `schema.sql` apply (hence lifespan boot) fails. Per SPEC the
> deployed DB is freshly scaffolded — slices 1-2 only added schema and a stub
> `runs` resource; the platform never bulk-loaded `requests` — so there are no
> duplicates to block it. Plain `ADD CONSTRAINT` (not `NOT VALID` + later
> `VALIDATE`) is therefore fine. If this ever runs against a *dirty* DB, a dedupe
> pass (`DELETE FROM requests a USING requests b WHERE a.ctid < b.ctid AND
> a.owner_user_id = b.owner_user_id AND a.name = b.name` plus re-pointing
> children) is required *before* the constraint can land — out of scope here,
> noted for whoever hits it.

**`chess_games` — new table.** Game state must survive a serverless cold start /
`uvicorn --reload` mid-run (the run engine would otherwise get a `404` halfway
through `chess-playable-game` → `error` verdict, flaky eval), so it is *not*
in-memory. Add after `collection_items` in `schema.sql`:

```sql
-- Server-driven chess games for the in-repo chess module (api/modules/chess).
-- Durable on purpose: a deploy landing mid-run must not drop the board.
-- Unauthenticated / anonymous — no owner column; rows are ephemeral-ish and a
-- slice-N reaper can prune by created_at (not this slice).
CREATE TABLE IF NOT EXISTS chess_games (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    engine_color  TEXT NOT NULL,                       -- 'white' | 'black' (side the server engine plays)
    seed          BIGINT NOT NULL,                     -- engine RNG seed, from the /games request body (default 0)
    starting_fen  TEXT NOT NULL,                       -- FEN the game opened from (startpos by default) — for replay
    fen           TEXT NOT NULL,                       -- current position
    move_history  JSONB NOT NULL DEFAULT '[]'::jsonb,  -- ordered UCI moves played (both sides); evidence + per-ply RNG replay
    move_count    INTEGER NOT NULL DEFAULT 0,          -- == jsonb_array_length(move_history); the ply index
    status        TEXT NOT NULL DEFAULT 'in_progress', -- in_progress | harness_won | harness_lost | draw
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Column rationale:
- `id UUID PK DEFAULT gen_random_uuid()` — `pgcrypto` is already enabled
  (`schema.sql:7`); games are looked up **only** by id (the `{{GAME_ID}}` capture
  threads it forward), so the PK is the sole access path.
- `engine_color` / `seed` — captured from the `POST /v1/modules/chess/games`
  request body (`engine_color`; `seed` default `0`, `SPEC.md:602`). `seed BIGINT`
  so any 64-bit int the author pins fits (and matches the Security section's
  `Field(ge=0, le=2**63-1)` on the request schema).
- `starting_fen` **and** `move_history` **and** `move_count` together — this is
  the fix for the Reliability/Architect flag (`SPEC.md:659`): a fresh
  `random.Random(seed)` per move gives the **same** move every call, so the
  engine's reply must be derived from `(seed, ply)` (e.g.
  `random.Random((seed, move_count))` over the sorted legal-move list) **or** by
  replaying `move_history` from `starting_fen` to re-advance one RNG. `move_count`
  alone is enough for the `(seed, ply)` derivation (cheapest, per the brief's
  recommendation); `move_history` is also stored because (a) it's evidence the
  run can show and (b) it lets the module reconstruct the board defensively
  rather than trusting only the denormalized `fen`. If the builder would rather
  compute the ply from `len(move_history)` on read, `move_count` can be dropped
  (it's the optional half) — recommendation: keep both, they're a few bytes and
  `move_count` is written `= len(move_history)` in the same `UPDATE`, so they
  cannot drift.
- `status` stored values are exactly the **terminal-or-ongoing** set:
  `in_progress` | `harness_won` | `harness_lost` | `draw`. Confirmed against the
  chess HTTP contract (`SPEC.md:608-612`): `illegal_move` is a **response**
  `status`, not a stored state — an illegal-given-the-position move is rejected,
  the board is unchanged, **no `UPDATE` runs**, the row stays `in_progress`. So
  `illegal_move` never appears in `chess_games.status`. No `CHECK` constraint on
  `status` (keeping DDL minimal, matching the rest of this schema's style) — it's
  an app invariant; a `CHECK (status IN (...))` could be added if drift is ever a
  concern, not recommended for the scaffold.
- `created_at` / `updated_at` — `updated_at` set by the module on each move (no
  trigger; the module owns its writes, consistent with the rest of this DB).
- **No `owner_user_id`** — the chess module is unauthenticated (`SPEC.md:615`);
  games are anonymous. Nothing to scope, nothing to wipe per-user.

**Indexes on `chess_games`: PK only.** Lookups are by `id` (PK covers it). No
`created_at` index this slice — SPEC says no reaper; add
`CREATE INDEX IF NOT EXISTS chess_games_created_idx ON chess_games (created_at)`
when/if a reaper lands. (Security section leans toward adding it now so a future
reaper is cheap — fine either way; it's a one-liner. My recommendation: skip it
this slice, the table is tiny and unscanned.)

**FK posture for `chess_games`: clean.** It references nothing; nothing
references it. No cascade considerations.

**No new columns on `environments` / `collections` / `evaluations` /
`collection_items`.** Confirmed against `schema.sql`:
- `environments` has `UNIQUE (owner_user_id, name)` (`schema.sql:19`) ✔ — the
  upsert arbiter the importer needs.
- `collections` has `UNIQUE (owner_user_id, name)` (`schema.sql:59`) ✔.
- `collection_items` has `UNIQUE (collection_id, position)` (`schema.sql:68`) ✔
  — the importer must therefore assign **dense** `position` `0..n-1` from the
  fixture array order, **after deleting the collection's existing items** (a
  partial overwrite could transiently collide on `position`). Note: the
  bundle-size cap the Security and Reliability sections recommend
  (`collections[].items` ≤ `api/runs/_constants.py:COLLECTION_ITEM_CAP` = 50, plus
  defensive caps on `requests[]` / `evaluations[]` and an optional raw-byte cap)
  is enforced **entirely at the Pydantic `FixtureBundle` model layer** — there is
  **no DB-side `CHECK` on item count or row count and none is added**; the
  importer always writes dense positions `0..n-1` regardless of `n`, and a
  bundle that exceeds the cap is rejected `422` before the transaction opens, so
  the DB never sees an over-cap collection. (Number-agnostic statement; the
  Security/Reliability sections own the actual figure.)
- `evaluations.request_id` is `REFERENCES requests(id) ON DELETE CASCADE`
  (`schema.sql:42`) ✔ — but the importer **does not delete the request row** (it
  `INSERT ... ON CONFLICT DO UPDATE`s it), so the cascade does **not** fire on
  re-import; the importer must `DELETE FROM evaluations WHERE request_id = $1`
  explicitly, then re-insert. Same logic for `collection_items` vs. `collections`.

**There is no `PATCH /v1/requests/{id}` route today** — confirmed:
`api/requests/routes.py` has only `POST ""` (`create_request`, line 53),
`GET ""` (line 84), `GET "/{request_id}"` (line 99). The `UpdateRequestRequest`
schema exists in `api/requests/schemas.py` but is unwired. So the new
`UNIQUE (owner_user_id, name)` adds a 409 surface only on `POST /v1/requests`
(see Integrity); nothing on PATCH because there is no PATCH.

### Migrations

All "migrations" are idempotent statements appended to `api/shared/schema.sql`,
re-applied every lifespan boot (`schema.py:23`). No separate migration runner.

| Step | Forward | Rollback | Online-safe? |
|---|---|---|---|
| Add `requests_owner_user_id_name_key` UNIQUE | `DO $$ ... pg_constraint guard ... ALTER TABLE requests ADD CONSTRAINT ... UNIQUE (owner_user_id, name) ... $$;` (DDL above) | `ALTER TABLE requests DROP CONSTRAINT IF EXISTS requests_owner_user_id_name_key;` | Builds a unique index → brief `SHARE` lock on `requests`. Table is near-empty on the deployed scaffold → effectively instant. Aborts boot iff pre-existing duplicate names — not the case here. Not concurrent (`CREATE INDEX CONCURRENTLY` can't run inside the schema-apply transaction; not needed at this size). |
| Create `chess_games` | `CREATE TABLE IF NOT EXISTS chess_games (...)` (DDL above) | `DROP TABLE IF EXISTS chess_games;` | Fully online — new table, no existing readers/writers, no data. |
| (`requests.capture` add) | already present (`schema.sql:36`), unchanged | n/a | n/a |

Both new statements are unconditionally safe to re-run: `CREATE TABLE IF NOT
EXISTS` is a no-op the second time; the `DO` block's `pg_constraint` guard makes
the `ALTER` a no-op once the constraint exists.

### Queries

Hot paths introduced this slice (resource CRUD is unchanged):

| Hot path | Query | Index used | Notes |
|---|---|---|---|
| Import: upsert one request | `INSERT INTO requests (owner_user_id,name,method,url,headers,body,capture) VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb) ON CONFLICT (owner_user_id,name) DO UPDATE SET method=EXCLUDED.method,url=EXCLUDED.url,headers=EXCLUDED.headers,body=EXCLUDED.body,capture=EXCLUDED.capture,updated_at=now() RETURNING id,(xmax = 0) AS inserted` | `requests_owner_user_id_name_key` (new unique index, as the ON CONFLICT arbiter) | One round trip per request; `chess.json` is a handful. |
| Import: replace a request's evals | `DELETE FROM evaluations WHERE request_id=$1` then one multi-row `INSERT INTO evaluations (request_id,name,kind,config) VALUES ($1,$2,$3,$4::jsonb),...` | `evaluations_request_idx` (`schema.sql:49`) for the delete | Delete-all-then-bulk-insert per request. |
| Import: upsert one collection | `INSERT INTO collections (owner_user_id,name,description) VALUES ($1,$2,$3) ON CONFLICT (owner_user_id,name) DO UPDATE SET description=EXCLUDED.description,updated_at=now() RETURNING id,(xmax = 0) AS inserted` | `collections_owner_user_id_name_key` (existing) | — |
| Import: replace a collection's items | `DELETE FROM collection_items WHERE collection_id=$1` then `INSERT INTO collection_items (collection_id,request_id,position) VALUES ($1,$2,$3),...` | `collection_items_collection_idx` (`schema.sql:70`) | Positions dense `0..n-1` from fixture array order. |
| Import: read existing env secrets (for merge) | `SELECT secrets FROM environments WHERE owner_user_id=$1 AND name=$2` | `environments_owner_user_id_name_key` (existing) | One read per env in the bundle, **before** the upsert (see Integrity §secrets). Row count = bundle env count; `chess.json` has zero envs, real bundles a handful — no batching needed. |
| Import: upsert one environment | `INSERT INTO environments (owner_user_id,name,variables,secrets) VALUES ($1,$2,$3::jsonb,$4::jsonb) ON CONFLICT (owner_user_id,name) DO UPDATE SET variables=EXCLUDED.variables,secrets=EXCLUDED.secrets,updated_at=now() RETURNING id,(xmax = 0) AS inserted` | `environments_owner_user_id_name_key` (existing) | `$4` is the **merged** secrets dict computed in Python (see Integrity). |
| Export a collection | (1) `SELECT id,name,description FROM collections WHERE id=$1` (2) `SELECT ci.position, r.* FROM collection_items ci JOIN requests r ON r.id=ci.request_id WHERE ci.collection_id=$1 ORDER BY ci.position` (3) `SELECT request_id,name,kind,config FROM evaluations WHERE request_id = ANY($1::uuid[]) ORDER BY request_id,created_at,name` | PK on `collections`; `collection_items_collection_idx` + PK on `requests` for the join; `evaluations_request_idx` for (3) | 3 queries total — **no N+1**; (3) is one batched query over the request-id array, not one-per-request. |
| Chess: load a game | `SELECT id,engine_color,seed,starting_fen,fen,move_history,move_count,status FROM chess_games WHERE id=$1` | PK | Single-row PK lookup, sub-ms. |
| Chess: create a game | `INSERT INTO chess_games (engine_color,seed,starting_fen,fen,move_history,move_count,status) VALUES (...) RETURNING id` | — | If `engine_color=="white"` the module computes the first move in the same request and the INSERT carries the post-move `fen`/`move_history`/`move_count=1`. |
| Chess: apply a move | `SELECT ... FROM chess_games WHERE id=$1 FOR UPDATE` then `UPDATE chess_games SET fen=$2,move_history=$3::jsonb,move_count=$4,status=$5,updated_at=now() WHERE id=$1` | PK | Single-row update; run inside the request's txn so harness-move + engine-reply land atomically (Integrity §chess). |

The `ON CONFLICT DO UPDATE ... SET col=EXCLUDED.col` form writes every column
unconditionally even when values are unchanged — intentional: declarative
desired-state, and SPEC's `{created, updated}` response has no `unchanged` bucket
(an `updated` row is one that *existed and was touched*, period). `(xmax = 0)` —
see Integrity.

### Retention & PII

- **`chess_games`** — grows **unbounded** within a slice (one row per game ever
  created; never deleted this slice). Row size ≈ 200–400 bytes (a FEN ~70 chars,
  a short UCI move list, a handful of scalars). At eval-suite cadence
  (`chess-playable-game` opens one game per run; CI runs the suite occasionally)
  that's **dozens of rows per CI run** — trivial; even at 100k games it's tens of
  MB. **No reaper this slice (SPEC).** When/if it matters:
  `DELETE FROM chess_games WHERE created_at < now() - interval '7 days';` (cron or
  a lifespan task) — and at that point add `chess_games_created_idx`. **No PII**
  in `chess_games` — no owner, no IP, no harness id; just board state. Nothing to
  anonymize. (Anonymous unbounded creation is a capacity concern, covered in the
  Security section; not a data-correctness one.)
- **Fixtures are not stored in the DB.** They're checked-in files
  (`fixtures/*.json`) and, on import, fan out into the **existing** `environments`
  / `requests` / `evaluations` / `collections` / `collection_items` rows — there
  is **no `fixtures` table**, hence no fixture-retention or fixture-PII concern.
- **Secret material never traverses the fixture boundary.** A fixture carries
  secret *keys* with `""` values only (non-empty → 422 at parse time,
  `SPEC.md:541`); on import the engine **preserves** the target environment's
  existing Fernet ciphertext for a known key and writes `encrypt("")` for a new
  key — `decrypt` is never called on the import path, and `GET .../export`
  always emits `environments: []`, so no decrypted secret is ever written to a
  git-tracked file or an export response. `environments.secrets` retention is
  unchanged from today (Fernet ciphertext at rest, lives as long as the env row).
- **`events`** — one append-only `fixtures.imported` row per successful import
  (`actor_kind="user"`, `actor_id` = operator id, `target_kind="fixtures"`,
  `target_id=NULL`, `payload` = the `{created, updated}` counts — **not** the
  bundle contents). Same retention as the rest of `events` (append-only, no
  reaper). Payload holds no secrets and no request bodies — just counts — so no
  new PII surface. (Whether the chess module emits a `chess_game.created` event
  is an open item in the Security section; if it does, the payload is
  `{seed, engine_color}` — non-sensitive.)

### Integrity

| Invariant | Enforcement |
|---|---|
| A `(owner_user_id, name)` pair has at most one `requests` row | **DB constraint** `requests_owner_user_id_name_key` UNIQUE (new). Backstops the importer's `ON CONFLICT` arbiter and makes `POST /v1/requests` deterministic and `GET .../export`'s item→name resolution unambiguous. |
| `POST /v1/requests` with a duplicate name → 409, not 500 | **App-level** on top of the DB constraint: wrap the `INSERT` in `create_request` (`api/requests/routes.py:58-71`) in `try/except asyncpg.UniqueViolationError → raise HTTPException(status_code=409, detail="A request named '<name>' already exists")`, mirroring `add_item` in `api/collections/routes.py:131-145`. Needs `import asyncpg` at the top of `requests/routes.py` (not currently imported there). Discrete change — list it in the build checklist. (Note: `POST /v1/collections`, `POST /v1/environments`, `PATCH /v1/environments/{id}` do **not** currently catch their `(owner_user_id, name)` violations — they 500; tightening those is out of scope, only `requests` is touched because this slice makes the violation reachable on `requests`.) |
| Idempotent re-import: a second import of the same bundle creates nothing | **DB + app** — `ON CONFLICT (owner_user_id, name) DO UPDATE` on all three top-level resources means re-import only ever `UPDATE`s; `created` counts come from the `(xmax = 0)` flag (true ⇒ this row was `INSERT`ed, false ⇒ `UPDATE`d). On re-import every flag is false ⇒ `created: 0` per kind ⇒ the idempotency signal SPEC's success criteria key on. |
| `created` vs `updated` counted without a prior SELECT | **App-level, via the `(xmax = 0)` Postgres idiom.** `INSERT ... ON CONFLICT DO UPDATE ... RETURNING id, (xmax = 0) AS inserted`: a freshly-inserted heap tuple has `xmax = 0`; an updated one inherits a non-zero `xmax` from the conflicting tuple it superseded. **Soundness caveat (stated):** `xmax` can be non-zero on a genuine insert if the row is locked/updated by a *concurrent* transaction within the same statement — vanishingly unlikely here (per-operator imports, tiny bundles, single-writer in practice), and the worst case is a miscount in the **response**, never wrong data. Acceptable; documented. |
| Children (`evaluations`, `collection_items`) reflect the fixture exactly (declarative desired-state) | **App-level: delete-all-children-then-bulk-insert per parent**, inside the import transaction. `DELETE FROM evaluations WHERE request_id=$1` then re-insert the fixture's list; `DELETE FROM collection_items WHERE collection_id=$1` then re-insert. The parent row survives (it was upserted, not deleted), so `ON DELETE CASCADE` does **not** fire — the explicit `DELETE` is load-bearing, not redundant. Stale rows present in the DB but absent from the fixture are dropped (silently, per SPEC; the loud OpenAPI doc is the only surfacing). |
| `collection_items.position` is dense `0..n-1` and matches fixture array order | **DB constraint** `UNIQUE (collection_id, position)` + **app** assigning `position = array_index` from the fixture **after** the per-collection `DELETE` (so no transient `position` collision). Export emits items in `ORDER BY position`, so the round-trip is stable. |
| Every collection-item name resolves to a request in the **same** bundle | **App-level, in-memory** (bundle-only resolution, `SPEC.md:542`): the engine builds a `name → request_id` map **as it upserts the bundle's requests** (requests are processed before collections within the txn), then resolves each `items[]` name against that map; an unresolved name → 422 (`{"error":"unresolved_request_ref",...}`), rolling back. No fall-back to whatever request happens to be stored under that name (that would make import non-deterministic across instances — the drift this feature kills). |
| FKs satisfied during import | **App-level write ordering**: within the single import transaction, process `environments` → `requests` (+ their `evaluations`) → `collections` (+ their `collection_items`). `collection_items.request_id` and `evaluations.request_id` therefore always reference rows already inserted/upserted earlier in the same txn. Because resolution is bundle-only, **every** request a collection item references is upserted before the `collection_items` insert. |
| Whole import is all-or-nothing | **DB transaction**: the route wraps `engine.import_bundle` in one `async with auth.db.transaction():` (mirrors `api/runs/...` and the SPEC Architect section). One bad eval `kind` in a 50-request bundle rolls back **everything** — no partial state, no half-imported fixture. |
| Secrets merge — fixture key with `""` value never clobbers stored ciphertext | **App-level read-then-merge** (NOT a blind JSONB overwrite): per env in the bundle, `SELECT secrets FROM environments WHERE owner_user_id=$1 AND name=$2`; in Python `merged = dict(stored)`, then for each fixture secret key `k` (value is `""`, parse-time enforced): if `k not in merged` set `merged[k] = encrypt("")` (new key → unset placeholder), else leave `merged[k]` (stored ciphertext) untouched; **stored keys the fixture doesn't mention are carried forward** (they're already in `merged` from the `dict(stored)` copy) — import never *deletes* a secret key, only adds/preserves (matches "top-level resources are upserted, not replaced"; the declarative-replace rule applies to *children*). Then `INSERT ... ON CONFLICT DO UPDATE SET variables=$3::jsonb, secrets=$4::jsonb, updated_at=now()` with `$4 = json.dumps(merged)`. A non-empty fixture secret value never reaches this code — it's a 422 at Pydantic parse time, before any DB read. This is the same algorithm the Security section describes ("import preserves stored ciphertext for known keys, writes `encrypt('')` for new keys, never calls `decrypt`") — read-then-merge-then-upsert is just the mechanical spelling of it. **Concurrency:** within a single import the whole sequence is one transaction, so the `SELECT` and the `ON CONFLICT` upsert see a consistent snapshot. Two imports running concurrently *for the same `owner_user_id` + env name* serialize on the conflicting-INSERT's row lock — the second blocks until the first commits, then its `DO UPDATE` runs against the post-first-commit row. Residual (minor, placeholder-only): the second import's `merged` dict was computed from a `SELECT` that ran *before* the first committed, so a brand-new secret *key* the first import added (value `encrypt("")`) that the second import's fixture doesn't mention could be dropped by the second's `SET secrets = $4`. In practice both concurrent imports are the *same* fixture (CI + an operator), so they declare the same key set and this can't bite; and the lost value is only ever an unset `encrypt("")` placeholder, never real credential material (real values only ever arrive via `PATCH /v1/environments/{id}`, which this path never touches). If the trio wants it airtight, push the merge into SQL — `SET secrets = EXCLUDED.secrets || environments.secrets` (existing keys' stored ciphertext wins; new keys take the `encrypt("")` placeholder from `EXCLUDED`) — which is atomic and needs no pre-`SELECT` at all; I'd take that simplification, but it's not required for correctness at this scale. |
| Round-trip fidelity: `export → import → export` byte-identical (modulo timestamps/ids — in practice modulo nothing) | **App-level, by construction**: export emits requests in first-referenced order and assigns `collection_items.position` densely from array index, so the shared `normalize_for_roundtrip` (`api/fixtures/schemas.py`) sort/strip steps are no-ops on export-produced bundles. `requests.body` is arbitrary JSON stored as **JSONB** — JSONB does **not** preserve key order or insignificant whitespace and may re-render numbers / collapse duplicate keys, so the *first* export of a hand-authored fixture can differ from the source **file** (the body has been through JSONB once); but the *second* export equals the first (it's been through JSONB the same way), so `export → import → export` is stable even though `file → import → export` is not. SPEC's invariant is the former (`SPEC.md:22,30,180-191`) — **confirmed**; JSONB normalization is fine. **Builder flag:** author `fixtures/chess.json` with sorted keys / JSONB-canonical bodies so `import(chess.json) → export` is *also* diff-clean (otherwise a contributor diffing a fresh export against the committed file sees spurious churn) — or accept that only round-trips, not file-vs-export, are guaranteed and say so in `fixtures/README.md`. Recommendation: sorted keys in the committed fixture. Also confirmed: `id` / `created_at` / `updated_at` / `owner_user_id` / `request_id` / `collection_id` / `position` must **not appear in the Fixture Pydantic models at all** (`FixtureBundle` and friends), so they never leak into an export in the first place — `normalize_for_roundtrip` stripping them is a belt-and-braces guard against hand-authored input, not the primary mechanism. |
| `chess_games`: a move applies atomically (harness move + engine reply + status, or nothing) | **DB transaction (per request)** + **app**: the `POST /v1/modules/chess/games/{id}/moves` handler does `SELECT ... FROM chess_games WHERE id=$1 FOR UPDATE` (or runs the whole handler in one txn), computes the new board / engine reply / status in Python, then a single `UPDATE chess_games SET fen=...,move_history=...,move_count=...,status=...,updated_at=now() WHERE id=$1`. Concurrent moves on the same `game_id` serialize on the row lock — last writer's view is consistent. (`chess.json`'s playable-game collection is a strictly sequential run anyway, so contention is theoretical, but the row lock makes it correct under it.) `move_count` is written `= len(move_history)` in the same statement — the denormalization cannot drift. |
| `chess_games.status` only ever holds a stored state, never `illegal_move` | **App-level**: the module returns `status:"illegal_move"` in the **response body** for an illegal-given-position move and leaves the row untouched (no `UPDATE`) — `illegal_move` is never written to the column. (No DB `CHECK` on `status`, by choice; an app invariant.) |
| `chess_games` has no orphan / no dangling reference | **N/A by design** — `chess_games` references nothing and nothing references it. Cleanest possible FK posture. |

### Constraints relied on — summary

| Constraint | Status | Used by |
|---|---|---|
| `requests UNIQUE (owner_user_id, name)` (`requests_owner_user_id_name_key`) | **NEW** (idempotent `DO` block in `schema.sql`) | importer's `ON CONFLICT` arbiter; `POST /v1/requests` 409; unambiguous item-name → request resolution on export |
| `environments UNIQUE (owner_user_id, name)` | existing (`schema.sql:19`) | importer env upsert |
| `collections UNIQUE (owner_user_id, name)` | existing (`schema.sql:59`) | importer collection upsert |
| `collection_items UNIQUE (collection_id, position)` | existing (`schema.sql:68`) | importer assigns dense `position 0..n-1` after per-collection delete |
| `evaluations.request_id FK ON DELETE CASCADE` | existing (`schema.sql:42`) | present, but importer deletes evals **explicitly** by `request_id` (it upserts, never deletes, the parent request — cascade doesn't fire); same logic for `collection_items.collection_id` |
| `chess_games` PK on `id` only | **NEW** | sole lookup path for a game |

### Import write-sequence — exact pseudocode (one bundle, inside one txn)

```text
# Route: async with auth.db.transaction():  →  engine.import_bundle(conn, owner_id, bundle)
# (FixtureBundle already validated by Pydantic — bad version / bad eval kind|config /
#  non-empty secret value / object-form collection item / duplicate-name-within-bundle
#  already rejected as 422 before we get here.)

counts = {k: {"created": 0, "updated": 0} for k in ("environments","requests","evaluations","collections")}
warnings = []                      # volatile-field-stripped warnings collected at the Pydantic layer / here
name_to_request_id = {}

# 1. ENVIRONMENTS  (read-then-merge for secrets)
for env in bundle.environments:
    stored = await conn.fetchval(
        "SELECT secrets FROM environments WHERE owner_user_id = $1 AND name = $2", owner_id, env.name)
    stored = json.loads(stored) if isinstance(stored, str) else (stored or {})
    merged = dict(stored)                          # carry forward stored keys the fixture omits
    for k in env.secrets:                          # every value is "" (parse-time enforced)
        if k not in merged:
            merged[k] = encrypt("")                # new key -> unset placeholder
        # else: keep stored[k] ciphertext untouched
    row = await conn.fetchrow(
        """
        INSERT INTO environments (owner_user_id, name, variables, secrets)
        VALUES ($1, $2, $3::jsonb, $4::jsonb)
        ON CONFLICT (owner_user_id, name) DO UPDATE
          SET variables = EXCLUDED.variables, secrets = EXCLUDED.secrets, updated_at = now()
        RETURNING id, (xmax = 0) AS inserted
        """,
        owner_id, env.name, json.dumps(env.variables), json.dumps(merged))
    counts["environments"]["created" if row["inserted"] else "updated"] += 1

# 2. REQUESTS  (+ their evaluations, delete-all-then-reinsert)
for req in bundle.requests:
    row = await conn.fetchrow(
        """
        INSERT INTO requests (owner_user_id, name, method, url, headers, body, capture)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb)
        ON CONFLICT (owner_user_id, name) DO UPDATE
          SET method = EXCLUDED.method, url = EXCLUDED.url, headers = EXCLUDED.headers,
              body = EXCLUDED.body, capture = EXCLUDED.capture, updated_at = now()
        RETURNING id, (xmax = 0) AS inserted
        """,
        owner_id, req.name, req.method, req.url,
        json.dumps(req.headers),
        json.dumps(req.body) if req.body is not None else None,
        json.dumps(req.capture))
    rid = row["id"]
    name_to_request_id[req.name] = rid
    counts["requests"]["created" if row["inserted"] else "updated"] += 1

    await conn.execute("DELETE FROM evaluations WHERE request_id = $1", rid)
    for ev in req.evaluations:
        await conn.execute(
            "INSERT INTO evaluations (request_id, name, kind, config) VALUES ($1, $2, $3, $4::jsonb)",
            rid, ev.name, ev.kind, json.dumps(ev.config))
    # eval set counts under the PARENT request's bucket (SPEC: no per-eval created/updated)
    counts["evaluations"]["created" if row["inserted"] else "updated"] += len(req.evaluations)

# 3. COLLECTIONS  (+ their items, delete-all-then-reinsert, bundle-only name resolution)
for col in bundle.collections:
    row = await conn.fetchrow(
        """
        INSERT INTO collections (owner_user_id, name, description)
        VALUES ($1, $2, $3)
        ON CONFLICT (owner_user_id, name) DO UPDATE
          SET description = EXCLUDED.description, updated_at = now()
        RETURNING id, (xmax = 0) AS inserted
        """,
        owner_id, col.name, col.description)
    cid = row["id"]
    counts["collections"]["created" if row["inserted"] else "updated"] += 1

    await conn.execute("DELETE FROM collection_items WHERE collection_id = $1", cid)
    for position, item_name in enumerate(col.items):           # array index == position
        rid = name_to_request_id.get(item_name)
        if rid is None:
            raise FixtureError(422, {"error": "unresolved_request_ref",
                                     "collection": col.name, "item_index": position, "request": item_name})
        await conn.execute(
            "INSERT INTO collection_items (collection_id, request_id, position) VALUES ($1, $2, $3)",
            cid, rid, position)

# any raised FixtureError -> route maps to HTTPException(422, ...) and the txn rolls back -> DB unchanged
return FixtureImportResponse(**counts, warnings=warnings)
```

(Each `INSERT INTO evaluations` / `collection_items` is shown one-row-at-a-time
for clarity; the builder may batch into a single multi-row `INSERT ... VALUES
(...),(...),...` per parent — pure perf, no semantic change. `executemany` is
fine too.)

### Export read-sequence — exact pseudocode

```text
# Route: api/collections/routes.py — GET /v1/collections/{id}/export, require_user
async def build_export(conn, collection_id):
    col = await conn.fetchrow("SELECT id, name, description FROM collections WHERE id = $1", collection_id)
    if col is None:
        raise HTTPException(404, {"error": "collection_not_found"})

    items = await conn.fetch(
        """
        SELECT ci.position, r.id AS rid, r.name, r.method, r.url, r.headers, r.body, r.capture
        FROM collection_items ci JOIN requests r ON r.id = ci.request_id
        WHERE ci.collection_id = $1 ORDER BY ci.position
        """, collection_id)

    # requests[]: distinct, in first-referenced (== position) order
    seen, requests_out, ordered_rids = set(), [], []
    for it in items:
        if it["rid"] not in seen:
            seen.add(it["rid"]); ordered_rids.append(it["rid"])
            requests_out.append({...})          # name, method, url, headers, body, capture — NO id/owner/timestamps

    evals = await conn.fetch(
        """
        SELECT request_id, name, kind, config FROM evaluations
        WHERE request_id = ANY($1::uuid[]) ORDER BY request_id, created_at, name
        """, ordered_rids)                      # ONE batched query — no N+1
    # group evals by request_id, attach inline to each requests_out entry in created_at-then-name order

    return FixtureBundle(
        version=1,
        environments=[],                        # ALWAYS [] on export (SPEC) — never key-omitted
        requests=requests_out,
        collections=[{"name": col["name"], "description": col["description"],
                      "items": [it["name"] for it in items]}])      # request NAME strings, position order
```

3 queries, no N+1. `environments: []` is literal (an empty list field, not an
omitted key) so the round-trip normal form is stable.

