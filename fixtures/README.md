# fixtures/

Checked-in **fixture bundles** — self-contained declarative JSON files that
*are* eval suites. Importing one brings a pagehub-evals instance to a known
state idempotently; exporting a collection captures it back into source.
This directory replaces imperative `platform/evals/seeds/`-style population
for pagehub-evals itself (the platform layer still uses seeds for its own
eval *coverage* — see `PLAN.md`).

## The bundle schema

A bundle is the `FixtureBundle` Pydantic model. The authoritative shape
lives in the OpenAPI docs (`/docs`, `/redoc`) — `FixtureBundle`,
`FixtureEvaluation`, `FixtureRequest`, `FixtureEnvironment`,
`FixtureCollection`, `FixtureImportResponse`. Sketch:

```jsonc
{
  "version": 1,                       // int, required, must equal 1
  "environments": [                   // optional; OMITTED/[] in exports
    {
      "name": "example-env",
      "variables": { "BASE_URL": "https://httpbin.org" },
      "secrets":   { }                 // KEYS ONLY — every value MUST be "" (non-empty → 422)
    }
  ],
  "requests": [                       // names unique within the bundle
    {
      "name": "example-get",
      "method": "GET",
      "url": "{{BASE_URL}}/get",
      "headers": { "Accept": "application/json" },
      "body": null,                    // arbitrary JSON, or null
      "capture": { },                  // var_name -> JSONPath-lite ($.field, [int], [?(@.key=='v')])
      "timeout_ms": 15000,             // OPTIONAL, 100..60000; omit for the engine default (10 s)
      "evaluations": [                 // INLINE under each request
        { "name": "status-ok", "kind": "status_eq", "config": { "expected": 200 } }
      ]
    }
  ],
  "collections": [                    // names unique within the bundle
    {
      "name": "example-smoke",
      "description": "...",
      "items": [ "example-get" ]       // ARRAY of request *names*; index == position
    }
  ]
}
```

## Workflow

- **Import:** `POST /v1/fixtures/import` with the file as the JSON body
  (operator JWT). Response: `{created, updated}` per resource kind +
  `warnings: list[str]`. Request bodies over **1 MiB (1,048,576 bytes)** are
  rejected with `413` (self-describing body) before any JSON parsing — checked-in
  fixtures are tiny, so this only ever bites a runaway/malformed bundle.
- **Export:** `GET /v1/collections/{id}/export` → a `FixtureBundle` with
  `environments: []`, the collection, every request it references, every
  evaluation on those requests. Pipe it to `fixtures/<name>.json`. Served
  inline as `application/json` (no `Content-Disposition`).
- **Fetch on-disk source:** `GET /v1/fixtures/{name}` (operator JWT) returns
  the **byte-identical** contents of `fixtures/<name>.json` — same length,
  same whitespace, no parse-and-reformat round-trip. Companion to
  `POST /v1/fixtures/import`: that one *consumes* a bundle, this one
  *serves* the canonical source. `name` matches `^[a-z0-9][a-z0-9-]*$` (the
  file stem), so traversal-shaped values are rejected by the router before
  the handler runs. 404 if no file with that name exists in `fixtures/`.
  Use case: pagehub-benchmarks injects the exact fixture bytes the grader
  will use into the build prompt, so "what the harness sees" ≡ "what the
  grader will check."

## Declarative desired-state — the one surprise

`POST /v1/fixtures/import` **replaces** each named request's evaluation set
and each named collection's item list with exactly what the bundle
declares. A re-import **undoes** any evaluation or collection item you added
out-of-band via the resource APIs (`POST /v1/requests/{id}/evaluations`,
`POST /v1/collections/{id}/items`) since the last import — divergence is
discarded, not merged. (Import never *deletes* a request / collection /
environment the bundle doesn't mention; only *children of mentioned
parents* get the replace treatment.) The whole import is one transaction —
all-or-nothing. Re-importing the same bundle reports `created: 0` for every
kind; that's the idempotency signal.

## The round-trip invariant

`export → import → export` is byte-identical modulo timestamps/ids — and in
practice modulo nothing, because export emits requests in first-referenced
order and assigns collection-item positions densely from array index. The
comparison is pinned to `normalize_for_roundtrip(bundle)` in
`api/fixtures/schemas.py` (the single source of truth used by both
`api/tests/test_fixtures_roundtrip.py` and the platform eval seed).

> The *guaranteed* invariant is `export → import → export`: byte-stable.
> `file → import → export` is "close but not byte-identical" — export emits
> object keys in Pydantic field-definition order (not alphabetical), and a
> request `body` round-trips through JSONB (which drops key order /
> insignificant whitespace), so a literal `diff` of a hand-authored file
> vs. a fresh export still shows key-order churn. Authoring committed
> fixtures with sorted keys + JSONB-canonical bodies keeps them readable and
> makes a `json.dumps(..., sort_keys=True)` comparison of file vs. export
> clean — but a raw `diff` of the two isn't.

## Secrets never cross the boundary

