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

Inherits the fleet standard. See `~/github/pagehub-io/platform/STANDARD_STACK.md`.

- Backend: FastAPI in `api/`. Every route declares `response_model`; every
  request body is a Pydantic model.
- Frontend: Expo (React Native) with TypeScript strict mode.
- Database: PostgreSQL via Supabase (staging/prod), Postgres via
  docker-compose (local). Schema applied idempotently from
  `api/shared/schema.sql` on boot.
- Observability: `/metrics` Prometheus endpoint scraped by `eyes/`.

## Scope guardrails for builds and reviews

- **In scope (this scaffold)**: structural parity with `pagehub` + `app-*`,
  the deploy contract for `pagehub-infra`, a bootable but stub-only API.
- **In scope (follow-up JTBD pivot)**: verdict-bearing runs, harness-claim
  ingest, twin-zero-traffic evidence, operator triage UX in `mobile/`.
- **Out of scope**: porting `platform/evals/` data; building a new
  `@pagehub-io/ux` widget; any non-LLM-harness eval use cases.
