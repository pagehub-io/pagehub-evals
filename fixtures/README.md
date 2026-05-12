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
      "capture": { },                  // var_name -> JSONPath-lite ($-prefixed)
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
