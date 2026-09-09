# Engine parity for migrated suites

Assertion kinds, per-request timeout, run builtins, config substitution,
filter captures, transient retry, run budget, item cap, capabilities on
`/health`.

**Status:** plan rev 9 (2026-09-09). **Gate passed in rev 8 review**: 0
critical, 0 important, 5 nits, swept here without design change.
Rev 8: Rev 7 review: 0 critical, 1 important
(the redact-then-truncate rule was applied to the new evidence field only,
while the existing body excerpt and error string truncate first in the same
function), 6 nits; folded in. Rev 7: Rev 6 review: 0 critical, 1 important
(a wording error: the platform seed cannot read the cap at seed time and
must hard-code it in lockstep), 5 nits; folded in. Rev 6: Rev 5 review: 0 critical, 2
important (the cap bump turns a platform contract eval red unless that seed
moves in lockstep; `/health` returns a raw dict and needs a response model
for the capabilities to exist in OpenAPI), 4 nits; folded in. Rev 5: Rev 4 review: 0 critical, 3
important (a trickling response defeats a per-chunk read timeout, so the
budget's "plus one item" needed a per-attempt ceiling; the skipped-item
record used a `status` field this schema does not have; the transient
classification must come from the exception arm, not the error string, and
`UnsupportedProtocol` must not count), 8 nits; folded in, plus a
`capabilities` list on `/health` that the first consumer probes. Rev 4: Rev 3 review: 0 critical, 4
important, all in the retry section (it claimed platform parity while
deviating twice; the wall-clock bound was wrong; `attempts` needed a schema
default; idempotency was unstated), 8 nits; folded in, and a run-level
budget added so the wall-clock argument stops being an argument. Rev 3: absorbed five consumer-driven capabilities. Rev 1 review: 1 critical, 6 important,
9 nits. Rev 2 review: 0 critical, 2 important (path grammar; the round-trip
invariant as written was red on `main`), 9 nits. All folded in. Rev 3 also
absorbs five capabilities that the review of the first consumer's plan
(serve, `specs/evals-as-fixtures.md` in that repo) showed the engine lacks:
without them 34 of its 40 collections cannot run at all. Implementation follows this revision.

## Problem

`fixtures/README.md` sets the direction: fixture bundles replace imperative
`platform/evals/seeds/`-style population. The first real suite being
migrated is an application suite of 881 requests in 40 collections with
1,625 conditions; 355 requests are browser steps through `pagehub-browser`.
Converted faithfully, it needs seven things the engine does not have. Each
is measured against that suite; each also generalises.

