# deploy/

Per-environment, **non-secret** runtime config consumed by the
`pagehub-infra` reusable workflows.

## Files

| File | Pushed by | Consumer |
|---|---|---|
| `staging.env` | `deploy-app.yml@v1` → Vercel | FastAPI runtime in staging |
| `production.env` | `deploy-app.yml@v1` → Vercel | FastAPI runtime in production |
| `staging.frontend.env` | `deploy-cloudflare.yml@v1` → Cloudflare Pages | Expo web build (staging) |
| `production.frontend.env` | `deploy-cloudflare.yml@v1` → Cloudflare Pages | Expo web build (production) |

## Format

`KEY=value` per line. `#` comments and blank lines are ignored. Empty
values are skipped (the reusable workflow refuses to write a bare
empty string into Vercel). No shell expansion.

## What does NOT belong here

- **Secrets.** Nothing here. These files are committed to a public
  repo. Secrets flow through GitHub Actions secrets and the reusable
  workflow's `upsert_env` path — see
  `pagehub-infra/.github/workflows/deploy-app.yml`.
- Anything the platform already provides automatically:
  `DATABASE_URL`, `JWT_SIGNING_KEY`, `SERVICE_API_KEY`, `APP_RELEASE`,
  `GIT_SHA`, `APP_SLUG`, `ENVIRONMENT`, `NEW_RELIC_*`, and the
  optional `SENTRY_*` set. The workflow upserts them straight from
  org-level secrets / inputs.

## What DOES belong here

- `APP_URL` — canonical user-visible site (drives CORS gate).
- `PAGEHUB_AUTH_BASE_URL` / `PAGEHUB_AUTH_ISSUER` — explicit registry
  endpoint and JWT issuer for the verifier.
- `ADMIN_EMAILS` — comma-separated operator allowlist.
- `RUNS_ENABLED` — kill switch.
- Frontend public keys — anything that ships in the JS bundle and is
  safe to be public.

## One pre-deploy gap (manual one-time setup)

The runtime requires `ENCRYPTION_KEY` (Fernet key for environment-
secrets ciphertexts). It is **not** pushed by the workflow today — no
org-level secret exists for it yet. Set it manually for each env
before the first deploy:

```bash
# Generate a fresh key:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Push to staging + production Vercel projects:
vercel env add ENCRYPTION_KEY preview production --token=$VERCEL_TOKEN
```

(Follow-up: add a `PAGEHUB_EVALS_ENCRYPTION_KEY_{STAGING,PROD}` pair
to `pagehub-infra` org secrets and an `upsert_env ENCRYPTION_KEY ...`
line to `deploy-app.yml` so this becomes automated.)

The strict env-var policy in `api/config.py` means the app refuses to
boot if any required value is missing — so a missing
`ENCRYPTION_KEY` will surface as a red health-check on the first
deploy, not a silent runtime issue.
