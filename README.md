# pagehub-evals

**Ground-truth gate for LLM coding harnesses** — with inspiration from Postman.

LLM coding harnesses tend to lie ("I fixed it") and blame others ("must be flaky tests / network / your env"). Pagehub-evals records the agent's claim, runs an independent black-box check (HTTP request collections + assertions, with optional traffic-isolation evidence via twin routing), and emits a non-overrideable verdict.

> Status: **scaffolding only.** API stubs, deploy contract, and mobile shell are in place. The JTBD pivot (verdict-bearing runs, twin-zero-traffic evidence, harness-claim ingest) lands in a follow-up.

## Repo shape

```
api/         FastAPI service (own Postgres, schema applied on boot)
mobile/      Expo (React Native) — drawer + breadcrumbs + SupportWidget
deploy/      Per-env runtime config consumed by pagehub-infra
.github/     CI + tag-triggered deploy via pagehub-infra reusable workflows
```

## Local dev

```bash
make up        # Postgres on :5533, API on :8002, Swagger at /docs
make test
make down
```

## Deploy

Tag-triggered:

```bash
git tag staging-2026-05-10 && git push --tags   # → pagehub-evals-staging.vercel.app
git tag v0.1.0              && git push --tags  # → pagehub-evals-production.vercel.app
```

Both tags fan out through `pagehub-infra/.github/workflows/deploy-app.yml@v1` (Vercel API) and `deploy-cloudflare.yml@v1` (Expo web → Cloudflare Pages).

## Stack

FastAPI + Pydantic, asyncpg, PostgreSQL via Supabase in prod/staging, Expo + TypeScript strict, Vercel + Cloudflare Pages, Modal for background jobs (none in v1).