A fixture carries secret **keys** only, with `""` placeholder values — a
non-empty secret value is a hard `422` at parse time. On import, a known key
keeps the environment's stored ciphertext untouched; a new key is created
with an empty placeholder for the operator to fill in via
`PATCH /v1/environments/{id}`. Export never emits `environments` at all
(always `[]`), so no secret material (or even a secret *key name*) ever
lands in a git-tracked file.

## `fixtures/example.json`

A minimal, illustrative bundle — and the one the fixture round-trip test
(`api/tests/test_fixtures_roundtrip.py`) and the platform fixtures
eval-seed import. It carries:

- env `example-env` with `BASE_URL → https://httpbin.org` — operators
  override `BASE_URL` for whatever target they actually point the
  collection at.
- two requests, `example-get` (`GET {{BASE_URL}}/get`, evals: status 200 +
  `body_contains "httpbin"`) and `example-status-418` (`GET
  {{BASE_URL}}/status/418`, eval: status 418).
- collection `example-smoke` ordering those two.

It is deliberately *not* a real conformance suite — eval **targets are
always external** to this app (see `CLAUDE.md` § "Pure evals — no in-repo
targets"). A future chess conformance suite, for example, will be a fixture
whose `requests[]` point at a separately-deployed chess service via
`{{BASE_URL}}`, not at anything hosted here.

## Evaluation kinds

Every `config` is validated strictly at write time (unknown shape → 422,
never a stored evaluation that silently never runs). Paths use the
JSONPath-lite grammar: `$` then one or more of `.field`, `[int]`, or the
filter `[?(@.key=='value')]` (first object element whose `key` equals the
string literal; non-object elements are skipped; no coercion). A bare `$`
is rejected: a non-JSON response is a string, and `$` would resolve to the
HTML of a 502 page.

| Kind | Config | Passes when | Evidence |
|---|---|---|---|
| `status_eq` | `{expected}` | status equals | `{expected, observed}` |
| `json_path_eq` | `{path, expected}` | value at path equals `expected` (`{{VAR}}` in a string `expected` is rendered) | `{path, expected, expected_raw?, observed, missing?}` |
| `header_present` | `{header}` | header exists (case-insensitive) | `{header, present}` |
| `body_contains` | `{needle}` | whole body (JSON dump or text) contains the rendered `needle` | `{needle, needle_raw?, present}` |
| `json_path_exists` | `{path}` | path resolves, to anything including `null` | `{path, missing, observed_type}` |
| `json_path_not_exists` | `{path}` | path does not resolve | `{path, missing, observed_type}` |
| `json_path_contains` | `{path, needle}` | path resolves to a **string** containing the rendered `needle` | `{path, needle, needle_raw, missing, found, observed_type, observed}` (observed bounded to 1,000 chars) |
| `json_path_cmp` | `{path, op, expected}`, `op` in `gt gte lt lte`, numeric `expected` | path resolves to a JSON number (booleans excluded) and the comparison holds | `{path, op, expected, missing, observed_type, observed}` |

`observed_type` uses JSON names: `object`, `array`, `string`, `number`,
`boolean`, `null`. Config substitution renders only `expected` (when a
string) and `needle`, never `path`, using the substitution map as of before
the request's own captures; a leftover `{{X}}` is reported in the request's
`substitution_missed`.

## Run-time behaviour worth knowing

- **`{{RUN_ID}}`** is a builtin: the run UUID's hex, first 12 characters,
  merged after environment variables and before captures. The name is
  reserved; an environment variable, secret, or capture named `RUN_ID` is
  a 422.
- **`timeout_ms`** is per request, 100 to 60,000, default 10,000. httpx's
  timeout is per phase (connect capped at 10 s, then read/write per chunk),
  not a total; each attempt additionally has a hard ceiling of
  `timeout_ms + 10 s` so a trickling response cannot hold a run.
- **Transient retry.** Every attempt is fired and evaluated; it is re-fired
  only when it is not passing and the cause is a transport error (timeouts
  included) or a 429/502/503/504, up to 4 attempts with 250 ms exponential
  backoff. A deliberately asserted 429 that passes is not retried. Retry is
  not idempotency-aware: a re-fired `POST` whose first attempt landed may
  answer 409 on the retry, and that attempt is what is evaluated; read
  `attempts > 1` in the evidence as "an earlier attempt may have had an
  effect". Unrendered `{{BASE_URL}}` URLs and malformed headers are
  deterministic and never retried.
- **Run budget.** A run's HTTP loop is bounded by 3,600 s (checked before
  each item and each retry attempt); remaining items are recorded with
  `transport_error: "skipped: run budget exceeded"`, `attempts: 0`, and the
  run finishes `error` with `engine_error` set. The count of executed
  requests includes skipped ones. On Vercel, function duration bounds a
  run regardless of any of this; long collections need a long-lived
  process (docker or a worker).
- **`GET /health`** lists `capabilities` so a consumer can probe what this
  build supports (the builtin, the filter grammar and the retry policy are
  not visible in OpenAPI). A runs gate (`RUNS_ENABLED=false`) still answers
  503 regardless.
- Evidence is redacted before it is bounded: a secret straddling the
  1,000-character excerpt cut or the 500-character error-string cut is
  masked, and evaluation `detail` values are redacted too.
