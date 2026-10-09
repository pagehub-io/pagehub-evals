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

## Local only: pagehub-evals is not deployed

**We don't deploy evals** (owner, 2026-10-08, #34). Pagehub-evals runs only in
each developer machine's local stack (API `8002`), where the fleet's apps run
their full eval suites as the local gate before a `staging-*` tag. No CI job
runs evals against staging or preprod. There is no production.

**Don't cut `staging-*` or `v*` tags in this repo**, whatever the fleet-wide
release order says. `.github/workflows/deploy.yml` still fires on them and
prints a manual redeploy command.

**Still open:** `pagehub-evals-staging.vercel.app` is still served, publicly,
at `3e518fd`, last deployed in May 2026, with the same access model as below.
Taking it down is the owner's call. Until then, the "local only" reasoning
below doesn't cover it. Anything below about domains, Supabase or
`pagehub-infra` is the scaffold's original target, not how it runs.

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
   `{staging.,}pagehub-evals-app.pages.dev`. Production was never
   deployed, and staging is a stale May build (see "Local only" above).
2. **Local dev ports** — API `8002`, Postgres `5533`. No collision with
   pagehub (`8001`/`5532`) or platform/evals (`4002`).
3. **Auth** — pagehub-auth-issued JWT, verified locally by `kid`: EdDSA
   against `PAGEHUB_AUTH_JWKS`; the legacy HS256 fleet kid
   (`JWT_SIGNING_KEYS`) is accepted, logged and counted only until step D
   (pagehub-auth `specs/asymmetric-access-tokens.md` §3.2). Slug-matched
   to `pagehub-evals` (per `app-prayers`).
   **Who is an operator:** anyone holding a valid pagehub-evals token.
   `require_user` only refuses harness keys, so every such token can do all of
   the following:
   - author requests and collections;
   - list every owner's rows, and edit any owner's environment;
   - mint and revoke harness keys;
   - start runs.

   A run can send a stored secret to a URL the operator chooses (read in the
   code, not executed). So the admin-only secrets view below isn't a real
   secrets boundary. `ADMIN_EMAILS` (default
   `support@pagehub.io`) only sets `is_admin`, which today gates nothing but
   `GET /v1/environments/{id}?reveal_secrets=true` (checked on a local stack,
   2026-10-08: a non-admin token lists collections and environments, 200, and
   is refused secrets, 403). On the local stack that's acceptable, because
   the only token holders are that machine's own operators (#34). Settle it
   before pagehub-evals is ever deployed, and for the still-served staging
   above.
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
