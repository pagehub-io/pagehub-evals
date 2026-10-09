# CLAUDE.md — pagehub-evals

## JTBD

**Pagehub-evals is the ground-truth gate for LLM coding harnesses.** LLM agents
working in long autonomous loops tend to (a) claim work is done when it isn't and
(b) blame failures on environment, flaky tests, or network. Pagehub-evals
records the agent's claim, runs an independent black-box check against the
deployed system, and emits a non-overrideable verdict (pass / fail with
evidence). Postman-style request collections are the building block; verdict-
bearing **runs** are the product.

> Status: **scaffolding only.** This file documents the target shape; the JTBD
> implementation lands in a follow-up.

## Pure evals — no in-repo targets

**Pagehub-evals is a pure evals app: it must never host the system under
test.** No in-repo "modules", demo endpoints, fixture-specific services, or
toy targets. The thing an eval suite exercises is *always* an external,
separately-deployed system reached over HTTP — request templates point at it
via an environment variable (e.g. `{{BASE_URL}}`), never at a route hosted
here. (An early `api/modules/chess/` "eval target" was a mistake and was
removed; a chess conformance suite, when it returns, will be a fixture
against a separately-deployed chess service.) The only HTTP surfaces this app
exposes are its own product surfaces — environments, requests, evaluations,
collections, fixtures, runs, harness keys, events — plus health/metrics. New
endpoints that exist *to be tested by* a fixture do not belong in this repo.

## Generic harness — an app's eval SUITE lives in that app's repo

Complementary to "pure evals": just as this repo hosts no system-under-test, it hosts no
*specific application's eval suite*. Pagehub-evals is a **generic, abstract test harness**
(the engine, the runner, the run/verdict product) plus **a few starter examples** to help
people get going. A given app's collections / fixtures / seeds are that app's own artifact
and live in that app's repo — e.g. serve owns its bundles in `app-serve`
(`evals/collections/serve-*.json`), imported into the harness at runtime; they are not
committed here. A branch that builds out one application's full eval battery *inside*
pagehub-evals (per-app fixtures accreting in `fixtures/`, app-named collections) is drift,
not the model — treat it as that app's content that belongs in that app's repo. When you
extend the harness, make the capability **generic** (a new eval kind, a new locator strategy,
a config knob), never app-specific; if a change is only justified by one app's needs, the
value it hardcodes may be app-specific but the mechanism must not be.


## Surfaces

1. **Standalone site** (Expo web on Cloudflare Pages) — operator triage UI for
   verdicts: filter by app, environment, harness, status; drill into evidence
   (request/response pairs, twin-traffic counts, timing).
2. **Embedded SupportWidget** — same `@pagehub-io/ux` SupportWidget the rest of
   the fleet mounts; nothing pagehub-evals-specific.

## Dependencies

### Hard runtime

- **pagehub-auth**: identity. JWT verifier asserts
  `app_slug == "pagehub-evals"` (mirrors the `app-prayers` pattern, NOT
  pagehub's cross-app inversion).
- **pagehub** (operator support backend) — the `SupportWidget` POSTs tickets
  there, not here.

### Hard build/deploy

- **pagehub-infra** — `app_name: pagehub-evals` in the reusable workflow.
- **shared platform Supabase** — own DB `pagehub_evals` inside the existing
  platform Supabase project (option a from the scaffold plan; one-line
  addition to `pagehub-infra/modules/platform-supabase/main.tf` when ready).

### What pagehub-evals does NOT depend on

- `platform/evals/` (the in-platform Postman-clone). Pagehub-evals is its
  spiritual successor and may absorb its schema, but the two are independent
  deployable units.

## Locked design decisions (scaffold)

1. **app_name / Vercel project slug** — `pagehub-evals`. Domains:
   `pagehub-evals-{staging,production}.vercel.app`. Cloudflare Pages:
   `{staging.,}pagehub-evals-app.pages.dev`.
2. **Local dev ports** — API `8002`, Postgres `5533`. No collision with
   pagehub (`8001`/`5532`) or platform/evals (`4002`).
3. **Auth** — pagehub-auth-issued HS256 JWT, verified locally, slug-matched
   to `pagehub-evals` (per `app-prayers`). Operator allowlist via
   `ADMIN_EMAILS` (default `support@pagehub.io`).
4. **Mobile** — Expo + drawer (auto-opens ≥medium) + breadcrumbs +
   `SupportWidget` from `@pagehub-io/ux@^0.1.0`.
5. **JTBD-pivot deferred** — scaffolding ships pure structural parity. The
   `runs/` resource is a stub (verdict shape declared in `schemas.py`, route
   handler returns 501). Twin-zero-traffic evidence and harness-claim ingest
   land next.

## Stack contract

- Backend: FastAPI in `api/`. Every route declares `response_model`; every
  request body is a Pydantic model.
- Frontend: Expo (React Native) with TypeScript strict mode.
- Database: PostgreSQL via Supabase (staging/prod), Postgres via
  docker-compose (local). Schema applied idempotently from
  `api/shared/schema.sql` on boot.
- Observability: `/metrics` Prometheus endpoint scraped by the platform's
  observability service.

## Scope guardrails for builds and reviews

- **In scope (this scaffold)**: structural parity with `pagehub` + `app-*`,
  the deploy contract for `pagehub-infra`, a bootable but stub-only API.
- **In scope (follow-up JTBD pivot)**: verdict-bearing runs, harness-claim
  ingest, twin-zero-traffic evidence, operator triage UX in `mobile/`.
- **Out of scope**: porting `platform/evals/` data; building a new
  `@pagehub-io/ux` widget; any non-LLM-harness eval use cases.

### Execute the path, don't read it (security claims especially)

**Never report a vulnerability, data leak, or "X can reach Y" as fact on
the strength of reading code. Execute it — issue the credential, send the
request, read the response — and quote the output.** Say "unverified"
until you have.

The failure mode this stops: verifying the two ENDS of a path and
inferring the middle. "This writes settings" + "that reads settings"
does not mean data flows between them — there may be a filter,
blocklist, allowlist, or unreachable branch in between. Applies equally
to claims in specs, claims reported to the user, and findings a reviewer
subagent hands you (verify before repeating; say which claims you checked
yourself). When a path genuinely can't be executed (production data,
destructive side effects), label the claim reasoned-not-executed rather
than presenting inference as fact.