| Gap | Evidence in the suite |
|---|---|
| Assertions beyond the four kinds | 415 "path exists", 16 "numeric ≥", 3 "path absent", 14 "path contains" |
| Per-request timeout | 231 requests declare 15 to 35 s; the engine's fixed 10 s gives up before a `pagehub-browser` wait (default 10 s, up to 28 s in the suite) does |
| A per-run unique value | 144 requests use `{{RUN_ID}}` in org slugs and signup emails; the platform runner injects it; here it renders literally and every rerun collides on unique slugs |
| `{{VAR}}` inside assertion config | 12 `body eq` conditions expect `{{USER_ID}}`, `EvalOrg-{{RUN_ID}}`, …; `json_path_eq` compares the literal template today |
| A filter capture | `$.roles[?(@.name=='default')].id`, feeding two later requests; today `_resolve_path` returns `_MISSING` and the capture is silently empty |
| Transient retry | the platform runner re-fires connection errors and 429/502/503/504 up to 3 more times (4 attempts total) for every request; a cold-pool 503 passes there and fails here |
| Item cap | two collections have 53 and 51 items; `COLLECTION_ITEM_CAP` is 50 on `main` (PR #17 raises it to 90, unmerged) |

## Shape

### 1. Four new evaluation kinds

Same registry pattern as the existing four: an `EvaluationKind` member, a
strict per-kind config model in `api/evaluations/schemas.py`, an evaluator
in `api/runs/engine.py`, an entry in `_KINDS`.

**Config models are strict, because stored config is persisted verbatim**
(`create_evaluation` stores `json.dumps(body.config)` after validating a
copy). The four new models use `extra="forbid"`, `StrictStr` for `path` and
`needle`, `Literal["gt","gte","lt","lte"]` for `op`, and
`StrictInt | StrictFloat` with `allow_inf_nan=False` for `expected`
(checked: rejects `"5"`, `True`, `nan`, `inf`, `None`, extra keys; accepts
`5`, `5.5`).

**Path grammar is validated against what the tokenizer actually does.**
`_resolve_path` resolves `$`, `$.`, `$..` to the whole body and resolves
`$.a[]`, `$.a[x]`, `$.a[0` to `_MISSING`. A typo in a `json_path_not_exists`
path would therefore always pass. All four new kinds validate `path` with:

```
^\$(?:\.[^.\[\]\s]+|\[-?\d+\]|\[\?\(@\.[^.\[\]=\s]+==(?:'[^']*'|"[^"]*")\)\])+$
```

It accepts `$.a`, `$.a.b[1]`, `$[0]`, `$.items[-1].name`, and the filter
form from §5, and rejects `$`, `$.`, `$..`, `$a`, `$.a[]`, `$.a[x]`, `$.a[0`,
`$.a.`, `$.a..b`, `$['a']`, `a.b`. `json_path_eq` keeps its current lax
validation in this change (its 114 stored paths in the checked-in bundles
are validated at import only by `min_length=1`, not against the new
grammar); tightening it is a follow-up.

Key segments exclude whitespace (so `$.a `, `$. a`, `$.a\n` are rejected);
quoted literals may contain spaces. The capture validator uses
`re.fullmatch`, because Python's `re.match` with `$` accepts a trailing
newline that pydantic's regex engine rejects, and the two must agree.

| Kind | Config | Passes when | Evidence dict (exact keys) |
|---|---|---|---|
| `json_path_exists` | `{path}` | the path resolves, to any value including `null` | `{path, missing: bool, observed_type}` |
| `json_path_not_exists` | `{path}` | the path does not resolve | `{path, missing: bool, observed_type}` |
| `json_path_contains` | `{path, needle}` | the path resolves to a **string** containing the rendered `needle` | `{path, needle, needle_raw, missing: bool, found: bool, observed_type, observed}` |
| `json_path_cmp` | `{path, op, expected}` | the path resolves to a JSON number (`bool` excluded) and `observed op expected` holds | `{path, op, expected, missing: bool, observed_type, observed}` |

Evidence rules, uniform across the new kinds:

- `missing` always present and means "the path did not resolve". `found`
  is the needle result for `contains`, never overloaded with path presence.
- `observed_type` uses JSON names: `object`, `array`, `string`, `number`,
  `boolean`, `null`; it is `null` when `missing`.
- `observed` is `null` whenever `missing` or the type did not match; the
  `_MISSING` sentinel never reaches an evidence dict (serialising it raises
  inside the terminal-write `try` and the run would stay `running`).
- `observed` is JSON-native and bounded: for `contains` the string is
  **redacted first, then truncated** to `_BODY_EXCERPT_MAX_CHARS` (so a
  secret straddling the cut cannot leak a prefix); for `cmp` it is the
  number; `exists` / `not_exists` never include the value.
- **The same order is applied to the two existing fields with that shape.**
  Today `_execute_request` truncates `response_body_excerpt` to 1,000 and
  `transport_error` to 500 characters before `_redact_result_in_place`
  runs, so a secret straddling either cut leaves its prefix (verified:
  `'xxxxSECRET-abc'` survives in the excerpt tail, `'?k=tok_ssss'` in the
  error tail; httpx exception strings carry the post-substitution URL).
  `_execute_request` returns the untruncated excerpt and error string, and
  the caller does one "redact, then bound" pass over `url`, headers,
  excerpt, `transport_error` and every evaluation `detail`. Its order
  relative to captures is immaterial: redaction returns a new structure and
  captures read the raw body. Bounds, per field: excerpt 1,000 characters
  (strings only; JSON-typed excerpts stay unbounded as today), error string
  500, `contains.observed` 1,000; `json_path_eq.observed` stays unbounded
  as today. The ` (timeout_ms=N)` suffix is appended after bounding so it
  cannot be truncated away.
- `_redact_result_in_place` walks `result["evaluations"][*].detail` with
  the same secret-value replacement as the body excerpt. Today
  `detail.observed` keeps a secret that the excerpt masks; this closes that
  for `json_path_eq` too.
- Non-JSON responses are `r.text`, a string: bare `$` would resolve to the
  HTML of a 502 page, which is why the grammar requires a segment.
- `json_path_contains` on a non-string fails with `observed_type` set; 13
  of the 14 source conditions target a `text` string field and the 14th
  targets `notifications[0].message`, also a string.
- Noted asymmetry, unchanged: `json_path_eq` treats `true == 1`;
  `json_path_cmp` excludes booleans.

Not added: `ne`, regex, array length.

### 2. `timeout_ms` on requests

| Layer | Change |
|---|---|
| Schema (`api/shared/schema.sql`) | `ALTER TABLE requests ADD COLUMN IF NOT EXISTS timeout_ms INTEGER;` after the `capture` line; a `CHECK (timeout_ms IS NULL OR timeout_ms BETWEEN 100 AND 60000)` named `requests_timeout_ms_check`, added through the existing `pg_constraint` DO-block pattern (verified idempotent on PG 17). `NULL` means "engine default". |
| Request API (`api/requests/schemas.py`, `routes.py`) | `timeout_ms: StrictInt \| None = None`, `ge=100, le=60000`, on **create** only (no update route exists; none is added). Returned on every `RequestResponse`: the `RETURNING` list at the create route, the list and get `SELECT`s, and `_row_to_response`, which indexes the row directly and would 500 on a missed site. |
| Fixtures (`api/fixtures/schemas.py`, `engine.py`) | `FixtureRequest.timeout_ms: StrictInt \| None = None`, same bounds. Import upserts it (`SET timeout_ms = EXCLUDED.timeout_ms`; removing the key resets the row, consistent with upsert-by-name). `normalize_for_roundtrip` treats explicit `null` as absent. |
| **Export** (`build_export`, `api/collections/routes.py`) | `build_export` adds the key **only when the row value is not null**, and the export route sets `response_model_exclude_unset=True`. Verified end to end: unset is omitted, explicit `None` would not be, and every other key (`body: null`, `description: null`, `environments: []`, `capture: {}`, `headers: {}`) is set explicitly by `build_export` so nothing else changes. `exclude_none` is not used; it would drop `body` and `description`. |
| Engine (`api/runs/engine.py`) | the run query selects `r.timeout_ms`; the loop copies it into `proxy_row` (selecting it in SQL alone does nothing); `_execute_request` builds `httpx.Timeout(t, connect=min(t, 10.0))`. The timeout `transport_error` stays `f"{type(e).__name__}: {e}"` (keeps `ReadTimeout` vs `ConnectTimeout`) with ` (timeout_ms=N)` appended, under the existing 500-char cap. |
| Metrics (`api/runs/_metrics.py`) | `run_duration_seconds` buckets extended with 1200, 1800, 3600, 7200. |

`httpx.Timeout(t)` is per phase (connect, read, write, pool), not a total;
`timeout_ms × items` is not an upper bound on a run. Connect is capped at
10 s so a black-holed host fails fast. Bounds: the floor stops accidental
instant failure; 60 s is above the suite's largest value (35 s) and
`pagehub-browser`'s slowest sensible wait.

### 3. Run builtins: `{{RUN_ID}}`

The engine merges a builtins map into `subs` **after** environment
variables and **before** captures: `RUN_ID` is the run UUID's hex without
dashes, first 12 characters (the platform runner's format, so migrated
slugs and emails keep their length). Builtin names are reserved everywhere a
name enters: `POST`/`PATCH /v1/environments`, fixture import, and
`_validate_capture_dict` all reject a variable, secret, or capture named
`RUN_ID` with a 422, so no silent override exists. Redaction is unaffected
by the merge: `secret_values` is built from the environment's merged map,
not from `subs`.
`RunResponse` gains nothing; the value is recoverable from the run id.

### 4. `{{VAR}}` substitution inside evaluation config

At evaluation time, **named** string-valued config fields are rendered
through the same `_substitute` as url/headers/body: `expected` when it is
a string (`json_path_eq`), and `needle` (`json_path_contains`,
`body_contains`). `path` is never rendered, so the implementation renders
fields by name, not `_substitute(config, subs)`. Rendering uses the
substitution map **as of before this request's captures are applied**, the
platform's order. Strings only, no coercion: a rendered `"5"` stays a
string. Evidence records both `expected_raw` / `needle_raw` and the
rendered value; the redaction walk (§1) masks a rendered value that came
from a secret. Leftover `{{X}}` in a rendered config string counts in the
request's `substitution_missed`. No checked-in bundle has `{{` in any
evaluation config (checked across all 281), so nothing existing changes.
In the first consumer, needle rendering is load-bearing too: 21 seeds put
`{{…}}` in a `body_contains` needle.

### 5. Filter captures

`_resolve_path` gains one bracket form, `[?(@.key=='value')]` with single
or double quotes: on a list, the first **object** element whose `key` is a
string equal to the literal; non-object elements are skipped (platform
behaviour); `_MISSING` on no match or on a non-list. Equality is string
equality only, with no coercion: the platform compares `str(el.get(key))`,
which also matches the literal `'None'` against an absent key; that is a
deliberate divergence. Tokenizer change, in the `[` branch before the
integer-index path, so a `]` inside a quoted literal cannot truncate the
match:

```python
_FILTER_RE = re.compile(r"\[\?\(@\.([^.\[\]=\s]+)==(?:'([^']*)'|\"([^\"]*)\")\)\]")
m = _FILTER_RE.match(path, i)
if m is not None:
    key = m.group(1); lit = m.group(2) if m.group(2) is not None else m.group(3)
    if not isinstance(cur, list):
        return _MISSING
    hit = next((el for el in cur if isinstance(el, dict)
                and isinstance(el.get(key), str) and el[key] == lit), _MISSING)
    if hit is _MISSING:
        return _MISSING
    cur = hit; i = m.end(); continue
```

The same grammar (§1) is applied by `_validate_capture_dict`, which today
checks only `^\$`; a test proves every capture in the four checked-in
bundles still validates, and a grammar-versus-tokenizer agreement test
includes a `]`-in-literal case.

### 6. Transient retry

The platform runner's actual rule (`app/core/runner.py`, pinned by its
`test_runner_transient_backoff.py`): every attempt is **fired and
evaluated**; it is re-fired only when it is **not passing** and the cause
is transient. Up to **4 attempts total**, backoff 250 ms × 2ⁿ (250, 500,
1000 ms). Only the final attempt is persisted and feeds captures. This
plan adopts that rule, narrowed in one place:

- "Not passing" is the existing per-request `passed` formula
  (`transport_error is None and len(evals) > 0 and all(...)`). One
  divergence follows: the platform never fires a zero-evaluation request;
  this engine fires it and treats it as not passing, so a zero-evaluation
  request answering 503 is retried here.
- An eval that deliberately asserts a 429 or 503 and passes is **not**
  retried (the first consumer has one such request, asserting 429 plus
  `Retry-After`).
- **The classification is produced where the exception is caught, not
  re-derived from a string.** `_execute_request` returns
  `(result, raw_body, transient)`, and `transient` is `True` only in the
  `except httpx.TransportError` arm (all four timeout classes, network
  errors such as `ReadError` on a mid-response reset, protocol and proxy
  errors) and in the per-attempt ceiling arm below, minus
  two deterministic subclasses: `httpx.UnsupportedProtocol` (a URL that
  still contains `{{BASE_URL}}` because the environment is missing; in
  development the SSRF guard passes it through to httpx) and
  `httpx.LocalProtocolError` (a header name with a space or a value with
  leading or trailing whitespace, which the CR/LF sanitiser lets through).
  `RemoteProtocolError` stays transient. Items that
  `_execute_request` marks `blocked:` (SSRF guard, CR/LF headers) never
  raise and are never retried. Not retried: `InvalidURL`,
  `TooManyRedirects`, `DecodingError`, `UnsupportedProtocol`,
  `LocalProtocolError`, and any non-transport `httpx.HTTPError`. The platform retries on any exception;
  this is narrower on purpose.
