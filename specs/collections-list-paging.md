# Collections list: paging, name and owner filters (#30)

**Status:** plan rev 1 (2026-10-08), for spec review. The owner asked for it from the Releases board: "Build the
fix for pagehub-evals#30 (the 500-collection limit), through the normal spec and review gates."

## 1. Problem (executed 2026-10-08 against the local stack at 5c91177)

- `GET /v1/collections` (`api/collections/routes.py:80-93`) returns `ORDER BY created_at DESC LIMIT 500`. It has no
  query parameters, so a row past 500 can't be reached through the list at all.
- The local evals DB holds 748 collections under 296 names. Most extra rows are stale copies: an import as a new
  operator identity forks a new `(owner_user_id, name)` row (`api/fixtures/engine.py:160-162`).
- A plain `GET /v1/collections` with an operator JWT returned exactly 500 rows, with no paging keys. Give Happy's gate
  sync failed with `collection 'give-happy-auth' missing after import`: that collection's newest copy ranked 510.
- Consumers that list collections and pick by name (read, not executed for the ones not run):
  - Give Happy: `evals/tools/run_evals.py` (executed: it's the one that failed);
  - serve: `evals/tools/run_pagehub_evals.py` and `scripts/run-evals.py`;
  - prayers: `evals/tools/run_pagehub_evals.py` and `run_platform_suite.py`;
  - `pagehub-benchmarks/pagehub_benchmarks/grader/client.py`;
  - `platform/evals/seeds/_helpers/pagehub_evals_client.py`.
- Each listed row also costs one more query for its items (`_row_to_response` → `_load_items`), so a full list is
  501 queries.

Every other list route has the same `LIMIT 500` (environments, requests, runs, harness keys, evaluations, events).
Only collections is past it today: environments has 20 rows locally. Those stay out of scope (§8).

## 2. Contract

`GET /v1/collections`, still operator-only (`require_user`). New, all optional, query parameters:

| Param | Type | Meaning |
|---|---|---|
| `limit` | int, 1–500, default 500 | Page size |
| `cursor` | string, max 200 chars | Opaque. From a previous response's `next_cursor`. Continue after that row |
| `name` | string, repeatable, 1–50 values, each 1–200 chars | Only rows whose `name` equals one of these, exactly |
| `owner` | `me` only | Only rows owned by the caller (`owner_user_id = auth.actor_id`) |

- **Order:** `created_at DESC, id DESC`. The `id` tiebreak is new; today two rows with the same `created_at` have no
  defined order. Today's order is otherwise unchanged.
- **Keyset paging:** the cursor carries the last row's `(created_at, id)`, and the next page is
  `WHERE (created_at, id) < ($1, $2)`. The query fetches `limit + 1` rows. If the extra row exists, it's dropped, and
  `next_cursor` encodes the last returned row.
- **Cursor format:** base64url (unpadded) of the JSON `{"c": "<created_at ISO 8601 with offset>", "i": "<uuid>"}`.
  Clients treat it as opaque. Decoding is strict: bad base64, bad JSON, missing keys, an unparseable timestamp, a
  timestamp without an offset, or a bad UUID each answer **422** `{"detail": "invalid cursor"}`.
- **Filters and cursor together:** the cursor isn't bound to the filters. A client continues a listing by sending
  the same filters with the cursor. A cursor sent with different filters just starts that listing at the cursor's
  position. That's harmless, because every row is one the caller can already list (§4).
- **Response:** `CollectionListResponse` gains `next_cursor: str | None`. It's `null` on the last page, and is always
  present: no `response_model_exclude_none`, because `null` says "no more". `items` is unchanged.
- **Validation (FastAPI `Query`):**
  - `limit` outside 1–500 → 422;
  - more than 50 `name`s, or a `name` that's empty or longer than 200 characters → 422;
  - `owner` other than `me` → 422.
- **With no parameters,** the call returns the same first 500 rows as today in the same shape, plus `next_cursor`.
  Existing clients see no change: each consumer in §1 reads `.get("items")` or `["items"]` from a dict (read).
- **Same names as the platform client already expects:** `platform/evals/seeds/_helpers/pagehub_evals_client.py:48`
  (`_get_all_pages`) already sends `cursor` and follows `next_cursor` until it's falsy. Today it stops after one
  capped page. After this change it pages to the end, with no client change.
- **A new capability:** `collections_list_filters` is added to `CAPABILITIES` (`api/schemas.py`), so `/health`
  advertises it. Consumers probe it before relying on `name`, `owner` or `cursor`, as they already do for other
  capabilities. Against an older server, an unknown query parameter is silently ignored, and the client would get
  today's capped list and think it was filtered. The probe is what prevents that.

## 3. Implementation

- **`api/collections/routes.py` `list_collections`:**
  - Build one parameterized SQL with optional `name = ANY($n::text[])`, `owner_user_id = $n` and keyset clauses.
    Placeholders only, never string-interpolated values.
  - `ORDER BY created_at DESC, id DESC LIMIT $n`, with `limit + 1`.
- **Items in one query:** a new `_load_items_for(db, ids) -> dict[UUID, list[CollectionItemResponse]]` runs
  `WHERE collection_id = ANY($1) ORDER BY collection_id, position ASC`.
  - The list builds each `CollectionResponse` from that map, with an empty list for a collection that has no items.
  - That's 2 queries per page instead of `1 + rows`.
  - `get_collection`, `create_collection` and the item routes keep `_row_to_response`.
- **Cursor helpers:** `_encode_cursor(created_at, id)` and `_decode_cursor(s) -> (datetime, UUID)` live in the same
  module and raise `HTTPException(422, "invalid cursor")`.
- **`api/collections/schemas.py`:** `next_cursor: str | None = None` on `CollectionListResponse`.
- **`api/schemas.py`:** add `"collections_list_filters"` to `CAPABILITIES`, and update the health test that pins the
  tuple.
- **No schema or index change.** The table holds hundreds of rows, and `(owner_user_id, name)` already has its unique
  index. A `(created_at DESC, id DESC)` index can follow if a deployed DB ever grows large (§8).

## 4. Who can do what: what does an attacker who controls X get?

| Actor | Before | After |
|---|---|---|
| Harness key | 403 (`require_user`) | 403, unchanged |
| Operator JWT (any operator) | Lists the newest 500 rows, any owner. Reads any row by id | Can page through every row, any owner, and filter by name or self. **New:** rows past 500 can now be enumerated without knowing their ids |
| Unauthenticated | 401 | 401, unchanged |
| Crafted `cursor` | n/a | Only moves the start position of a listing the caller can already see. Values are bound as parameters, so a tampered cursor can't inject. Anything malformed is a 422 |
| Many `name`s, or a huge `limit` | n/a | Capped at 50 names of 200 characters and 500 rows. Anything over is a 422 before the DB is touched |

The one widening, operators enumerating rows past 500, matches the route's existing design. The listing was never
owner-scoped (every operator sees every owner's newest 500), and the 500 cap was a page size, not an access rule.
Collections hold request names and item order. Secrets live in environments, which this doesn't touch.

## 5. Failure modes

| If | Then |
|---|---|
| A row is inserted while a client pages | It's newer than the cursor, so it isn't returned by later pages. No duplicates and no skips among rows that existed at the start |
| A row is deleted while a client pages | It's simply absent. The keyset never depends on the row still existing |
| Two rows share `created_at` | The `id` tiebreak orders them, and the keyset `<` on the tuple keeps paging exact |
| A client sends filters to an old server | The parameters are ignored, and the client gets today's list. Clients guard with the capability probe (§2) |
| A cursor from a different filter set | Starts there. No error and no exposure (§2) |

## 6. Tests (`api/tests/test_collections_list.py`)

The real-Postgres tier uses `api/tests/_db.py`'s `db_pool`, as `test_fixtures_import.py` does, and skips cleanly
without `DATABASE_URL`. CI runs it against its Postgres service.

1. **No parameters, 3 rows:** `items` are newest first, `next_cursor` is `null`, and each row's `items` match its
   collection, by position.
2. **Paging:** 7 rows with `limit=3`, following `next_cursor`, give every row exactly once in `(created_at, id) DESC`
   order across 3 pages. The last page has `next_cursor: null`. Two of the rows share one `created_at`, so the
   tiebreak is exercised.
3. **`name`:** repeated `name=a&name=b` returns only rows named `a` or `b`, from both owners.
4. **`owner=me`:** with rows from actors A and B, A gets only A's.
5. **`name` with `owner=me`:** both filters apply together.
6. **422s:** `limit=0`, `limit=501`, 51 `name`s, an empty `name`, `owner=someone`, and the bad cursors from §2's list.
   These run in the no-DB tier, with the exploding connection: rejected before the DB.
7. **Items:** a page of 3 collections with 0, 1 and 3 items gets each its own items in position order. The batching
   regression test counts `fetch` calls on a wrapped connection: 2 per page.
8. **Harness key → 403**, unchanged.
9. **`/health` lists `collections_list_filters`.**

## 7. Release

- One build PR, then a fresh review, CI green (lint, type-check, pytest with Postgres), and merge.
- **Local:** the fleet's `:8002` is built from `pagehub-evals-gate-main` under `-p pagehub-evals`. It's rebuilt at the
  merge commit **only after any running gate has finished**, after checking with the sessions that use it (serve's
  gate may be running).
- **End to end:** Give Happy's full local gate runs against the rebuilt `:8002` and must stay at 0 failures. It lists
  collections and resolves by name.
- **Consumers adopt in their own PRs.** The server change is backward compatible, so nothing breaks before they do.
  - Give Happy's `run_evals.py` is first, in its own plan-light PR, through its review: probe the capability, then
    list with `name=` for its bundles, at most 50 per call, and keep the newest-`updated_at` choice. That PR is what
    retires the fresh-operator-id workaround.
  - serve, prayers, pagehub-benchmarks and the platform seeds get a pointer on #30 to adopt.
- **Remote deploy: not in this slice** (an owner question, Q1). `pagehub-evals-staging` runs 3e518fd from May, 8
  commits behind `main` (#18–#29, none of them deployed). Production has no deployment, and no gate uses either host.
  - A `staging-*` tag here would ship all 8 commits at once. The last public-repo deploy dispatch, on 2026-05-23,
    failed.
  - Catching the remote up is its own decision and its own check.

## 8. Out of scope, as follow-ups

- The same `LIMIT 500` on environments, requests, runs, harness keys and evaluations. None is past the cap locally
  today.
- Removing stale fork copies (#30's option 3). That needs the owner's go, and paging makes it unnecessary for
  correctness.
- Consumers looking up by the ids the import now returns (#25, #30's option 2).
- A `(created_at DESC, id DESC)` index.

## 9. Owner questions

- **Q1 (recommended: no):** should this slice also catch up the remote `pagehub-evals-staging` (8 commits)? The
  recommendation is a separate slice, because none of the fleet's gates use it.