- **Per-attempt ceiling.** `httpx`'s read timeout is per chunk, so a target
  that emits a byte every 59 s under a 60 s read timeout never times out
  (verified: a 503 trickled over 3.25 s under a 1 s timeout completed).
  Each attempt is therefore wrapped in `asyncio.timeout(t + 10)` (Python
  3.11+, the first 3.11-only call in `api/`; the venv, CI and
  `requires-python` all agree, and the plan states that floor) where `t`
  is the request timeout in seconds; the builtin `TimeoutError` it raises
  is classified transient like an `httpx` timeout, with
  `transport_error: "TimeoutError: attempt ceiling (timeout_ms=N)"`.
- Timeouts are retried because the platform retries them; the 231
  timeout-bearing browser waits in the first consumer rely on it.
- The budget (§6a) is checked before each item and before each retry
  attempt, **before** the backoff sleep, so the bound is the budget plus at
  most one backoff (≤ 1 s) and one attempt ceiling. When the budget trips between attempts, the in-flight
  item keeps its last attempt's result with `attempts = n`; only items not
  yet started are recorded as skipped.
- Constants named for tests, as on the platform: `_TRANSIENT_STATUSES =
  {429, 502, 503, 504}`, `_TRANSIENT_MAX_ATTEMPTS = 4`,
  `_TRANSIENT_BASE_DELAY_MS = 250`; tests patch the delay or
  `asyncio.sleep` so a four-attempt case does not really sleep 1.75 s.
- `RunRequestResult` gains `attempts: int = Field(default=1, ge=0)`,
  \1 `_execute_request` returns `fired: bool` alongside\n  `transient` so the caller does not derive it from the `blocked:` prefix. The default matters: `_row_to_response` re-validates
  stored evidence and deliberately lets a validation error surface as a
  500, so a required field would break `GET /v1/runs` on every
  pre-existing row. A test validates pre-change evidence without the key.
- `latency_ms` is the final attempt's time, as on the platform.

**Retry is not idempotency-aware, and this is stated rather than hidden.**
A 504 means "outcome unknown"; a re-fired `POST` whose first attempt landed
may return 409 on the retry, and that attempt is what is evaluated. The
platform re-fires every method, so this is parity; `attempts > 1` in the
evidence is the diagnostic, and the README's retry section tells authors
to read it that way. 144 requests in the first consumer create uniquely
slugged resources, which is why the diagnostic matters.

### 6a. Run budget

`RUN_BUDGET_SECONDS = 3600` in `api/runs/_constants.py`, imported by name
into the engine so tests patch `engine_mod.RUN_BUDGET_SECONDS`. The clock
is `time.monotonic()` taken when the HTTP loop starts (pre-flight DB time
excluded). Before each item and before each retry attempt the engine
compares elapsed time to the budget; once exceeded, every remaining item
is recorded, mirroring the existing `blocked:` record that the mobile view
already renders as `ERR`:

```
request_id, request_name, method: from the row (as the blocked: record does),
response_status: 0, latency_ms: 0, url: <rendered url, passed through _redact_result_in_place>,
response_headers: {}, response_body_excerpt: null, evaluations: [], captured: [],
substitution_missed: [], passed: false, attempts: 0,
transport_error: "skipped: run budget exceeded"
```

`_aggregate_verdict` already returns `error` when any item carries a
`transport_error`, so no new run-status path is needed; `harness_claim` is
write-once at create and never read by the engine, so there is no
interaction. `evidence.engine_error` is set to `"run budget exceeded"` whenever the
budget trips, whether or not any item was left to skip (if it trips inside
the last item's retry loop, that item keeps its last result and the banner
is the only signal). `run_request_count` and the
`run.completed` payload's `request_count` include skipped items (they are
result rows); the README says so.

With the per-attempt ceiling in §6, a run is bounded by the budget plus
at most one backoff (≤ 1 s) and one attempt ceiling (`timeout_ms + 10 s`,
at most 70 s), 71 s in all, and that bound is tested: a tiny patched budget plus a fake `AsyncClient.request` that
sleeps past it on the first item proves item 1 fired and item 2 was
skipped, without patching `time.monotonic`, which asyncio itself uses.

### 7. Item cap

`COLLECTION_ITEM_CAP = 90`, the same one-line change as PR #17 (which then
rebases trivially), with `test_runs_routes.py` deriving its 50/51 literals
from the constant so the test stops encoding the number, its docstring
updated, `run_request_count` buckets in `_metrics.py` gaining 100 so 51 to
90 items no longer land in `+Inf`, and the stale "50-item × 10 s" comment
there rewritten. **Contract eval in lockstep:** the platform seed
`pagehub_evals_runs_oversize_collection_rejected.py` builds a 51-item
collection and asserts a 422 containing `max items=50`; at 90 that becomes
a 202 and the eval goes red on deploy. That seed is updated in the same
platform PR as the eval-kinds seed, sequenced with the staging deploy.
The seed **cannot** discover the cap: it only ever talks to the platform
evals service and must author `cap + 1` item inserts at seed time. So it
defines `COLLECTION_ITEM_CAP = 90` once, builds `cap + 1` items, asserts
`max items={cap}` and `got {cap + 1}` through f-strings, and says in its
docstring that every future cap change updates that constant in lockstep.
The 422 detail text (`collection exceeds max items=N (got M)`) does not
change. PR #17 has the same exposure and is told so.

### 8. Capabilities on `/health`

`GET /health` returns a raw dict today, which the API rules forbid and
which keeps anything it emits out of OpenAPI. It gains a
`HealthResponse` model in `api/schemas.py`, next to `api/main.py` where the
route lives (not under `api/shared/`, which already holds `schema.py`, the
schema-apply module, and `schema.sql`) (`status`, `version`,
`env`, `git_sha`, `boot: dict[str, Any] | None`, `capabilities:
list[str]`) declared as `response_model`, and `capabilities` is a fixed list this build emits: `kinds:json_path_exists`, `kinds:json_path_not_exists`,
`kinds:json_path_contains`, `kinds:json_path_cmp`, `request_timeout_ms`,
`run_id_builtin`, `config_substitution`, `filter_capture`,
`transient_retry`, `run_budget`. OpenAPI already exposes the kinds enum,
`timeout_ms` and the item cap, but nothing exposes the builtin, the capture
grammar or the retry policy; a consumer that needs them (the first one
does) probes this list instead of inferring from schema shapes. The list
describes what the build supports; a consumer still handles the runs gate
(`RUNS_ENABLED=false` answers 503 on `POST /v1/runs` regardless).

### What does not change

One read-path hazard, stated: `build_export` re-validates stored captures
through `FixtureRequest`, so a capture stored under the old `^\$`-only rule
that the new grammar rejects (`$`, `$.a[]`, `$['a']`, or one named
`RUN_ID`) would make `GET /v1/collections/{id}/export` a 500 for that
collection after deploy. Verified before merge: staging has zero request
rows with a non-empty capture and preprod has no `requests` table, so no
live data is affected. A needle that renders to the empty string is
treated as failed (`empty_needle: true`), not as a vacuous match.


Existing kinds' configs and verdicts (the redaction walk changes what is
persisted for a secret, not any verdict). The DB `kind` column is `TEXT`.
The four checked-in bundles keep their guaranteed invariant, which
`fixtures/README.md` states precisely: **`export → import → export` is
identical**, not `file → export` (three of the four files omit keys the
export re-emits, and that is already true today).

## Actors and what an attacker gets

| Actor | Gets | Does not get |
|---|---|---|
| Operator (JWT) | per-request timeouts up to 60 s; the new kinds; `{{VAR}}` in assertions | anything they could not already do by pointing a request at a slow target |
| **Harness key** (`require_auth` on `POST /v1/runs`, no owner check, no rate limit wired) | can start a run on any collection id, each an in-process background task holding an httpx client, and read its evidence back; a task is now bounded by the run budget plus at most one backoff and one attempt ceiling (71 s) instead of an implicit `50 × 10 s` that a trickling response already defeated | evidence values that the redaction walk now masks, including a secret straddling the excerpt or error-string cut; before this change a secret could be read out of `detail.observed` or out of a truncated tail |
| The system under test | its body is compared by the new kinds: substring test on strings, numeric comparison on numbers, string equality in a filter; a transient failure it returns is re-fired up to 3 more times, including a `POST` whose first attempt may have landed | any evaluation of its content as code; JSONPath-lite stays a fixed tokenizer |
| Unauthenticated caller | the static `/health` capabilities list (feature names, no secrets; the route has no auth today either) | — |
| A bundle author (import) | invalid `config`, bad path grammar, or out-of-range `timeout_ms` is a 422 for the whole import | a stored evaluation that can never fail or never pass |

**Run wall-clock.** Bounded by §6a and the per-attempt ceiling in §6:
the budget plus at most one backoff and one attempt ceiling (71 s) on a
long-lived process (local,
docker, a worker), where every actor who can start a run is authenticated. Before this change the practical bound
was `50 × 10 s`; the budget replaces an implicit bound with an explicit,
tested one. On **Vercel**, function duration bounds a run regardless; even
today's `50 × 10 s` cannot complete there, and neither `timeout_ms` nor the
budget changes that. The README says so.

## Tests

All under `api/tests/`; DB-backed ones skip cleanly without Postgres; CI
runs them against `postgres:17`.

- Engine unit tests per new kind: hit, miss, missing path, type mismatch
  (`bool` for `cmp`, non-string for `contains`), exact evidence dicts.
  Filter capture: match, no match, non-list, non-object element. Config
  substitution: rendered `expected` and `needle`, evidence raw/rendered,
  leftover template counted in `substitution_missed`. Builtin: `RUN_ID`
  present in `subs`, 12 hex chars; reserved name rejected with 422 on
  environment create, patch, and import, and in captures.
  Redaction: a secret inside a matched path is masked in `detail`; a
  secret spanning the 1,000-character excerpt cut and one spanning the
  500-character error-string cut are masked in both fields.
- Schema tests, DB-free via the `_ExplodingConn` pattern: 422 for each
  malformed config (missing `path`, `$`, `$..`, `$.a[]`, bad `op`, string
  or boolean `expected`, `nan`, empty `needle`, extra key), `timeout_ms` out
  of range or non-integer, a capture named `RUN_ID`; every capture in
  `fixtures/*.json` still validates.
- Fixture tests: a bundle with `timeout_ms` imports, the row carries it,
  the export contains it; a bundle without it exports **without the key**;
  the existing `export → import → export` test (today it covers only
  `example.json`) parametrised over
  `fixtures/*.json` with an explicit assertion that no request in those
  exports carries `timeout_ms`; `normalize_for_roundtrip` on explicit
  `null`; an environment defining `RUN_ID` is a 422 on import.
- Engine tests through `execute_run` using the `_FakePool/_FakeConn` in
  `test_runs_engine_concurrency.py`: the `collection_items` query returns a
  row with `timeout_ms` and a real `UUID` `request_id`;
  `httpx.AsyncClient.request` patched at class level returning
  `httpx.Response(200, json=…)` and recording the `timeout` kwarg, for the
  configured and default cases (proves the `proxy_row` threading); a 503-then-200
  sequence yields `attempts: 2`; a passing assertion on a 429 yields
  `attempts: 1`; a `ReadTimeout` then 200 yields `attempts: 2`; four
  timeouts yield `attempts: 4` and the timeout error string; a trickling
  fake (bytes arriving faster than the read timeout but never finishing)
  hits the per-attempt ceiling and is classified transient; a header with
  leading whitespace (`LocalProtocolError`) is not retried; an SSRF-blocked item
  and a `{{BASE_URL}}` URL (`UnsupportedProtocol`) are not retried and
  carry `attempts: 0` and `1` respectively; pre-change evidence without
  `attempts` validates; a run whose elapsed time exceeds a patched budget
  marks the remaining items with the exact skipped record above, sets
  `engine_error`, and finishes `error`.
- Routes: `test_runs_routes.py` derives its cap literals from the constant;
  `test_health.py` asserts the `capabilities` list through `HealthResponse`.
- Contract coverage outside this repo, one platform PR sequenced with the
  staging deploy: `pagehub_evals_runs_eval_kinds.py` gets the four new
  kinds; `pagehub_evals_runs_oversize_collection_rejected.py` moves its single
  cap constant to 90 (it cannot read the cap; see §7).

## Docs and UI

- `fixtures/README.md`: kinds table with all eight config shapes and exact
  evidence; `timeout_ms` with default, bounds, per-phase semantics and the
  Vercel note; reserved builtin names; config substitution; the filter
  capture form; the retry policy, including that a retried `POST` may have
  landed and `attempts > 1` is the signal; the run budget and the
  per-attempt ceiling; the `/health` capabilities list.
- `mobile/app/(drawer)/runs/[id].tsx` `formatEvalDetail`: cases for the four
  kinds, keyed on `missing`, `found`, `observed_type`.

## Rollout

1. Branch from `main` (`aa3dfd0`) in a clean clone; the local checkout
   holds PR #17's branch. That PR touches only `api/runs/_constants.py` and
   the hoppers fixture; the cap change here is identical to its.
2. One PR. CI: ruff, pip-audit, pytest against Postgres. Independent review
   of the diff; iterate until a round surfaces no new critical or important
   finding.
3. Merge. Deploy is tag-driven and out of scope; the first consumer's
   round-trip gate runs against a local docker instance.

## Rollback

Revert the PR. The added column and CHECK are nullable and harmless if
left behind. A partial rollback must keep both the `EvaluationKind`
members **and** their `_CONFIG_VALIDATORS` entries: `build_export`
re-validates stored config through that map and would 500 on export for
any row using a removed kind.
